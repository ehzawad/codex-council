#!/usr/bin/env python3
"""Fan out a prompt to a council of Codex sub-agents in parallel.

Each role runs in its own `codex exec` subprocess with a distinct
framing instruction. Sessions are isolated per (project, host session,
role) when a terminal/session identifier is available, and persist
across calls from that host session so each role accumulates its own
thread of project knowledge.

Results are aggregated into one structured markdown report on stdout
for Claude to reconcile.

This script is a pure orchestrator — there is no built-in role
catalog and no default council. Callers (Claude as orchestrator)
ultrathink about the user's task, compose the role panel JSON
on-the-fly per invocation, announce it briefly, then invoke this
script without waiting for manual launch approval. Roles arrive via
`--roles-file` (a path to a JSON file holding the panel), which keeps
a large role array out of the shell entirely.

Usage:
    python3 codex_council.py --roles-file roles.json --context-file context.md

Env vars:
    CODEX_COUNCIL_SESSION_KEY     explicit council thread scope override
    CODEX_COUNCIL_DISABLE_AUTO_SESSION_KEY=1
                                   fall back to project-wide role state

No wall-clock timeouts are enforced — each role runs as long as Codex
takes. Ctrl+C still tears down every in-flight codex process group.

POSIX-only: uses start_new_session and process-group signals.
"""

import argparse
import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from functools import cache
from typing import Optional

STATE_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
    "codex-council",
)

# Substring markers (matched case-insensitively) classifying failure modes.
# These are the FALLBACK signal; the primary signal is the numeric HTTP status
# parsed out of the JSONL error body (see _extract_statuses). Order of check on
# the resume path: auth first (never clear state), then ANCHORED-status retriable
# (a real API 429/5xx — by JSON status, `HTTP NNN`, or a reason phrase — beats a
# stale-looking message), then stale-resume (clear and restart), then the
# SUBSTRING retriable fallback — kept last so a stale error that merely contains
# a bare digit run (e.g. "...stale-429-sid") still restarts fresh instead of
# being mistaken for a rate limit.
AUTH_ERROR_MARKERS = (
    "401 unauthorized",
    "incorrect api key",
    "authentication failed",
    "auth: token rejected",
    "please run `codex login`",
    "please run codex login",
)
RATE_LIMIT_MARKERS = (
    # NB: bare "429" is intentionally NOT here — the anchored status parser
    # (_extract_statuses) covers every real 429 form ("status":429, HTTP 429,
    # status 429, last status: 429, 429 Too Many Requests), so a bare digit run
    # like "4291" or "stale-429-sid" is never mistaken for a rate limit.
    "rate limit",
    "rate_limit",
    "too many requests",
    # NB: "quota exceeded" / usage caps are deliberately NOT retriable markers —
    # a plan/usage cap does not clear within a 5s backoff, so it is surfaced
    # terminal (see the Retries note in SKILL.md / DESIGN.md). Genuine transient
    # 429s are caught by the anchored parser or the rate-limit phrases above.
)
TRANSIENT_5XX_MARKERS = (
    "500 internal",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
    "internal server error",
    "service unavailable",
    # codex-cli 0.135.0 friendly-rewrites some upstream 5xx/overload errors to
    # prose that carries no status code (HTTP 500 -> "...experiencing high
    # demand..."; serverOverloaded -> "...backend overloaded..."). These two
    # phrases are version-coupled fallbacks for that code-less case; the numeric
    # range in _structured_retriable_class handles every 5xx that DOES carry a
    # status. "backend overloaded" is intentionally specific (not bare
    # "overloaded") so unrelated text like "operator overloaded" is not matched.
    "backend overloaded",
    "experiencing high demand",
)
STALE_RESUME_MARKERS = (
    "no rollout found",
    "thread not found",
    "session not found",
    "session expired",
    "thread expired",
)

# A definitively non-retriable error TYPE that codex/OpenAI put in the JSONL
# error body for 4xx client errors. codex-cli 0.135.0 sometimes surfaces a 400
# as raw JSON with this type but NO numeric status; its presence (when no
# anchored retriable status is found) suppresses the substring retriable
# fallback, so a 400 whose message text merely contains a 5xx reason phrase or
# "too many requests" is not wrongly retried.
NONRETRIABLE_ERROR_TYPE_MARKERS = (
    "invalid_request_error",
)

SESSION_KEY_ENV = "CODEX_COUNCIL_SESSION_KEY"
DISABLE_AUTO_SESSION_KEY_ENV = "CODEX_COUNCIL_DISABLE_AUTO_SESSION_KEY"
AUTO_SESSION_ENV_VARS = (
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_SESSION_ID",
    "CODEX_THREAD_ID",
    "TERM_SESSION_ID",
    "TMUX_PANE",
    "STY",
    "VSCODE_PID",
)

MAX_PARALLEL = 6  # matches codex's own DEFAULT_AGENT_MAX_THREADS
MAX_RETRY_ATTEMPTS = 2
INITIAL_BACKOFF_SECS = 5
TERMINATION_GRACE_SECS = 0.2
MAX_STDIN_BYTES = 10 << 20  # 10 MiB (a sanity guard, not a model-context
# limit — Codex's own context window is the real ceiling; compress anyway).
MAX_PROMPT_BYTES = MAX_STDIN_BYTES
ROLE_LABEL_MAX_BYTES = 80
ROLE_INSTRUCTION_MAX_BYTES = 8192
REQUIRED_SCOPE_PHRASE = "nothing material"
REQUIRED_CADENCE_SENTENCE = "Thoroughness beats speed."
LINEBREAK_CHARS = ("\r", "\n", "\u2028", "\u2029")
STAGING_PATH_HINT = (
    "Staging hint: use the exact directory printed by `mktemp -d` for "
    "both roles.json and context.md in this invocation; keep roles, context, "
    "out.md, and err.log under the same mktemp directory. Shell variables do "
    "not persist across Claude Code Bash calls."
)
# Action-first recovery text for a rejected staging DIRECTORY. The orchestrator
# is an LLM; the cheapest literal reading of "create it with mktemp -d" was
# observed (GH issue #1) to be satisfied by mkdir/chmod on the same predictable
# path, so the recovery must forbid exactly those moves and demand a NEW path.
STAGING_DIR_RECOVERY = (
    "Recovery: abandon this directory — do not chmod it, do not mkdir it, "
    "and do not reuse its name. Run `mktemp -d` again, copy the NEW printed "
    "absolute path, re-Write BOTH roles.json and context.md into that new "
    "directory, and re-run --check-staging-dir on it."
)

