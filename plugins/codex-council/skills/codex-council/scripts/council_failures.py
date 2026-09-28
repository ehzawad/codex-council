"""Failure classification for one codex exec attempt (codex-council runner).

Reads stderr plus the structured `error` / `turn.failed` events of codex's
JSONL stdout (never agent messages, reasoning, or tool output), decides one
verdict in a fixed order (auth, quota, anchored 429/5xx, model rejected,
stale on resume only, substring retriable fallback, untagged), and formats
the tagged error text a role reports. A model rejection, and a usage limit
Codex names for one model, end with one action for the model that was sent.
The stall verdict is structured and decided by the runner before any of
this runs.
"""

import json
import re
from dataclasses import dataclass
from typing import Optional

from council_common import _dedupe_preserve_order, _iter_json_objects
from council_selection import _INHERIT_DECISION

# Substring markers (matched case-insensitively) classifying failure modes.
# These are the FALLBACK signal; the primary signal is the numeric HTTP status
# parsed out of the JSONL error body (see _extract_statuses) and, for quota
# and model rejection, the structured failure records (_failure_records).
# Order of check, identical on the fresh and resume paths (see
# _failure_verdict): auth first (never clear state), then quota (terminal,
# even when it carries HTTP 429), then ANCHORED-status retriable (a real API
# 429/5xx — by JSON status, `HTTP NNN`, or a reason phrase — beats a
# stale-looking message), then model rejection (terminal; never clears
# state), then stale-resume (resume only: clear and restart), then the
# SUBSTRING retriable fallback — kept last so a stale error that merely
# contains a bare digit run (e.g. "...stale-429-sid") still restarts fresh
# instead of being mistaken for a rate limit.
AUTH_ERROR_MARKERS = (
    "401 unauthorized",
    "incorrect api key",
    "authentication failed",
    "auth: token rejected",
    "please run `codex login`",
    "please run codex login",
    # current codex-cli terminal refresh-token failure wording ("Your access
    # token could not be refreshed because your refresh token ...").
    "access token could not be refreshed",
)
RATE_LIMIT_MARKERS = (
    # NB: bare "429" is intentionally NOT here — codex normalizes ordinary
    # HTTP errors to text carrying the real transport status, which the
    # anchored parser (_extract_statuses) reads, so a bare digit run like
    # "4291" or "stale-429-sid" is never mistaken for a rate limit. The phrase
    # markers below cover codex's code-less rewrites; an echoed status phrase
    # inside an error.message has no provenance and remains a known limit.
    "rate limit",
    "rate_limit",
    "too many requests",
    # Exact current codex wording for a throttled SSE stream: response.failed
    # handling drops code/status_code/statusCode and keeps only the message
    # ("stream disconnected before completion: Request was throttled").
    # Deliberately NOT bare "throttled" — quota/policy prose could collide.
    "request was throttled",
    # NB: "quota exceeded" / usage caps are deliberately NOT retriable markers —
    # a plan/usage cap does not clear within a 5s backoff, so it is surfaced
    # terminal (see "Retries and long runs" in references/runtime-behavior.md
    # and DESIGN.md); the recognized quota forms are tagged [quota] ahead of
    # the anchored parser (QUOTA_ERROR_CODES). Genuine transient
    # 429s are caught by the anchored parser or the rate-limit phrases above.
)
TRANSIENT_5XX_MARKERS = (
    "500 internal",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
    "internal server error",
    "service unavailable",
    # current codex-cli friendly-rewrites some upstream 5xx/overload errors to
    # prose that carries no status code (HTTP 500 -> "...experiencing high
    # demand..."; overload -> "...server overloaded..."; HTTP 503
    # server_is_overloaded/slow_down -> "Selected model is at capacity. Please
    # try a different model."). "backend overloaded" is kept as a fallback for
    # older codex/provider text. These phrases are version-coupled fallbacks
    # for the code-less case; the numeric range in _structured_retriable_class
    # handles every 5xx that DOES carry a status. The overload markers are
    # intentionally specific (not bare "overloaded") so unrelated text like
    # "operator overloaded" is not matched.
    "server overloaded",
    "backend overloaded",
    "experiencing high demand",
    "selected model is at capacity",
)
STALE_RESUME_MARKERS = (
    "no rollout found",
    "thread not found",
    "session not found",
    "session expired",
    "thread expired",
)

# A definitively non-retriable error TYPE that codex/OpenAI put in the JSONL
# error body for 4xx client errors. current codex-cli sometimes surfaces a 400
# as raw JSON with this type but NO numeric status; its presence (when no
# anchored retriable status is found) suppresses the substring retriable
# fallback, so a 400 whose message text merely contains a 5xx reason phrase or
# "too many requests" is not wrongly retried.
NONRETRIABLE_ERROR_TYPE_MARKERS = (
    "invalid_request_error",
)

