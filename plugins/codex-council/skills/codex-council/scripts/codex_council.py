#!/usr/bin/env python3
"""Fan out a prompt to a council of Codex sub-agents in parallel.

Each role runs in its own `codex exec` subprocess with a distinct
framing instruction. Sessions are isolated per (project, role) and
persist across calls so each role accumulates its own thread of
project knowledge.

Results are aggregated into one structured markdown report on stdout
for Claude to reconcile.

This script is a pure orchestrator — there is no built-in role
catalog and no default council. Callers (Claude as orchestrator)
ultrathink about the user's task, compose the role panel JSON
on-the-fly per invocation, confirm with the user via
AskUserQuestion, then invoke this script. Roles arrive via
`--roles-file` (a path to a JSON file holding the panel), which keeps
a large role array out of the shell entirely.

Usage:
    echo "<context>" | python3 codex_council.py --roles-file roles.json

Env vars:
    CODEX_COUNCIL_SESSION_KEY     scope council threads (e.g. per branch)

No wall-clock timeouts are enforced — each role runs as long as Codex
takes. Ctrl+C still tears down every in-flight codex process group.

POSIX-only: uses start_new_session and process-group signals.
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
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

# Stderr substring markers (matched case-insensitively) classifying
# failure modes. Order of check matters: auth first (never clear state),
# then retriable (rate-limit / 5xx), then stale-resume (clear and restart).
AUTH_ERROR_MARKERS = (
    "401 unauthorized",
    "incorrect api key",
    "authentication failed",
    "auth: token rejected",
    "please run `codex login`",
    "please run codex login",
)
RATE_LIMIT_MARKERS = (
    "429",
    "rate limit",
    "rate_limit",
    "too many requests",
    "quota exceeded",
)
TRANSIENT_5XX_MARKERS = (
    "500 internal",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
    "internal server error",
    "service unavailable",
)
STALE_RESUME_MARKERS = (
    "no rollout found",
    "thread not found",
    "session not found",
    "session expired",
    "thread expired",
)

SESSION_KEY_ENV = "CODEX_COUNCIL_SESSION_KEY"

MAX_PARALLEL = 6  # matches codex's own DEFAULT_AGENT_MAX_THREADS
MAX_RETRY_ATTEMPTS = 2
INITIAL_BACKOFF_SECS = 5
MAX_STDIN_BYTES = 10 << 20  # 10 MiB (a sanity guard, not a model-context
# limit — Codex's own context window is the real ceiling; compress anyway).

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


def _session_key():
    """Optional caller-provided key for scoping council threads."""
    return os.environ.get(SESSION_KEY_ENV, "").strip()


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


def load_session(role_id):
    """Return (session_id, meta) for this role's stored thread, or (None, None)."""
    try:
        with open(_state_path(role_id)) as f:
            meta = json.load(f)
            sid = meta.get("session_id")
            if sid:
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

def extract_session_id(jsonl_output):
    """Pull thread_id from the first thread.started event in the stream."""
    for line in jsonl_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            if event.get("type") == "thread.started" and "thread_id" in event:
                return event["thread_id"]
        except (json.JSONDecodeError, KeyError):
            continue
    return None


def extract_final_message(jsonl_output):
    """Pull the last agent_message text from item.completed events."""
    last_message = None
    for line in jsonl_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            if event.get("type") == "item.completed":
                item = event.get("item", {})
                if item.get("type") == "agent_message" and "text" in item:
                    last_message = item["text"]
        except (json.JSONDecodeError, KeyError):
            continue
    return last_message


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


# ---------- prompt composition ----------

def _compose_prompt(role, body):
    """Bookend the body with the role's framing instruction (both ends)."""
    return f"{role.instruction}\n\n{body}\n\n{role.instruction}"


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
        stdout_b, stderr_b = await proc.communicate(prompt.encode("utf-8"))
    except asyncio.CancelledError:
        _terminate_process_group(proc)
        raise
    return (
        proc.returncode,
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
    )


def _terminate_process_group(proc):
    """Best-effort SIGTERM then SIGKILL to the codex process group."""
    if proc.returncode is not None:
        return
    pid = proc.pid
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except (ProcessLookupError, OSError):
            pass
    try:
        if proc.returncode is None:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass


def _format_clean_exit_no_message(stderr_stripped):
    base = "Codex exited cleanly but produced no agent_message."
    return f"{base}\n{stderr_stripped}" if stderr_stripped else base


