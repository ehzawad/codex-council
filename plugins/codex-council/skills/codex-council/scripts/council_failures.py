"""Failure classification for one codex exec attempt (codex-council runner).

Reads stderr plus the structured `error` / `turn.failed` events of codex's
JSONL stdout (never agent messages, reasoning, or tool output), decides one
verdict in a fixed order (auth, quota, anchored 429/5xx, model rejected,
stale on resume only, substring retriable fallback, untagged), and formats
the tagged error text a role reports. The verdict, not that text, carries
whether the attempt may be retried (FailureVerdict.retriable): provider text
that happens to begin with a tag never decides a retry. A model rejection,
and a usage limit Codex names for one model, end with one action for the
model that was sent. The stall verdict is structured and decided by the
runner before any of this runs.
"""

import json
import re
from dataclasses import dataclass

from council_common import _dedupe_preserve_order, _iter_json_objects

# Substring markers (matched case-insensitively) classifying failure modes.
# These are the FALLBACK signal; the primary signal is the numeric HTTP status
# parsed out of the JSONL error body (see _extract_statuses) and, for quota
# and model rejection, the structured failure records (_failure_records).
# Order of check, identical on the fresh and resume paths (see
# _failure_verdict): auth first (never clear state; a structured 401 or
# authentication code, or the prose below), then quota (terminal,
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
    # codex-cli's terminal refresh-token failure ("Your access token could
    # not be refreshed because your refresh token ...").
    "access token could not be refreshed",
)
# Structured authentication failures, matched against a failure record's
# error code or type (alongside HTTP 401 on a record or as an anchored
# status). Checked first, ahead of the stale-resume text, so an
# authentication failure whose message also looks stale never clears state.
AUTH_ERROR_CODES = frozenset({
    "authentication_error",
    "invalid_api_key",
})
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
    # and "Failure-class tagging" in DESIGN.md); the recognized quota forms
    # are tagged [quota] ahead of the anchored parser (QUOTA_ERROR_CODES).
    # Genuine transient 429s are caught by the anchored parser or the
    # rate-limit phrases above.
)
TRANSIENT_5XX_MARKERS = (
    "500 internal",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
    "internal server error",
    "service unavailable",
    # codex-cli rewrites some upstream 5xx/overload errors to prose that
    # carries no status code (HTTP 500 -> "...experiencing high demand...";
    # overload -> "...server overloaded..."; HTTP 503
    # server_is_overloaded/slow_down -> "Selected model is at capacity. Please
    # try a different model."). These phrases cover only the code-less case;
    # the anchored status range (_anchored_retriable_class) handles every 5xx
    # that DOES carry a status. The overload marker is intentionally specific
    # (not bare "overloaded") so unrelated text like "operator overloaded" is
    # not matched.
    "server overloaded",
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
# error body for 4xx client errors. codex-cli sometimes surfaces a 400
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
# codex-cli rewrites a ChatGPT plan usage cap to code-less prose
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
# A failure record whose structured `param` is one of these is about the
# effort or service tier, not the model, so none of its text is read as a
# model rejection. Only that record is set aside: another record in the
# same failure can still be the rejection.
MODEL_REJECTION_EXCLUDED_PARAMS = (
    "reasoning.effort",
    "model_reasoning_effort",
    "service_tier",
)
# The same names as whole tokens in text that carries no structured param
# (a stderr line, a plain message). A model id is free to contain these
# words, so the quoted model id a rejection sentence names is removed
# before this is searched (see _sentence_rejection).
_EXCLUDED_PARAM_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:reasoning\.effort|model_reasoning_effort"
    r"|service_tier)(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
# The verdict kinds the runner retries (see FailureVerdict.retriable).
RETRIABLE_KINDS = ("rate-limit", "5xx")


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
    status: int | None = None
    type: str | None = None
    code: str | None = None
    param: str | None = None
    message: str | None = None


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

def _text_contains(text, markers):
    """True when the combined failure text holds any marker (any case)."""
    lowered = text.lower()
    return any(m in lowered for m in markers)


def _is_auth_error(failure_text, records=()):
    """True for an authentication failure: a structured one (HTTP 401 on a
    failure record or as an anchored status in the text, or an
    AUTH_ERROR_CODES code or type) or Codex's authentication prose."""
    for record in records:
        if (record.status == 401 or record.code in AUTH_ERROR_CODES
                or record.type in AUTH_ERROR_CODES):
            return True
    if 401 in _extract_statuses(failure_text):
        return True
    return _text_contains(failure_text, AUTH_ERROR_MARKERS)


def _is_rate_limit_error(text):
    return _text_contains(text, RATE_LIMIT_MARKERS)


def _is_transient_5xx_error(text):
    return _text_contains(text, TRANSIENT_5XX_MARKERS)


def _is_stale_resume_error(text):
    return _text_contains(text, STALE_RESUME_MARKERS)


# Numeric HTTP status as codex surfaces it. codex-cli does NOT put a
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
# forms match. At least one separator is required, so an identifier such as
# `future-status401` or `custom/http401` never names a status; the classifier
# also masks the requested model id before any status scan (_mask_model).
_STATUS_KEYWORD_RE = re.compile(
    r"(?:^|[^0-9a-z_])(?:http|status(?:[\s_-]*code)?)[\s:=\"']+([0-9]{3})(?![0-9])",
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


def _anchored_retriable_class(statuses):
    """Retriable class of anchored HTTP statuses: "rate-limit" (429), "5xx"
    (500-599), or None."""
    if any(s == 429 for s in statuses):
        return "rate-limit"
    if any(500 <= s <= 599 for s in statuses):
        return "5xx"
    return None


def _substring_retriable_class(text):
    """Retriable class from the phrase markers, for text with no anchored
    status (e.g. stderr-only transport errors, or codex's code-less overload
    phrases), or None.

    Text that names any anchored status never reaches the markers: a
    non-retriable status (e.g. 400/403) is authoritative, so a "429" or
    "service unavailable" echoed inside a 400 body cannot force a retry. A
    non-retriable error TYPE ("invalid_request_error", a 4xx body codex can
    surface without a numeric status) suppresses the markers the same way.
    """
    if _extract_statuses(text):
        return None
    if _text_contains(text, NONRETRIABLE_ERROR_TYPE_MARKERS):
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
    return _text_contains(failure_text, QUOTA_MARKERS)


def _names_excluded_param(text):
    """True when text names reasoning effort or service tier as a whole
    token (see _EXCLUDED_PARAM_RE)."""
    return _EXCLUDED_PARAM_RE.search(text) is not None


def _about_a_setting(record):
    """True when a record's structured param is reasoning effort or service
    tier: that record is about the setting, not the model."""
    param = (record.param or "").strip().lower()
    return param in MODEL_REJECTION_EXCLUDED_PARAMS


# Codex passes the provider's rejection sentence through as text. The model
# id is in single quotes in the ChatGPT sign-in form observed live, and in
# backticks in the OpenAI API's model_not_found wording, so either quote is
# accepted around it.
_MODEL_QUOTE = "['`]"


def _model_rejection_patterns(requested_model):
    """Codex's complete rejection sentences for the model this invocation
    sent (regex-escaped), or for any model when none was sent. The model
    may be quoted with single quotes or backticks; its span is the "model"
    group."""
    q = _MODEL_QUOTE
    model = re.escape(requested_model) if requested_model else r"[^'`]+"
    model = f"(?P<model>{model})"
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


def _sentence_rejection(text, patterns):
    """True when text carries one of the rejection `patterns` and, outside
    the quoted model id that sentence names, names neither reasoning effort
    nor service tier. The id is left out because a model id may contain
    those words; elsewhere in unstructured text they are the only
    unambiguous sign that the failure is about that setting."""
    for pattern in patterns:
        match = pattern.search(text)
        if match is None:
            continue
        rest = text[:match.start("model")] + text[match.end("model"):]
        if not _names_excluded_param(rest):
            return True
    return False


def _model_rejection(failure_text, records, requested_model):
    """Codex's own message when it rejected this invocation's model, or None.

    Positive evidence only: a structured `model_not_found` code whose param
    is `model` or absent (status 400, 404, or absent), or one of Codex's
    complete rejection sentences for the model this invocation sent. Bare
    "not found" / "not supported", the "Model metadata for ... not found"
    advisory, and "Selected model is at capacity" (transient) never
    qualify. Each record is judged on its own, so a definitive rejection
    wins whatever order the records came in: a record whose structured
    param is reasoning effort or service tier is about that setting, and
    none of its text counts (nor does a failure-text line repeating its
    message), but it never hides another record. A record with any other
    structured param is judged by that param alone; text with no structured
    param is excluded only when it names one of those settings outside the
    quoted model id (see _sentence_rejection).
    """
    kept = [record for record in records if not _about_a_setting(record)]
    for record in kept:
        if (record.code == "model_not_found"
                and (record.param or "model").strip().lower() == "model"
                and record.status in (None, 400, 404)):
            return record.message or "model_not_found"
    patterns = _model_rejection_patterns(requested_model)
    for record in kept:
        if not record.message:
            continue
        if record.param is not None:
            # A structured param already says what the record is about.
            if any(pattern.search(record.message) for pattern in patterns):
                return record.message
        elif _sentence_rejection(record.message, patterns):
            return record.message
    set_aside = [record.message for record in records
                 if record.message and _about_a_setting(record)]
    for line in failure_text.splitlines():
        line = line.strip()
        if not line or any(line in message for message in set_aside):
            continue
        if _sentence_rejection(line, patterns):
            return line
    return None


@dataclass(frozen=True)
class FailureVerdict:
    """One attempt's failure class (see _failure_verdict).

    `kind` is "auth", "quota", "rate-limit", "5xx", "model-rejected",
    "stale", or None (untagged); `rejection` is Codex's own rejection
    message when kind is "model-rejected", else None.
    """
    kind: str | None
    rejection: str | None = None

    @property
    def retriable(self):
        """Whether the runner may retry this attempt: the one retry
        decision, carried as data and never read back from the formatted
        tag (an untagged failure's text is Codex's and may look like one)."""
        return self.kind in RETRIABLE_KINDS


def _failure_verdict(failure_text, records, requested_model, resume=False):
    """Classify a non-zero codex exit; the stall verdict is decided earlier.

    Precedence, identical on the fresh and resume paths: auth (structured
    401 or authentication code, or auth prose) -> quota -> anchored
    429/5xx -> model rejected -> stale (resume only) -> substring retriable
    fallback -> None (untagged). Returns a FailureVerdict, whose rejection
    message is parsed here, once per attempt, and whose `retriable` is the
    runner's retry decision.
    """
    # Status scans never see the model id: its text is the user's or the
    # catalog's, and an id like `future-status:429` must not name a status.
    scan_text = _mask_model(failure_text, requested_model)
    if _is_auth_error(scan_text, records):
        return FailureVerdict("auth")
    if _is_quota_error(failure_text, records):
        return FailureVerdict("quota")
    # Ahead of the stale check, so a genuine anchored 429/5xx (e.g. "HTTP 429
    # Too Many Requests; thread not found") is retried, while a stale
    # message whose only digits are a thread id ("stale-429-sid") names no
    # status and still restarts fresh.
    anchored = _anchored_retriable_class(_extract_statuses(scan_text))
    if anchored:
        return FailureVerdict(anchored)
    rejection = _model_rejection(failure_text, records, requested_model)
    if rejection is not None:
        return FailureVerdict("model-rejected", rejection)
    if resume and _is_stale_resume_error(failure_text):
        return FailureVerdict("stale")
    return FailureVerdict(_substring_retriable_class(scan_text))


def _mask_model(text, model):
    """Replace every occurrence of the requested model id with a neutral
    placeholder, so the auth and retriable scans (statuses and phrase
    markers) read only the provider's words. The quota, rejection, and stale
    checks read the unmasked text."""
    if not model:
        return text
    return text.replace(model, "<model>")


# ---------- failure tags ----------

# The recovery action for a refused model ([model-rejected], or a [quota]
# usage limit Codex names for one model) depends on WHICH model was sent,
# not on how the role chose it. A routed model or an explicit model pin can
# be dropped. The natively configured model (sent by a native_effort role,
# or inherited, as by an effort-only pin) is what an inheriting re-run
# would send again, so only a configuration change or an explicit pin
# avoids it, and both are the user's decision: the action addresses the
# user, never the orchestrator, which must not edit Codex configuration or
# pick a model on the user's behalf. A routed or pinned model that the
# resolving discovery proved IS the native model counts as the native one:
# dropping it would send the same refused model again. The runner never
# substitutes or replays: the host re-runs the role.
_INHERIT_ACTION = (
    "Re-run this role with model, effort, and selection omitted to inherit "
    "native configuration."
)
_CHANGE_PIN_ACTION = "Change or remove the explicit pin."
_NATIVE_MODEL_ACTION = (
    "Ask the user to update the Codex configuration (model) or to name a "
    "model to pin."
)


def _sent_the_native_model(decision):
    """Whether the model `decision` sent is known to be the native one: an
    override was sent and discovery proved it equals the native model."""
    return (decision.dispatch_model is not None
            and decision.dispatch_model == decision.native_model)


def _refused_model_action(decision):
    """The one recovery action when Codex refused the model `decision` sent
    (a rejection, or that model's usage limit)."""
    if (decision.dispatch_model is None
            or decision.provenance == "native_effort"
            or _sent_the_native_model(decision)):
        return _NATIVE_MODEL_ACTION
    if decision.provenance == "routed":
        return _INHERIT_ACTION
    return _CHANGE_PIN_ACTION


def _model_rejected_error(decision, provider_message, phase):
    """The terminal [model-rejected] text: what was rejected, Codex's own
    message, and one action for the model that was refused."""
    if decision.dispatch_model:
        subject = f"requested model '{decision.dispatch_model}'"
        if _sent_the_native_model(decision):
            # Why the action is the native model's, not a plain re-run.
            subject += ", which is also the natively configured model,"
    else:
        subject = "natively configured model"
    # Only a resume has a saved thread this failure could have touched.
    kept = " and the saved thread was kept" if phase == "resume" else ""
    return (
        f"[model-rejected] Codex rejected the {subject} for this invocation: "
        f"{provider_message.rstrip(' .')}. No substitute model was "
        f"tried{kept}. {_refused_model_action(decision)}"
    )


def _classify_failure(failure_text, rc, phase, decision, verdict):
    """Return a tagged error string for a non-zero codex exit.

    `decision` is the role's SelectionDecision and `verdict` the attempt's
    FailureVerdict, the one the caller already branched on, so the tag
    always matches that branch. A stale resume is never formatted: the
    resume path restarts fresh instead. A [quota] whose text is Codex's
    usage limit for one model ends with the action for the model that was
    sent; any other [quota] keeps Codex's text alone.
    """
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