# Terminal quota/billing failures, matched against a structured failure
# record's error code or type (never retried, never clearing state). They
# are checked BEFORE the anchored parser because the provider can send them
# with HTTP 429, which would otherwise read as a transient rate limit.
QUOTA_ERROR_CODES = frozenset({
    "insufficient_quota",
    "usage_limit_reached",
    "usage_limit_exceeded",
    "credit_balance_exhausted",
    "organization_spend_limit_exceeded",
    "project_spend_limit_exceeded",
    "organization_usage_limit_exceeded",
})
# current codex-cli rewrites a ChatGPT plan usage cap to code-less prose
# ("You've hit your usage limit. ...").
QUOTA_MARKERS = (
    "hit your usage limit",
)
# codex-cli 0.157.1's wording when the cap applies to one model rather than
# the plan: "You've hit your usage limit for <label>. Switch to another model
# now, or try again at <time>." (with a typographic apostrophe). The label is
# the server's name for the limit, not an echo of the -m value, so it is
# never compared with the model sent: the message itself says the model
# that request used is capped, and the tag adds the action for that model.
_MODEL_USAGE_LIMIT_RE = re.compile(
    r"hit your usage limit for \S.*?\. Switch to another model",
    re.IGNORECASE,
)
# A failure naming one of these parameters is about the effort or service
# tier, not the model, so it is never read as a model rejection.
MODEL_REJECTION_EXCLUDED_PARAMS = (
    "reasoning.effort",
    "model_reasoning_effort",
    "service_tier",
)


# ---------- failure text and structured failure records ----------

def extract_error_messages(jsonl_output):
    """Pull structured error messages from Codex JSONL stdout."""
    messages = []
    for event in _iter_json_objects(jsonl_output):
        event_type = event.get("type")
        if event_type == "error":
            message = event.get("message")
            if not isinstance(message, str):
                error = event.get("error")
                if isinstance(error, dict):
                    message = error.get("message")
            if isinstance(message, str) and message.strip():
                messages.extend(_expand_error_message(message))
        elif event_type == "turn.failed":
            error = event.get("error")
            if isinstance(error, dict):
                message = error.get("message")
            else:
                message = error
            if isinstance(message, str) and message.strip():
                messages.extend(_expand_error_message(message))
    return _dedupe_preserve_order(messages)


def _expand_error_message(message):
    """Return the message plus any nested JSON error.message it contains."""
    stripped = message.strip()
    messages = [stripped]
    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError:
        return messages
    if isinstance(decoded, dict):
        error = decoded.get("error")
        if isinstance(error, dict):
            inner = error.get("message")
            if isinstance(inner, str) and inner.strip():
                messages.append(inner.strip())
    return messages


def _failure_text(stdout, stderr):
    """Combine stderr and structured stdout error events for classification."""
    parts = []
    stderr_stripped = stderr.strip()
    if stderr_stripped:
        parts.append(stderr_stripped)
    parts.extend(extract_error_messages(stdout))
    return "\n".join(parts)


@dataclass(frozen=True)
class FailureRecord:
    """One structured failure from a codex `error` / `turn.failed` event.

    Fields are None when absent; `status` is the HTTP status of the nearest
    enclosing level that carried one.
    """
    status: Optional[int] = None
    type: Optional[str] = None
    code: Optional[str] = None
    param: Optional[str] = None
    message: Optional[str] = None


# codex exec reports a failed request as a message string that is often
# itself JSON ({"type":"error","status":400,"error":{"type":..,"code":..,
# "param":..,"message":..}}), whose error.message can nest JSON again. At
# most this many JSON-in-message levels are decoded.
_FAILURE_JSON_DEPTH = 3


def _json_object(text):
    """text decoded as a JSON object, or None."""
    if not text.lstrip().startswith("{"):
        return None
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _str_or_none(value):
    return value if isinstance(value, str) else None


def _failure_status(obj, inherited):
    for key in ("status", "status_code", "statusCode"):
        value = obj.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return inherited