def _classify_failure(stderr_stripped, rc, phase):
    """Return a tagged error string for a non-zero codex exit."""
    if _is_auth_error(stderr_stripped):
        return f"[auth] {stderr_stripped or f'codex {phase} exited {rc}'}"
    if _is_rate_limit_error(stderr_stripped):
        return f"[retriable:rate-limit] {stderr_stripped or f'codex {phase} exited {rc}'}"
    if _is_transient_5xx_error(stderr_stripped):
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
        stderr_stripped = stderr.strip()

        if rc == 0:
            # Resume-footgun mitigation: codex `resume <bogus_id>` silently
            # starts a NEW thread instead of failing. Detect by comparing the
            # emitted thread.started.thread_id to what we asked to resume.
            # If mismatched, adopt the new id (no benefit re-running an
            # already-completed turn) and warn — the role lost its prior
            # accumulated framing so the next call starts cold-ish.
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
                error=_format_clean_exit_no_message(stderr_stripped),
                thread_id=session_id, elapsed_seconds=elapsed, attempts=attempt,
                warning=warning,
            )

        # rc != 0 on resume. Classify, then decide retry vs stale vs fail.
        err = _classify_failure(stderr_stripped, rc, "resume")
        if err.startswith("[auth]") or err.startswith("[retriable:"):
            return RoleResult(
                role=role, ok=False, error=err,
                elapsed_seconds=time.monotonic() - started, attempts=attempt,
            )
        if not _is_stale_resume_error(stderr_stripped):
            return RoleResult(
                role=role, ok=False, error=err,
                elapsed_seconds=time.monotonic() - started, attempts=attempt,
            )

        # Stale: log, clear, fall through to fresh.
        updated = (meta or {}).get("updated_at", "unknown")
        print(
            f"[codex-council:{role.id}] session {session_id} (last used {updated}) "
            f"is stale ({stderr_stripped}) — starting fresh.",
            file=sys.stderr,
        )
        current_id, _ = load_session(role.id)
        if current_id == session_id:
            clear_session(role.id)

    # Fresh path.
    rc, stdout, stderr = await _run_codex_subprocess(_fresh_cmd(root), prompt)
    stderr_stripped = stderr.strip()

    if rc != 0:
        return RoleResult(
            role=role, ok=False,
            error=_classify_failure(stderr_stripped, rc, "exec"),
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
        error=_format_clean_exit_no_message(stderr_stripped),
        thread_id=new_id, elapsed_seconds=elapsed, attempts=attempt,
    )


async def _run_role(role, prompt):
    """One role with retry on rate-limit/5xx. No wall-clock deadline.

    Each attempt runs to completion; Codex decides when it is done.
    Ctrl+C propagates as asyncio.CancelledError, which tears down the
    codex process group via _run_codex_subprocess's cancellation path.
    """
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
        t = asyncio.create_task(_run_role(role, _compose_prompt(role, body)))
        t.add_done_callback(lambda task, role=role: _on_role_done(role, task))
        tasks.append(t)
    started = time.monotonic()
    results = await asyncio.gather(*tasks, return_exceptions=True)
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
        lines.append(
            f"- **{r.role.label}** [{r.role.id}]: {status}{attempts}{warn_note} — "
            f"{r.elapsed_seconds:.1f}s"
        )
    lines.append("")

    for r in results:
        lines.append(f"## {r.role.label} ({r.role.id})")
        lines.append("")
        if r.warning:
            lines.append(f"_Warning: {r.warning}_")
            lines.append("")
        if r.ok:
            lines.append((r.text or "").rstrip())
        else:
            lines.append(f"_Failed: {r.error}_")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


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
    return parser.parse_args(argv)


def _usage_exit(msg):
    """Exit 2 with msg on stderr (argparse-compatible usage-error code)."""
    print(msg, file=sys.stderr)
    raise SystemExit(2)


def _read_roles_file(path):
    """Read the raw roles JSON from a file.

    Passing the panel as a path lets the caller write the JSON with a real
    editor/tool instead of escaping a large blob through the shell, where a
    stray quote or unbalanced brace would break the call. Read and decode
    errors exit 2 like other usage errors; JSON validity is left to
    _parse_roles_json.
    """
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        _usage_exit(f"--roles-file: cannot read {path!r} ({e}).")
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


def _parse_roles_json(raw):
    """Parse the --roles-file blob into a list of Role objects.

    Validates each entry has non-empty id/label/instruction strings, id
    is well-formed, and ids are unique within the JSON.
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
        for field in ("id", "label", "instruction"):
            if field not in entry:
                _usage_exit(f"--roles-file {ctx}: missing field {field!r}.")
            value = entry[field]
            if not isinstance(value, str) or not value.strip():
                _usage_exit(
                    f"--roles-file {ctx}: field {field!r} must be a non-empty string."
                )
        rid = entry["id"]
        _validate_role_id(rid, ctx)
        if rid in seen:
            _usage_exit(
                f"--roles-file {ctx}: duplicate id {rid!r} within JSON payload."
            )
        seen.add(rid)
        roles.append(Role(rid, entry["label"], entry["instruction"]))
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


def _read_stdin_body(stream):
    """Read the prompt body from a binary stream, enforcing the byte cap.

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
            f"Input exceeds {MAX_STDIN_BYTES} bytes "
            f"({MAX_STDIN_BYTES >> 20} MiB) — trim before piping.",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        body = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        print(f"Input is not valid UTF-8 ({e}) — pipe text.", file=sys.stderr)
        sys.exit(1)
    if not body.strip():
        print("Empty input — pipe a complete prompt instead.", file=sys.stderr)
        sys.exit(1)
    return body


def main():
    args = _parse_args(sys.argv[1:])

    if not shutil.which("codex"):
        print(
            "Codex CLI not found — install with: npm i -g @openai/codex",
            file=sys.stderr,
        )
        sys.exit(1)

    if sys.stdin.isatty():
        print(
            "No input piped. Usage: echo 'context' | "
            "python3 codex_council.py --roles-file roles.json",
            file=sys.stderr,
        )
        sys.exit(1)

    body = _read_stdin_body(sys.stdin.buffer)

    # Parse unconditionally when a file path was supplied: an empty file
    # then yields the clear "invalid JSON" usage error rather than the
    # generic "no roles requested" message (which is for bare invocation).
    if args.roles_file:
        custom_roles = _parse_roles_json(_read_roles_file(args.roles_file))
    else:
        custom_roles = []
    roles = _resolve_roles(custom_roles)

    print(
        f"[codex-council] dispatching {len(roles)} roles "
        f"({', '.join(r.id for r in roles)}).",
        file=sys.stderr,
    )

    started = time.monotonic()
    try:
        results = asyncio.run(run_council(roles, body))
    except KeyboardInterrupt:
        print("\n[codex-council] interrupted by user", file=sys.stderr)
        sys.exit(130)

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