# No timeout, by design. Neither this script nor `codex exec` enforces a
# wall-clock or run-level timeout, so a role may think for hours or days.
# codex's only default that could end a long-QUIET run is the
# per-PROVIDER stream-idle timeout (`model_providers.<id>.stream_idle_timeout_ms`,
# 5 min, then bounded retries), which an actively-streaming role never
# trips. Widening that for long stalls is left to the user's
# ~/.codex/config.toml rather than overridden here: it is provider-scoped
# and the active provider id varies, so the council cannot target it
# portably. (Verified against codex-cli 0.135.0.)


@dataclass(frozen=True)
class Role:
    id: str
    label: str
    instruction: str


@dataclass
class RoleResult:
    role: Role
    ok: bool
    text: Optional[str] = None
    error: Optional[str] = None
    thread_id: Optional[str] = None
    elapsed_seconds: float = 0.0
    attempts: int = 1
    warning: Optional[str] = None


ROLE_ID_PATTERN = re.compile(r"^[a-z0-9_-]+$")
ROLE_ID_MAX_LEN = 32


# ---------- project / session state (sync) ----------

@cache
def _project_root():
    """Return the git repo root for the current dir, falling back to cwd.

    Cached because _project_key is called once per role; without the
    cache, git rev-parse runs N+ times per invocation.
    """
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True,
        ).stdout.strip()
    except OSError:
        root = ""
    return root or os.getcwd()


def _auto_session_key():
    """Best-effort stable host-session key for terminal/tab/pane isolation."""
    for name in AUTO_SESSION_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return f"{name}={value}"
    return ""


def _truthy_env(name):
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _session_key():
    """Return explicit or auto-detected key for scoping council threads."""
    explicit = os.environ.get(SESSION_KEY_ENV, "").strip()
    if explicit:
        return explicit
    if _truthy_env(DISABLE_AUTO_SESSION_KEY_ENV):
        return ""
    return _auto_session_key()


def _project_key(role_id):
    """Stable state key for (project, role, optional session-key)."""
    base = hashlib.sha256(_project_root().encode()).hexdigest()[:16]
    session_key = _session_key()
    if session_key:
        suffix = hashlib.sha256(session_key.encode()).hexdigest()[:16]
        return f"{base}-{suffix}__{role_id}"
    return f"{base}__{role_id}"


def _state_path(role_id):
    """Per-(project, role) state file path."""
    return os.path.join(STATE_DIR, f"{_project_key(role_id)}.json")


def _state_lock_path(role_id):
    """Per-state-file lock path used across council processes."""
    return _state_path(role_id) + ".lock"


async def _acquire_role_state_lock(role_id):
    """Acquire an exclusive cross-process lock for one role's state."""
    os.makedirs(STATE_DIR, exist_ok=True)
    lock_path = _state_lock_path(role_id)
    lock_file = open(lock_path, "a+")
    try:
        while True:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return lock_file
            except BlockingIOError:
                await asyncio.sleep(0.1)
    except BaseException:
        lock_file.close()
        raise


def _release_role_state_lock(lock_file):
    """Release a lock returned by _acquire_role_state_lock."""
    try:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
    finally:
        lock_file.close()


def load_session(role_id):
    """Return (session_id, meta) for this role's stored thread, or (None, None)."""
    try:
        with open(_state_path(role_id)) as f:
            meta = json.load(f)
            sid = meta.get("session_id")
            if isinstance(sid, str) and sid:
                return sid, meta
    except (OSError, json.JSONDecodeError):
        pass
    return None, None