def _collect_failure_records(value, status, depth, records):
    """Append the records one failure payload (text or object) carries."""
    if isinstance(value, str):
        decoded = _json_object(value) if depth < _FAILURE_JSON_DEPTH else None
        if decoded is None:
            if value.strip():
                records.append(FailureRecord(status, message=value.strip()))
            return
        value, depth = decoded, depth + 1
    if not isinstance(value, dict):
        return
    status = _failure_status(value, status)
    error = value.get("error")
    if isinstance(error, str):
        _collect_failure_records(error, status, depth, records)
        return
    fields = error if isinstance(error, dict) else value
    message = _str_or_none(fields.get("message"))
    nested = (
        message is not None and depth < _FAILURE_JSON_DEPTH
        and _json_object(message) is not None
    )
    record = FailureRecord(
        status, _str_or_none(fields.get("type")),
        _str_or_none(fields.get("code")), _str_or_none(fields.get("param")),
        None if nested or message is None else message.strip() or None,
    )
    if any((record.type, record.code, record.param, record.message)):
        records.append(record)
    if nested:
        _collect_failure_records(message, status, depth, records)


def _failure_records(jsonl_output):
    """Structured failure records from codex `error` / `turn.failed` events.

    Only those two event types are read — never agent_message, reasoning,
    or tool output — and JSON-in-message forms are decoded up to
    _FAILURE_JSON_DEPTH levels. Duplicates (codex repeats one failure in
    both events) collapse.
    """
    records = []
    for event in _iter_json_objects(jsonl_output):
        kind = event.get("type")
        if kind == "error":
            payloads = (event.get("message"), event.get("error"))
        elif kind == "turn.failed":
            payloads = (event.get("error"),)
        else:
            continue
        for payload in payloads:
            _collect_failure_records(payload, None, 0, records)
    return _dedupe_preserve_order(records)


# ---------- error classifiers ----------

def _stderr_contains(stderr_text, markers):
    lowered = stderr_text.lower()
    return any(m in lowered for m in markers)


def _is_auth_error(stderr_text):
    return _stderr_contains(stderr_text, AUTH_ERROR_MARKERS)


def _is_rate_limit_error(stderr_text):
    return _stderr_contains(stderr_text, RATE_LIMIT_MARKERS)


def _is_transient_5xx_error(stderr_text):
    return _stderr_contains(stderr_text, TRANSIENT_5XX_MARKERS)


def _is_stale_resume_error(stderr_text):
    return _stderr_contains(stderr_text, STALE_RESUME_MARKERS)


# Numeric HTTP status as codex surfaces it. current codex-cli does NOT put a
# status on the top-level JSONL event, so we scan the combined failure text for
# an ANCHORED status — one in a recognizable status context, so a bare digit run
# (e.g. "429" inside a thread id like "stale-429-sid") is never mistaken for one.
# Two anchors are accepted:
#   * keyword-prefixed: `"status": 429`, `status 529`, `status code 429`,
#     `status_code: 400`, `statusCode: 503`, `HTTP 429`, `last status: 429`
#     (the JSON key spellings and the prose forms);
#   * reason-phrase-suffixed: `429 Too Many Requests`, `503 Service Unavailable`,
#     `502 Bad Gateway`, `504 Gateway Timeout`, `500 Internal Server Error`,
#     `529 <unknown status code>` (codex's "unexpected status N" form).
# Anchored detection is the PRIMARY retriable signal and (unlike a bare
# substring) is trusted ahead of the stale check on the resume path.
# The separator class excludes "/" so a URL like `http://127.0.0.1:8080/...`
# is NOT read as "http" + status 127; only real `HTTP 429` / `status: 429`
# forms match.
_STATUS_KEYWORD_RE = re.compile(
    r"(?:^|[^0-9a-z_])(?:http|status(?:[\s_-]*code)?)[\s:=\"']*([0-9]{3})(?![0-9])",
    re.IGNORECASE,
)
_STATUS_REASON_RE = re.compile(
    r"(?<![0-9])([0-9]{3})\s+(?:too many requests|bad gateway|service unavailable"
    r"|gateway timeout|internal server error|<unknown status code>)",
    re.IGNORECASE,
)


def _extract_statuses(text):
    """Return the anchored HTTP status codes named in failure text (deduped).

    "Anchored" = appearing in a status context (a `status`/`HTTP` keyword, or a
    canonical HTTP reason phrase), never a bare digit run. This is what lets a
    real `HTTP 429 Too Many Requests` be treated as authoritative — and beat the
    stale-resume check — while `...thread id stale-429-sid` names no status.
    """
    found = _STATUS_KEYWORD_RE.findall(text) + _STATUS_REASON_RE.findall(text)
    out = []
    for m in found:
        s = int(m)
        if s not in out:
            out.append(s)
    return out