def save_session(role_id, session_id):
    """Persist session metadata atomically (unique tempfile + os.replace)."""
    os.makedirs(STATE_DIR, exist_ok=True)
    meta = {
        "session_id": session_id,
        "role_id": role_id,
        "project_path": _project_root(),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    session_key = _session_key()
    if session_key:
        meta["session_key"] = session_key
    path = _state_path(role_id)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp.", dir=STATE_DIR)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(meta, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def clear_session(role_id):
    """Remove this role's stored thread state."""
    try:
        os.remove(_state_path(role_id))
    except OSError:
        pass


# ---------- JSONL parsing ----------

def _iter_json_objects(jsonl_output):
    """Yield JSON object lines from a JSONL stream, skipping malformed lines."""
    for line in jsonl_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def extract_session_id(jsonl_output):
    """Pull thread_id from the first thread.started event in the stream."""
    for event in _iter_json_objects(jsonl_output):
        if event.get("type") == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                return thread_id
    return None


def extract_final_message(jsonl_output):
    """Pull the last agent_message text from item.completed events."""
    last_message = None
    for event in _iter_json_objects(jsonl_output):
        if event.get("type") == "item.completed":
            item = event.get("item", {})
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    last_message = text
    return last_message


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


def _dedupe_preserve_order(items):
    seen = set()
    out = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _failure_text(stdout, stderr):
    """Combine stderr and structured stdout error events for classification."""
    parts = []
    stderr_stripped = stderr.strip()
    if stderr_stripped:
        parts.append(stderr_stripped)
    parts.extend(extract_error_messages(stdout))
    return "\n".join(parts)


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


def _is_retriable_error(stderr_text):
    return _is_rate_limit_error(stderr_text) or _is_transient_5xx_error(stderr_text)


def _is_stale_resume_error(stderr_text):
    return _stderr_contains(stderr_text, STALE_RESUME_MARKERS)


# Numeric HTTP status as codex surfaces it. codex-cli 0.135.0 does NOT put a
# status on the top-level JSONL event, so we scan the combined failure text for
# an ANCHORED status — one in a recognizable status context, so a bare digit run
# (e.g. "429" inside a thread id like "stale-429-sid") is never mistaken for one.
# Two anchors are accepted:
#   * keyword-prefixed: `"status": 429`, `status 529`, `status code 429`,
#     `HTTP 429`, `last status: 429` (the JSON key and the prose forms);
#   * reason-phrase-suffixed: `429 Too Many Requests`, `503 Service Unavailable`,
#     `502 Bad Gateway`, `504 Gateway Timeout`, `500 Internal Server Error`,
#     `529 <unknown status code>` (codex's "unexpected status N" form).
# Anchored detection is the PRIMARY retriable signal and (unlike a bare
# substring) is trusted ahead of the stale check on the resume path.
# The separator class excludes "/" so a URL like `http://127.0.0.1:8080/...`
# is NOT read as "http" + status 127; only real `HTTP 429` / `status: 429`
# forms match.
_STATUS_KEYWORD_RE = re.compile(
    r"(?:^|[^0-9a-z_])(?:http|status)(?:\s+code)?[\s:=\"']*([0-9]{3})(?![0-9])",
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


# ---------- prompt composition ----------

def _compose_prompt(role, body):
    """Bookend the body with the role's framing instruction (both ends)."""
    return f"{role.instruction}\n\n{body}\n\n{role.instruction}"


def _validate_prompt_size(role, body):
    prompt_bytes = len(_compose_prompt(role, body).encode("utf-8"))
    if prompt_bytes > MAX_PROMPT_BYTES:
        print(
            f"Composed prompt for role {role.id!r} is {prompt_bytes} bytes; "
            f"limit is {MAX_PROMPT_BYTES}. Trim stdin or role instructions.",
            file=sys.stderr,
        )
        sys.exit(1)


# ---------- async codex invocation ----------

def _resume_cmd(root, session_id):
    # `-C` is a parent option of `codex exec` and must precede `resume`.
    return [
        "codex", "exec", "-C", root, "resume", session_id,
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "--json", "-",
    ]


def _fresh_cmd(root):
    return [
        "codex", "exec", "-C", root,
        "--dangerously-bypass-approvals-and-sandbox",
        "--json", "--skip-git-repo-check", "-",
    ]


async def _run_codex_subprocess(cmd, prompt):
    """Run codex exec async; on cancellation, kill the whole process group.

    start_new_session=True puts codex in its own process group so a
    SIGTERM to the group also reaches any shell commands codex itself
    spawned for tool calls. Without it, those grandchildren leak.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        # start_new_session=True makes the child process leader's pid the pgid.
        pgid = proc.pid
    try:
        stdout_b, stderr_b = await proc.communicate(prompt.encode("utf-8"))
    except asyncio.CancelledError:
        await _terminate_process_group(proc, pgid)
        raise
    return (
        proc.returncode,
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
    )


async def _terminate_process_group(proc, pgid=None):
    """Best-effort SIGTERM then SIGKILL to the codex process group."""
    def _signal_group(sig):
        if pgid is not None:
            try:
                os.killpg(pgid, sig)
                return
            except (ProcessLookupError, PermissionError, OSError):
                pass
        if proc.returncode is None:
            try:
                if sig == signal.SIGTERM:
                    proc.terminate()
                else:
                    proc.kill()
            except (ProcessLookupError, OSError):
                pass

    _signal_group(signal.SIGTERM)
    try:
        await asyncio.sleep(TERMINATION_GRACE_SECS)
    finally:
        _signal_group(signal.SIGKILL)
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError, OSError):
            await proc.wait()


def _format_clean_exit_no_message(stderr_stripped):
    base = "Codex exited cleanly but produced no agent_message."
    return f"{base}\n{stderr_stripped}" if stderr_stripped else base


def _classify_failure(stderr_stripped, rc, phase):
    """Return a tagged error string for a non-zero codex exit."""
    if _is_auth_error(stderr_stripped):
        return f"[auth] {stderr_stripped or f'codex {phase} exited {rc}'}"
    cls = _retriable_class(stderr_stripped)
    if cls == "rate-limit":
        return f"[retriable:rate-limit] {stderr_stripped or f'codex {phase} exited {rc}'}"
    if cls == "5xx":
        return f"[retriable:5xx] {stderr_stripped or f'codex {phase} exited {rc}'}"
    return stderr_stripped or f"codex {phase} exited {rc}"


async def _run_role_once(role, prompt, attempt):
    """One codex invocation for one role. No retry logic here."""
    started = time.monotonic()
    root = _project_root()
    session_id, meta = load_session(role.id)
    warning = None

    if session_id:
        rc, stdout, stderr = await _run_codex_subprocess(
            _resume_cmd(root, session_id), prompt
        )
        failure_text = _failure_text(stdout, stderr)

        if rc == 0:
            # Resume-footgun mitigation. `codex exec resume <id>` parses <id>
            # as a UUID first (UUIDs take precedence if it parses). On codex-cli
            # 0.135.0 a valid-but-unknown UUID ERRORS ("no rollout found ...
            # -32600", exit 1) and is handled by the stale-resume branch below;
            # only a value that is NOT a valid UUID is treated as a thread NAME
            # and silently starts a NEW thread (rc==0, fresh thread.started).
            # Stored ids are always real UUIDs, so silent-spawn is unreachable
            # via normal state — this check is defense-in-depth (corrupt/manual
            # state, or future CLI drift). Detect by comparing the emitted
            # thread.started.thread_id to what we asked to resume; if mismatched,
            # adopt the new id (no benefit re-running an already-completed turn)
            # and warn — the role lost its prior accumulated framing.
            emitted_id = extract_session_id(stdout)
            if emitted_id and emitted_id != session_id:
                warning = (
                    f"resume returned thread_id {emitted_id} != stored "
                    f"{session_id}; adopted new id (prior continuity lost)"
                )
                print(
                    f"[codex-council:{role.id}] {warning}",
                    file=sys.stderr,
                )
                session_id = emitted_id
            msg = extract_final_message(stdout)
            elapsed = time.monotonic() - started
            if msg:
                save_session(role.id, session_id)
                return RoleResult(
                    role=role, ok=True, text=msg, thread_id=session_id,
                    elapsed_seconds=elapsed, attempts=attempt, warning=warning,
                )
            return RoleResult(
                role=role, ok=False,
                error=_format_clean_exit_no_message(failure_text),
                thread_id=session_id, elapsed_seconds=elapsed, attempts=attempt,
                warning=warning,
            )

        # rc != 0 on resume. Order: auth (never clear state) -> ANCHORED-status
        # retriable (a real API 429/5xx, by JSON status / `HTTP NNN` / reason
        # phrase, beats a stale-looking message) -> stale-resume (clear + restart
        # fresh) -> SUBSTRING retriable fallback (inside _classify_failure). The
        # substring fallback sits after the stale check so a stale error that
        # merely contains a bare digit run (e.g. "...thread id stale-429-sid")
        # still restarts fresh.
        if _is_auth_error(failure_text):
            err = _classify_failure(failure_text, rc, "resume")
            return RoleResult(
                role=role, ok=False, error=err,
                elapsed_seconds=time.monotonic() - started, attempts=attempt,
            )
        if _structured_retriable_class(failure_text):
            err = _classify_failure(failure_text, rc, "resume")
            return RoleResult(
                role=role, ok=False, error=err,
                elapsed_seconds=time.monotonic() - started, attempts=attempt,
            )
        if not _is_stale_resume_error(failure_text):
            err = _classify_failure(failure_text, rc, "resume")
            return RoleResult(
                role=role, ok=False, error=err,
                elapsed_seconds=time.monotonic() - started, attempts=attempt,
            )

        # Stale: log, clear, fall through to fresh.
        updated = (meta or {}).get("updated_at", "unknown")
        print(
            f"[codex-council:{role.id}] session {session_id} (last used {updated}) "
            f"is stale ({failure_text}) — starting fresh.",
            file=sys.stderr,
        )
        current_id, _ = load_session(role.id)
        if current_id == session_id:
            clear_session(role.id)

    # Fresh path.
    rc, stdout, stderr = await _run_codex_subprocess(_fresh_cmd(root), prompt)
    failure_text = _failure_text(stdout, stderr)

    if rc != 0:
        return RoleResult(
            role=role, ok=False,
            error=_classify_failure(failure_text, rc, "exec"),
            elapsed_seconds=time.monotonic() - started, attempts=attempt,
        )

    msg = extract_final_message(stdout)
    new_id = extract_session_id(stdout)
    elapsed = time.monotonic() - started
    if msg:
        # Persist only when both halves of session continuity are
        # present; an agent_message without a thread.started is still a
        # valid reply but cannot be resumed, so skip the save — don't
        # persist a thread that produced no agent_message, but don't
        # drop a reply either.
        if new_id:
            save_session(role.id, new_id)
        return RoleResult(
            role=role, ok=True, text=msg, thread_id=new_id,
            elapsed_seconds=elapsed, attempts=attempt,
        )
    return RoleResult(
        role=role, ok=False,
        error=_format_clean_exit_no_message(failure_text),
        thread_id=new_id, elapsed_seconds=elapsed, attempts=attempt,
    )


async def _run_role(role, prompt):
    """One role with retry on rate-limit/5xx. No wall-clock deadline.

    Each attempt runs to completion; Codex decides when it is done.
    Ctrl+C propagates as asyncio.CancelledError, which tears down the
    codex process group via _run_codex_subprocess's cancellation path.
    """
    lock_file = await _acquire_role_state_lock(role.id)
    try:
        last_result = None
        backoff = INITIAL_BACKOFF_SECS

        for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
            result = await _run_role_once(role, prompt, attempt)
            last_result = result
            if result.ok or not (result.error or "").startswith("[retriable:"):
                return result
            if attempt >= MAX_RETRY_ATTEMPTS:
                return result
            print(
                f"[codex-council:{role.id}] retriable error on attempt "
                f"{attempt}/{MAX_RETRY_ATTEMPTS}; sleeping {backoff}s.",
                file=sys.stderr,
            )
            await asyncio.sleep(backoff)
            backoff *= 2

        return last_result  # type: ignore[return-value]
    finally:
        _release_role_state_lock(lock_file)


async def run_council(roles, body):
    """Fan out N roles in parallel and wait for all to finish.

    `roles` is a list of Role objects supplied by the caller via
    --roles-file. There is no built-in role registry.

    `return_exceptions=True` ensures one role's crash does not cancel
    its siblings — every role gets its turn and its result in the report.
    No wall-clock timeout: Codex decides when each role is done.

    Per-role completion progress is emitted to stderr (in completion
    order) as each role settles; stdout stays the report.
    """
    total = len(roles)
    counter = {"done": 0}

    def _on_role_done(role, task):
        # Fires on the single event loop thread as each role settles, in
        # COMPLETION order. No await between increment and print, so the
        # counter is race-free. stdout stays the report; progress is stderr.
        counter["done"] += 1
        n = counter["done"]
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            print(
                f"[codex-council] {n}/{total} {role.id}: crashed ({type(exc).__name__})",
                file=sys.stderr, flush=True,
            )
            return
        res = task.result()
        if isinstance(res, RoleResult):
            status = "ok" if res.ok else "FAILED"
            print(
                f"[codex-council] {n}/{total} {role.id}: {status} "
                f"({res.elapsed_seconds:.1f}s)",
                file=sys.stderr, flush=True,
            )
        else:
            print(
                f"[codex-council] {n}/{total} {role.id}: done",
                file=sys.stderr, flush=True,
            )

    tasks = []
    for role in roles:
        _validate_prompt_size(role, body)
        t = asyncio.create_task(_run_role(role, _compose_prompt(role, body)))
        t.add_done_callback(lambda task, role=role: _on_role_done(role, task))
        tasks.append(t)
    started = time.monotonic()
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    elapsed = time.monotonic() - started

    out = []
    for role, r in zip(roles, results):
        if isinstance(r, RoleResult):
            out.append(r)
        elif isinstance(r, BaseException):
            out.append(RoleResult(
                role=role, ok=False,
                error=f"[orchestrator-exception] {type(r).__name__}: {r}",
                elapsed_seconds=elapsed, attempts=1,
            ))
        else:
            out.append(RoleResult(
                role=role, ok=False,
                error=f"[orchestrator-bug] unexpected result {type(r).__name__}",
                elapsed_seconds=elapsed, attempts=1,
            ))
    return out


# ---------- report ----------

def _format_report(results, total_elapsed):
    """Render results as a single markdown report for Claude to reconcile."""
    ok = [r for r in results if r.ok]
    n = len(results)

    lines = []
    lines.append(
        f"# Codex Council — {len(ok)}/{n} roles responded ({total_elapsed:.1f}s)"
    )
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    for r in results:
        status = "ok" if r.ok else "FAILED"
        attempts = f" (attempts: {r.attempts})" if r.attempts > 1 else ""
        warn_note = " — WARNING" if r.warning else ""
        label = _report_inline(r.role.label)
        lines.append(
            f"- **{label}** [{r.role.id}]: {status}{attempts}{warn_note} — "
            f"{r.elapsed_seconds:.1f}s"
        )
    lines.append("")

    for r in results:
        label = _report_inline(r.role.label)
        lines.append(f"## {label} ({r.role.id})")
        lines.append("")
        if r.warning:
            lines.append(f"_Warning: {_report_inline(r.warning)}_")
            lines.append("")
        if r.ok:
            lines.append((r.text or "").rstrip())
        else:
            lines.append(f"_Failed: {_report_inline(r.error)}_")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _report_inline(value):
    """Keep report metadata on one Markdown line."""
    return str(value).replace("\r", "\\r").replace("\n", "\\n")


# ---------- CLI / entry point ----------

def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description=(
            "Fan out a prompt to a council of Codex agents in parallel. "
            "Roles are caller-supplied per invocation via --roles-file; "
            "there is no built-in catalog."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--roles-file", default=None, metavar="PATH",
        help=(
            "Path to a JSON file holding the role panel: a list of "
            "[{\"id\":..,\"label\":..,\"instruction\":..}] objects. Keeping "
            "the panel in a file (not an inline argument) means a large "
            "role array never has to survive shell quoting, where a stray "
            "quote or brace would break the call. Claude (the orchestrator) "
            "composes this per invocation; see SKILL.md."
        ),
    )
    parser.add_argument(
        "--check-staging-dir", default=None, metavar="DIR",
        help=(
            "Validate DIR/roles.json and DIR/context.md, then exit without "
            "launching Codex. Use this after writing the per-run staging "
            "files and before the background council launch."
        ),
    )
    parser.add_argument(
        "--context-file", default=None, metavar="PATH",
        help=(
            "Path to a UTF-8 context file to send to every role instead of "
            "reading stdin. This lets the script validate both staged inputs "
            "before launching any Codex subprocess."
        ),
    )
    args = parser.parse_args(argv)
    if args.roles_file == "":
        parser.error("--roles-file must be non-empty")
    if args.check_staging_dir == "":
        parser.error("--check-staging-dir must be non-empty")
    if args.context_file == "":
        parser.error("--context-file must be non-empty")
    if args.check_staging_dir is not None and args.roles_file is not None:
        parser.error("--check-staging-dir cannot be combined with --roles-file")
    if args.check_staging_dir is not None and args.context_file is not None:
        parser.error("--check-staging-dir cannot be combined with --context-file")
    return args


def _usage_exit(msg):
    """Exit 2 with msg on stderr (argparse-compatible usage-error code)."""
    print(msg, file=sys.stderr)
    raise SystemExit(2)


def _file_arg_problem(arg_name, path):
    """Return a staging diagnostic for an unreadable file arg, or None."""
    if path == "":
        return f"{arg_name} must be non-empty"
    abs_path = os.path.abspath(path)
    cwd = os.getcwd()
    parent = os.path.dirname(abs_path) or "."
    details = f"cwd={cwd!r}; absolute={abs_path!r}"
    if not os.path.isdir(parent):
        return (
            f"{arg_name}: cannot read {path!r}; parent directory does not "
            f"exist: {parent!r} ({details})"
        )
    if os.path.isdir(path):
        return f"{arg_name}: cannot read {path!r}; path is a directory ({details})"
    if not os.path.exists(path):
        return f"{arg_name}: cannot read {path!r}; file does not exist ({details})"
    if not os.path.isfile(path):
        return f"{arg_name}: cannot read {path!r}; not a regular file ({details})"
    if not os.access(path, os.R_OK):
        return f"{arg_name}: cannot read {path!r}; permission denied ({details})"
    return None


def _usage_exit_if_file_arg_problems(*arg_pairs):
    """Aggregate missing staged-input errors before attempting reads."""
    problems = [
        problem
        for arg_name, path in arg_pairs
        if path is not None
        for problem in [_file_arg_problem(arg_name, path)]
        if problem is not None
    ]
    if problems:
        _usage_exit(
            "codex-council input staging error:\n"
            + "\n".join(f"- {p}" for p in problems)
            + f"\n{STAGING_PATH_HINT}"
        )


def _usage_exit_if_staging_dirs_differ(roles_file, context_file):
    """Require staged launch inputs to live in the same per-run directory."""
    if roles_file is None or context_file is None:
        return
    roles_dir = os.path.realpath(os.path.dirname(os.path.abspath(roles_file)))
    context_dir = os.path.realpath(os.path.dirname(os.path.abspath(context_file)))
    if roles_dir != context_dir:
        _usage_exit(
            "codex-council input staging error:\n"
            f"- --roles-file and --context-file must be in the same mktemp "
            f"directory; got roles dir {roles_dir!r} and context dir "
            f"{context_dir!r}.\n"
            f"{STAGING_PATH_HINT}"
        )


def _read_roles_file(path):
    """Read the raw roles JSON from a file.

    Passing the panel as a path lets the caller write the JSON with a real
    editor/tool instead of escaping a large blob through the shell, where a
    stray quote or unbalanced brace would break the call. Read and decode
    errors exit 2 like other usage errors; JSON validity is left to
    _parse_roles_json.
    """
    problem = _file_arg_problem("--roles-file", path)
    if problem:
        _usage_exit(f"{problem}. {STAGING_PATH_HINT}")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        _usage_exit(f"--roles-file: cannot read {path!r} ({e}). {STAGING_PATH_HINT}")
    except UnicodeDecodeError as e:
        _usage_exit(f"--roles-file: {path!r} is not valid UTF-8 ({e}).")


def _validate_role_id(rid, ctx):
    """Reject malformed/oversize role IDs with a SystemExit citing context."""
    if not isinstance(rid, str) or not rid:
        _usage_exit(f"--roles-file {ctx}: 'id' must be a non-empty string.")
    if len(rid) > ROLE_ID_MAX_LEN:
        _usage_exit(
            f"--roles-file {ctx}: id {rid!r} exceeds {ROLE_ID_MAX_LEN} chars."
        )
    if not ROLE_ID_PATTERN.match(rid):
        _usage_exit(
            f"--roles-file {ctx}: id {rid!r} must match {ROLE_ID_PATTERN.pattern}."
        )


def _validate_role_label(label, ctx):
    """Reject labels that can break report structure or become unreadable."""
    if any(ch in label for ch in LINEBREAK_CHARS):
        _usage_exit(f"--roles-file {ctx}: label must not contain newlines.")
    encoded_len = len(label.encode("utf-8"))
    if encoded_len > ROLE_LABEL_MAX_BYTES:
        _usage_exit(
            f"--roles-file {ctx}: label exceeds {ROLE_LABEL_MAX_BYTES} UTF-8 bytes."
        )


ROLE_FIELDS = ("id", "label", "instruction")


def _normalize_instruction_list(value, ctx):
    """Join a list-form instruction into one whitespace-normalized paragraph.

    The list form exists because the only production writer of roles.json
    is an LLM using a file-Write tool: multi-kilobyte single-line JSON
    string literals are exactly where its writes corrupt (GH issue #2).
    Sentence-sized items on separate physical lines remove that failure
    surface; the script reassembles the paragraph Codex actually sees.
    """
    if not value:
        _usage_exit(
            f"--roles-file {ctx}: instruction list must not be empty. "
            "Recovery: rewrite the whole roles.json file with one sentence "
            "per list item, then re-run --check-staging-dir."
        )
    items = []
    for i, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            _usage_exit(
                f"--roles-file {ctx}: instruction list item {i} must be a "
                "non-empty string. Recovery: rewrite the whole roles.json "
                "file with one sentence per list item, then re-run "
                "--check-staging-dir."
            )
        # split() collapses every Unicode whitespace run, including all
        # LINEBREAK_CHARS, so the joined paragraph is single-line by
        # construction and the string-form validation below applies as-is.
        items.append(" ".join(item.split()))
    return " ".join(items)


def _validate_role_instruction(instruction, ctx):
    """Reject instructions that violate the documented role contract."""
    if any(ch in instruction for ch in LINEBREAK_CHARS):
        _usage_exit(
            f"--roles-file {ctx}: instruction must be a single paragraph "
            "with no newlines. Prefer the list form — one sentence per "
            "JSON list item — and rewrite the whole roles.json file."
        )
    encoded_len = len(instruction.encode("utf-8"))
    if encoded_len > ROLE_INSTRUCTION_MAX_BYTES:
        _usage_exit(
            f"--roles-file {ctx}: instruction exceeds "
            f"{ROLE_INSTRUCTION_MAX_BYTES} bytes."
        )
    lowered = instruction.lower()
    if REQUIRED_SCOPE_PHRASE not in lowered:
        _usage_exit(
            f"--roles-file {ctx}: instruction must include "
            f"{REQUIRED_SCOPE_PHRASE!r}."
        )
    if not instruction.rstrip().endswith(REQUIRED_CADENCE_SENTENCE):
        _usage_exit(
            f"--roles-file {ctx}: instruction must end with "
            f"{REQUIRED_CADENCE_SENTENCE!r}."
        )


def _parse_roles_json(raw):
    """Parse the --roles-file blob into a list of Role objects.

    Validates each entry has exactly the id/label/instruction fields
    (instruction may be a string or a list of strings, normalized to one
    paragraph), id is well-formed, instructions follow the documented
    contract, and ids are unique within the JSON. Unknown keys are
    rejected, not ignored: stray filler fields like '"_": ""' are the
    signature of a corrupted LLM write (GH issue #2), so surfacing them
    forces a clean rewrite instead of silently launching from a file
    that already glitched once.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        _usage_exit(f"--roles-file: invalid JSON ({e}).")
    if not isinstance(data, list):
        _usage_exit("--roles-file: top-level value must be a JSON list.")
    roles = []
    seen = set()
    for idx, entry in enumerate(data):
        ctx = f"entry {idx}"
        if not isinstance(entry, dict):
            _usage_exit(f"--roles-file {ctx}: each entry must be an object.")
        unknown = sorted(set(entry) - set(ROLE_FIELDS))
        if unknown:
            _usage_exit(
                f"--roles-file {ctx}: unknown field(s) "
                f"{', '.join(repr(k) for k in unknown)}. Each role object "
                "must have exactly 'id', 'label', and 'instruction' — no "
                "filler keys. Rewrite the whole roles.json file (do not "
                "patch a substring), then re-run --check-staging-dir."
            )
        for field in ROLE_FIELDS:
            if field not in entry:
                _usage_exit(f"--roles-file {ctx}: missing field {field!r}.")
            value = entry[field]
            if field == "instruction":
                if isinstance(value, list):
                    continue
                if not isinstance(value, str) or not value.strip():
                    _usage_exit(
                        f"--roles-file {ctx}: field 'instruction' must be a "
                        "JSON array of non-empty strings, one sentence per "
                        "item (a legacy single-string paragraph is accepted "
                        "only for old callers). Recovery: rewrite the whole "
                        "roles.json file using the array form, then re-run "
                        "--check-staging-dir."
                    )
                continue
            if not isinstance(value, str) or not value.strip():
                _usage_exit(
                    f"--roles-file {ctx}: field {field!r} must be a non-empty string."
                )
        rid = entry["id"]
        label = entry["label"]
        instruction = entry["instruction"]
        if isinstance(instruction, list):
            instruction = _normalize_instruction_list(instruction, ctx)
        _validate_role_id(rid, ctx)
        _validate_role_label(label, ctx)
        _validate_role_instruction(instruction, ctx)
        if rid in seen:
            _usage_exit(
                f"--roles-file {ctx}: duplicate id {rid!r} within JSON payload."
            )
        seen.add(rid)
        roles.append(Role(rid, label, instruction))
    return roles


def _resolve_roles(custom_roles):
    """Validate and return the list of caller-supplied Role objects.

    Errors if empty or > MAX_PARALLEL. Deduplicates by id preserving
    first occurrence — defense in depth; `_parse_roles_json` already
    rejects duplicate ids within a single JSON payload.
    """
    if not custom_roles:
        _usage_exit(
            "No roles requested. Pass --roles-file with the role panel "
            "(Claude composes this per invocation; see SKILL.md)."
        )

    ordered = []
    seen = set()
    for role in custom_roles:
        if role.id in seen:
            continue
        ordered.append(role)
        seen.add(role.id)

    if len(ordered) > MAX_PARALLEL:
        _usage_exit(
            f"Too many roles: {len(ordered)} > MAX_PARALLEL={MAX_PARALLEL}."
        )
    return ordered


def _read_prompt_body(stream, source_label):
    """Read a prompt body from a binary stream, enforcing the byte cap.

    Counts BYTES, not characters: stdin is read raw and the cap is checked
    against the byte length, because the prompt is later UTF-8 encoded for
    codex and a multibyte-heavy body would otherwise slip past a
    character-count check (the cap claims bytes). Exits 1 if over the cap
    or empty; input is never truncated. Decodes strictly as UTF-8 — a
    prompt should be text, so invalid bytes are a clear error rather than
    silently replaced.
    """
    raw = stream.read(MAX_STDIN_BYTES + 1)
    if len(raw) > MAX_STDIN_BYTES:
        print(
            f"{source_label} exceeds {MAX_STDIN_BYTES} bytes "
            f"({MAX_STDIN_BYTES >> 20} MiB) — trim before piping.",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        body = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        print(f"{source_label} is not valid UTF-8 ({e}) — pipe text.", file=sys.stderr)
        sys.exit(1)
    if not body.strip():
        if source_label == "Input":
            msg = "Empty input — pipe a complete prompt instead."
        else:
            msg = f"Empty {source_label} — write complete context first."
        print(msg, file=sys.stderr)
        sys.exit(1)
    return body


def _read_stdin_body(stream):
    """Read the prompt body from stdin."""
    return _read_prompt_body(stream, "Input")


def _read_context_file(path):
    """Read a staged context file, preserving stdin body validation."""
    problem = _file_arg_problem("--context-file", path)
    if problem:
        _usage_exit(f"{problem}. {STAGING_PATH_HINT}")
    try:
        with open(path, "rb") as f:
            return _read_prompt_body(f, f"Context file {path!r}")
    except OSError as e:
        _usage_exit(f"--context-file: cannot read {path!r} ({e}). {STAGING_PATH_HINT}")


def _check_private_dir(path):
    """Usage-error unless path is a user-owned, non-symlink, private dir.

    Returns the normalized path the checks were performed on; callers
    must use it for any subsequent joins so the validated path and the
    used path cannot diverge.

    lstat (not stat) so a symlink final component is rejected instead of
    silently followed, and ownership is checked so a foreign-owned dir
    that happens to be mode 0700 does not pass. Every rejection carries
    the action-first recovery text: the caller is an LLM, and the one
    correct move is always a NEW `mktemp -d` directory — never chmod,
    mkdir, or reuse of the rejected path.
    """
    # normpath strips trailing slashes first: lstat("link/") follows the
    # final symlink (the slash demands a directory target), so an
    # un-normalized path would let a symlink pass the check below.
    path = os.path.normpath(path)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        _usage_exit(
            f"--check-staging-dir: {path!r} does not exist. "
            f"{STAGING_DIR_RECOVERY}"
        )
    except OSError as e:
        _usage_exit(
            f"--check-staging-dir: cannot inspect {path!r} "
            f"({e.strerror or e}). {STAGING_DIR_RECOVERY}"
        )
    if stat.S_ISLNK(st.st_mode):
        _usage_exit(
            f"--check-staging-dir: {path!r} is a symlink, not the directory "
            f"printed by `mktemp -d`. {STAGING_DIR_RECOVERY}"
        )
    if not stat.S_ISDIR(st.st_mode):
        _usage_exit(
            f"--check-staging-dir: {path!r} is not a directory. "
            f"{STAGING_DIR_RECOVERY}"
        )
    if st.st_uid != os.geteuid():
        _usage_exit(
            f"--check-staging-dir: {path!r} is owned by uid {st.st_uid}, not "
            f"the invoking user (uid {os.geteuid()}). {STAGING_DIR_RECOVERY}"
        )
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        _usage_exit(
            f"--check-staging-dir: {path!r} is mode {mode:04o}, not private "
            f"0700 — not the private mode `mktemp -d` produces. Files "
            f"already written here may have been readable by other local "
            f"users. {STAGING_DIR_RECOVERY}"
        )
    return path


def _check_staging_dir(path):
    """Validate the per-run staging dir before launching Codex."""
    if path == "":
        _usage_exit("--check-staging-dir must be non-empty.")
    path = _check_private_dir(path)
    roles_path = os.path.join(path, "roles.json")
    context_path = os.path.join(path, "context.md")
    _usage_exit_if_file_arg_problems(
        ("--roles-file", roles_path),
        ("--context-file", context_path),
    )
    roles = _resolve_roles(_parse_roles_json(_read_roles_file(roles_path)))
    _read_context_file(context_path)
    print(
        f"[codex-council] staging OK: {os.path.abspath(path)} "
        f"({len(roles)} roles)"
    )


async def _run_council_with_signals(roles, body):
    """Run the council and translate POSIX termination signals into cleanup."""
    loop = asyncio.get_running_loop()
    council_task = asyncio.create_task(run_council(roles, body))
    interrupted = {"signum": None}
    registered = []

    def _cancel_for_signal(signum):
        if interrupted["signum"] is None:
            interrupted["signum"] = signum
        council_task.cancel()

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            loop.add_signal_handler(signum, _cancel_for_signal, signum)
            registered.append(signum)
        except (NotImplementedError, RuntimeError, ValueError):
            pass

    try:
        return await council_task, None
    except asyncio.CancelledError:
        return None, interrupted["signum"] or signal.SIGINT
    finally:
        for signum in registered:
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.remove_signal_handler(signum)


def main():
    args = _parse_args(sys.argv[1:])

    if args.check_staging_dir is not None:
        _check_staging_dir(args.check_staging_dir)
        return

    _usage_exit_if_file_arg_problems(
        ("--roles-file", args.roles_file),
        ("--context-file", args.context_file),
    )
    _usage_exit_if_staging_dirs_differ(args.roles_file, args.context_file)

    # Parse and validate staged inputs before requiring Codex. This catches
    # temp-path mismatches without launching or depending on any Codex state.
    if args.roles_file is not None:
        custom_roles = _parse_roles_json(_read_roles_file(args.roles_file))
    else:
        custom_roles = []
    roles = _resolve_roles(custom_roles)

    if args.context_file is not None:
        body = _read_context_file(args.context_file)
    else:
        if sys.stdin.isatty():
            print(
                "No input piped. Usage: echo 'context' | "
                "python3 codex_council.py --roles-file roles.json",
                file=sys.stderr,
            )
            sys.exit(1)
        body = _read_stdin_body(sys.stdin.buffer)

    if not shutil.which("codex"):
        print(
            "Codex CLI not found — install with: npm i -g @openai/codex",
            file=sys.stderr,
        )
        sys.exit(1)

    print(
        f"[codex-council] dispatching {len(roles)} roles "
        f"({', '.join(r.id for r in roles)}).",
        file=sys.stderr,
    )

    started = time.monotonic()
    try:
        results, signum = asyncio.run(_run_council_with_signals(roles, body))
    except KeyboardInterrupt:
        print("\n[codex-council] interrupted by user", file=sys.stderr)
        sys.exit(130)
    if signum is not None:
        signame = signal.Signals(signum).name
        print(f"\n[codex-council] interrupted by {signame}", file=sys.stderr)
        sys.exit(128 + int(signum))

    elapsed = time.monotonic() - started
    print(_format_report(results, elapsed), end="")
    sys.stdout.flush()

    successes = sum(1 for r in results if r.ok)
    total = len(results)
    exit_code = 0 if successes else 1

    # Final, uniquely-shaped, LAST stderr line. Its presence means the stdout
    # report is fully written; it carries the exit status so a lost/orphaned but
    # redirected run is fully recoverable from `err.log` (tail until this line).
    print(
        f"[codex-council] CODEX_COUNCIL_DONE ok={successes} total={total} "
        f"elapsed={elapsed:.1f}s exit={exit_code}",
        file=sys.stderr, flush=True,
    )

    if exit_code:
        sys.exit(1)


if __name__ == "__main__":
    main()