def _structured_retriable_class(text):
    """Retriable class from an ANCHORED HTTP status only (never a bare substring).

    "Anchored" = a status in keyword (`HTTP 429`, `status 529`) or reason-phrase
    (`429 Too Many Requests`) context, per _extract_statuses. Returns
    "rate-limit" (429), "5xx" (500-599), or None. Used ahead of the stale check
    on the resume path so a genuine anchored 429/5xx (e.g.
    "HTTP 429 Too Many Requests; thread not found") is retried, while a stale
    message whose only digits are a thread id (e.g. "stale-429-sid") names no
    status and so does not fire here.
    """
    statuses = _extract_statuses(text)
    if any(s == 429 for s in statuses):
        return "rate-limit"
    if any(500 <= s <= 599 for s in statuses):
        return "5xx"
    return None


def _retriable_class(text):
    """Full retriable classification: structured status first, then substrings.

    A structured status is authoritative when present: a non-retriable status
    (e.g. 400/403) returns None and SUPPRESSES the substring fallback, so a bare
    "429" or "service unavailable" echoed inside a 400 body no longer forces a
    wrong retry. A non-retriable error TYPE ("invalid_request_error") suppresses
    the fallback the same way, for 4xx bodies codex surfaces without a numeric
    status. Substring markers apply only when codex emitted no parseable status
    and no client-error type (e.g. stderr-only transport errors, or the
    version-coupled overload phrases above).
    """
    statuses = _extract_statuses(text)
    if statuses:
        if any(s == 429 for s in statuses):
            return "rate-limit"
        if any(500 <= s <= 599 for s in statuses):
            return "5xx"
        return None
    # No anchored status. A definitively non-retriable error TYPE (a 4xx client
    # error codex surfaces as `"type": "invalid_request_error"`, sometimes
    # without a numeric status) also suppresses the substring fallback, so a 400
    # whose message text merely contains a 5xx reason phrase is not retried.
    if _stderr_contains(text, NONRETRIABLE_ERROR_TYPE_MARKERS):
        return None
    if _is_rate_limit_error(text):
        return "rate-limit"
    if _is_transient_5xx_error(text):
        return "5xx"
    return None


def _is_quota_error(failure_text, records):
    """True for a terminal quota/billing failure: a recognized structured
    code (error.code or error.type) or codex's usage-limit prose."""
    for record in records:
        if record.code in QUOTA_ERROR_CODES or record.type in QUOTA_ERROR_CODES:
            return True
    return _stderr_contains(failure_text, QUOTA_MARKERS)


def _names_excluded_param(text):
    """True when text names reasoning effort or service tier."""
    lowered = text.lower()
    return any(name in lowered for name in MODEL_REJECTION_EXCLUDED_PARAMS)


# Codex passes the provider's rejection sentence through as text. The model
# id is in single quotes in the ChatGPT sign-in form observed live, and in
# backticks in the OpenAI API's model_not_found wording, so either quote is
# accepted around it.
_MODEL_QUOTE = "['`]"


def _model_rejection_patterns(requested_model):
    """Codex's complete rejection sentences for the model this invocation
    sent (regex-escaped), or for any model when none was sent. The model
    may be quoted with single quotes or backticks."""
    q = _MODEL_QUOTE
    model = re.escape(requested_model) if requested_model else r"[^'`]+"
    return (
        # ChatGPT sign-in; the trailing account wording varies.
        re.compile(
            rf"The {q}{model}{q} model is not supported when using Codex with"
        ),
        # API wording: unavailable or inaccessible (the two are not told
        # apart). Codex prints it bare or after its "unexpected status NNN
        # ...: " prefix; a line search finds it either way.
        re.compile(
            rf"The model {q}{model}{q} does not exist or you do not have "
            "access to it"
        ),
    )


def _model_rejection(failure_text, records, requested_model):
    """Codex's own message when it rejected this invocation's model, or None.

    Positive evidence only: a structured `model_not_found` code (status
    400, 404, or absent), or one of Codex's complete rejection sentences.
    Bare "not found" / "not supported", the "Model metadata for ... not
    found" advisory, and "Selected model is at capacity" (transient) never
    qualify. A structured failure naming reasoning effort or service tier
    is about that setting, so none of its text counts; neither does a
    text line naming one.
    """
    if any(
        _names_excluded_param(f"{record.param or ''} {record.message or ''}")
        for record in records
    ):
        return None
    for record in records:
        if record.code == "model_not_found" and record.status in (
            None, 400, 404,
        ):
            return record.message or "model_not_found"
    candidates = [record.message for record in records if record.message]
    candidates += failure_text.splitlines()
    patterns = _model_rejection_patterns(requested_model)
    for text in candidates:
        if _names_excluded_param(text):
            continue
        if any(pattern.search(text) for pattern in patterns):
            return text.strip()
    return None


@dataclass(frozen=True)
class FailureVerdict:
    """One attempt's failure class (see _failure_verdict).

    `kind` is "auth", "quota", "rate-limit", "5xx", "model-rejected",
    "stale", or None (untagged); `rejection` is Codex's own rejection
    message when kind is "model-rejected", else None.
    """
    kind: Optional[str]
    rejection: Optional[str] = None


def _failure_verdict(failure_text, records, requested_model, resume=False):
    """Classify a non-zero codex exit; the stall verdict is decided earlier.

    Precedence, identical on the fresh and resume paths: auth -> quota ->
    anchored 429/5xx -> model rejected -> stale (resume only) -> substring
    retriable fallback -> None (untagged). Returns a FailureVerdict, whose
    rejection message is parsed here, once per attempt.
    """
    if _is_auth_error(failure_text):
        return FailureVerdict("auth")
    if _is_quota_error(failure_text, records):
        return FailureVerdict("quota")
    anchored = _structured_retriable_class(failure_text)
    if anchored:
        return FailureVerdict(anchored)
    rejection = _model_rejection(failure_text, records, requested_model)
    if rejection is not None:
        return FailureVerdict("model-rejected", rejection)
    if resume and _is_stale_resume_error(failure_text):
        return FailureVerdict("stale")
    return FailureVerdict(_retriable_class(failure_text))


# ---------- failure tags ----------

# The recovery action for a refused model ([model-rejected], or a [quota]
# usage limit Codex names for one model) depends on WHICH model was sent,
# not on how the role chose it. A routed model or an explicit model pin can
# be dropped. The natively configured model (sent by a native_effort role,
# or inherited, as by an effort-only pin) is what an inheriting re-run
# would send again, so only a configuration change or an explicit pin
# avoids it, and both are the user's decision: the action addresses the
# user, never the orchestrator, which must not edit Codex configuration or
# pick a model on the user's behalf. The runner never substitutes or
# replays: the host re-runs the role.
_INHERIT_ACTION = (
    "Re-run this role with model, effort, and selection omitted to inherit "
    "native configuration."
)
_CHANGE_PIN_ACTION = "Change or remove the explicit pin."
_NATIVE_MODEL_ACTION = (
    "Ask the user to update the Codex configuration (model) or to name a "
    "model to pin."
)


def _refused_model_action(decision):
    """The one recovery action when Codex refused the model `decision` sent
    (a rejection, or that model's usage limit)."""
    if decision.dispatch_model is None or decision.provenance == "native_effort":
        return _NATIVE_MODEL_ACTION
    if decision.provenance == "routed":
        return _INHERIT_ACTION
    return _CHANGE_PIN_ACTION


def _model_rejected_error(decision, provider_message, phase):
    """The terminal [model-rejected] text: what was rejected, Codex's own
    message, and one action for the model that was refused."""
    if decision.dispatch_model:
        subject = f"requested model '{decision.dispatch_model}'"
    else:
        subject = "natively configured model"
    # Only a resume has a saved thread this failure could have touched.
    kept = " and the saved thread was kept" if phase == "resume" else ""
    return (
        f"[model-rejected] Codex rejected the {subject} for this invocation: "
        f"{provider_message.rstrip(' .')}. No substitute model was "
        f"tried{kept}. {_refused_model_action(decision)}"
    )


def _classify_failure(failure_text, rc, phase, records=(), decision=None,
                      verdict=None):
    """Return a tagged error string for a non-zero codex exit.

    `records` are the attempt's _failure_records and `decision` the role's
    SelectionDecision (inheritance when omitted). `verdict` is the
    attempt's FailureVerdict when the caller already has one: the resume
    path passes the verdict it branched on, so the tag always matches that
    branch. Without it the fresh-path order is applied here. A stale
    resume is never formatted: the resume path restarts fresh instead.
    A [quota] whose text is Codex's usage limit for one model ends with
    the action for the model that was sent; any other [quota] keeps
    Codex's text alone.
    """
    decision = decision or _INHERIT_DECISION
    if verdict is None:
        verdict = _failure_verdict(failure_text, records,
                                   decision.dispatch_model)
    detail = failure_text or f"codex {phase} exited {rc}"
    if verdict.kind == "quota" and _MODEL_USAGE_LIMIT_RE.search(detail):
        return f"[quota] {detail} {_refused_model_action(decision)}"
    if verdict.kind in ("auth", "quota"):
        return f"[{verdict.kind}] {detail}"
    if verdict.kind in ("rate-limit", "5xx"):
        return f"[retriable:{verdict.kind}] {detail}"
    if verdict.kind == "model-rejected":
        return _model_rejected_error(decision, verdict.rejection, phase)
    return detail
