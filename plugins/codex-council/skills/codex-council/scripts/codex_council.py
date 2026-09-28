#!/usr/bin/env python3
"""Coordinate an adaptive, context-driven council of Codex agents.

Each role runs in its own `codex exec` subprocess with a distinct
framing instruction. Sessions are isolated per (project, host session,
role) when a terminal/session identifier is available, and persist
across calls from that host session so each role accumulates its own
thread of project knowledge.

Results are aggregated into one structured markdown report on stdout
for Claude to reconcile.

This script is a pure runner — there is no built-in role catalog and no
default council or role count; one role is as valid as many. Claude
reconstructs the user's live problem and project state, then composes the
roles the work actually needs. Roles arrive via `--roles-file` (a path to a
JSON file holding the panel), which keeps a large role array out of the shell
entirely. Each role object has `id`, `label`, and `instruction`, plus the
optional `model`, `effort`, and `selection` keys.

Model and effort: a role that omits all three inherits Codex's native
configuration in the worker's execution context, and the runner sends no
model or effort override at all. Codex resolves that configuration itself
from its layers (CLI flags, a trusted project `.codex/config.toml` found
from the project root, the user's `$CODEX_HOME/config.toml`, cloud, system,
and managed layers, and any managed new-thread defaults). The runner
forwards no profile, runs every worker as `codex exec -C <git toplevel of
the launch directory, else the launch directory>`, and lets workers inherit
its own cwd and environment. A role's `selection` object declares where its
values came from: "user" is an explicit pin, forwarded unchanged and never
replaced; "routed" (a model and effort pair) and "native_effort" (an effort
on the proven native model, which the runner pins) are runtime-grounded
choices validated against this run's discovery snapshot and revalidated by
one fresh discovery at launch; evidence that no longer supports them
resolves the role to native inheritance with a recorded reason. The values
actually sent are passed as `-m <model>` and
`-c model_reasoning_effort="<effort>"` on every invocation of that role
(fresh and resume alike; they are not sticky across calls). codex exec does
not report which model or effort served a turn, so reports describe what
the council sent. A model Codex rejects fails the role as [model-rejected]
(no substitute is tried and the saved thread is kept), and a usage or
credit limit fails it as [quota]; neither is retried.

`--discover RUNDIR` runs bounded, metadata-only discovery against
`codex app-server` (no thread or turn is started), writes
RUNDIR/model-snapshot.json, and prints a compact summary Claude reads
before writing roles.json. CODEX_COUNCIL_MODEL_ROUTING=off disables
automatic selection; explicit user pins still apply.

The orchestrator imposes no size or count ceiling on role panels, role
fields, context, stdin, or composed prompts, and never truncates them.
Downstream model/provider limits and physical memory remain external.

Early results: as each role settles (ok, FAILED, or crashed) the launch
path writes that role's report section atomically to
`<RUNDIR>/replies/<key>.md` (RUNDIR = the private directory holding the
staged inputs; <key> = the role id, or a fixed-size hash for long ids) and
only then emits its completion progress line, which ends in
` reply=<absolute path>` when the file was written. Reply files already on
disk survive an interruption. `--follow RUNDIR` is a read-only follower for
a Claude Code Monitor: it streams the `[codex-council` lines of
RUNDIR/err.log and exits 0 after the CODEX_COUNCIL_DONE sentinel, an
interruption line, or a `runner aborted` line (3 = no council activity
appeared, 4 = the runner is presumed gone; see _follow).

One launch per RUNDIR: `--discover` and `--check-staging-dir` refuse a
directory that already holds out.md, err.log, or replies/, because the
launch command's own redirections would truncate a running council's files
before this script could object.

Usage:
    python3 codex_council.py --discover RUNDIR
    python3 codex_council.py --check-staging-dir RUNDIR
    python3 codex_council.py --roles-file roles.json --context-file context.md
    python3 codex_council.py --follow RUNDIR

Env vars:
    CODEX_COUNCIL_SESSION_KEY     explicit council thread scope override
    CODEX_COUNCIL_DISABLE_AUTO_SESSION_KEY=1
                                   fall back to project-wide role state
    CODEX_COUNCIL_MAX_PARALLEL    positive active-role concurrency override;
                                   otherwise Codex agents.max_threads, else
                                   the council's default of 6
    CODEX_COUNCIL_STALL_SECS      output-inactivity watchdog threshold in
                                   seconds (default 1800; 0 disables)
    CODEX_COUNCIL_MODEL_ROUTING   auto (default when unset or empty) or off;
                                   off disables automatic per-role model and
                                   effort selection (explicit pins still apply)

The council has no total elapsed-time or run-level deadline. A role may run
indefinitely while its codex subprocess continues producing output bytes.
Separately, each codex subprocess has an OUTPUT-INACTIVITY watchdog based
only on time since its most recent stdout/stderr byte. After
CODEX_COUNCIL_STALL_SECS of council-visible silence the runner terminates
that attempt and applies the stall policy: retriable only when no
side-effect-capable work had begun, success-with-warning when the turn had
already completed, terminal otherwise. Setting 0 may again permit an
indefinitely silent role. Ctrl+C still tears down every in-flight codex
process group.

The optional `--skill-contract <int>` flag pins the SKILL/script contract
epoch: absent it is ignored; present it must equal this script's epoch or
the launch is refused as a stale SKILL/script pair. It also marks the skill
path, where a model or effort without a `selection` object is refused;
direct CLI use without it keeps treating such a pin as an explicit user
pin. The --discover summary's first line and the staging-OK, dispatch,
heartbeat, and CODEX_COUNCIL_DONE lines carry
`version=<plugin version>` for postmortem visibility (it does not prevent
skew; the contract epoch does).

This file is the only entry point. It imports the sibling modules in
its directory: council_common.py (shared primitives),
council_discovery.py (--discover and the model snapshot),
council_selection.py (Role, the `selection` contract, and its resolver), and
council_failures.py (failure classification).

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
import sys
import time
from dataclasses import dataclass
from typing import Optional

try:
    import tomllib
except ImportError:  # Python < 3.11: keep the Codex default fallback.
    tomllib = None

# python3 -P and PYTHONSAFEPATH=1 leave this script's directory off
# sys.path; put it first (once) so the sibling modules below resolve.
_SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
if sys.path[:1] != [_SCRIPT_DIR]:
    sys.path.insert(0, _SCRIPT_DIR)

# Python never caches bytecode for the script it runs, so the single-file
# runner wrote nothing into its own (installed plugin) directory. Import the
# siblings with bytecode writes off to keep it that way.
_DONT_WRITE_BYTECODE = sys.dont_write_bytecode
sys.dont_write_bytecode = True
from council_common import (  # noqa: E402
    _READ_CHUNK_BYTES,
    LINEBREAK_CHARS,
    REPLIES_SUBDIR,
    REPLY_MARKER,
    ROLES_REWRITE_RECOVERY,
    STAGED_LAUNCH_RESTART,
    STAGED_LAUNCH_ROLES_RECOVERY,
    STAGING_DIR_RECOVERY,
    _atomic_write_private,
    _check_private_dir,
    _dedupe_preserve_order,
    _diag,
    _iter_json_objects,
    _log_inline,
    _plugin_version,
    _print_stdout,
    _private_stat_problem,
    _project_root,
    _report_inline,
    _roles_recovery,
    _roles_usage_exit,
    _strict_json_loads,
    _usage_exit,
    _usage_exit_if_launched,
    _utc_iso,
)
from council_discovery import (  # noqa: E402
    DISCOVERY_TIMEOUT_SECS,
    SNAPSHOT_FILENAME,
    _discover_command,
    _model_routing_mode,
)
from council_failures import (  # noqa: E402
    _classify_failure,
    _failure_records,
    _failure_text,
    _failure_verdict,
)
from council_selection import (  # noqa: E402
    MODEL_SELECTION_CAVEAT,
    NO_AUTOMATIC_SELECTIONS,
    Role,
    _discovery_sentence,
    _is_automatic,
    _launch_discovery_state,
    _model_selection_lines,
    _parse_role_selection,
    _resolve_run_selections,
    _role_decision,
    _selection_plan_text,
    _selection_section_text,
    _selection_summary_note,
)
sys.dont_write_bytecode = _DONT_WRITE_BYTECODE

STATE_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
    "codex-council",
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

MAX_RETRY_ATTEMPTS = 2
INITIAL_BACKOFF_SECS = 5
TERMINATION_GRACE_SECS = 0.2
LOCK_PROBE_INITIAL_BACKOFF_SECS = 0.1
LOCK_PROBE_MAX_BACKOFF_SECS = 2.0
DEFAULT_MAX_PARALLEL = 6
MAX_PARALLEL_ENV = "CODEX_COUNCIL_MAX_PARALLEL"
STALL_SECS_ENV = "CODEX_COUNCIL_STALL_SECS"
DEFAULT_STALL_SECS = 1800
PROGRESS_HEARTBEAT_SECS = 30 * 60
# Heartbeat cadence floor while the watchdog is enabled; the cadence adapts to
# min(PROGRESS_HEARTBEAT_SECS, stall_secs // 3) so at least two heartbeats can
# report a rising quiet value before the watchdog threshold.
HEARTBEAT_FLOOR_SECS = 300
# Contract epoch for the optional --skill-contract handshake. Bump only when
# SKILL.md's launch/preflight command contract changes incompatibly (3: the
# `selection` object and --discover).
SKILL_CONTRACT_EPOCH = 3
# --follow: poll cadence, how long to wait for a council to show any sign of
# launching (err.log present AND a dispatch line), and how long a dispatched
# council's err.log may stay byte-silent before the follower concludes the
# runner died. A live runner emits a status heartbeat at least every
# PROGRESS_HEARTBEAT_SECS, so twice that plus a margin is never reached by a
# healthy council.
FOLLOW_POLL_SECS = 0.5
FOLLOW_START_SECS = 120
FOLLOW_SILENCE_SECS = 2 * PROGRESS_HEARTBEAT_SECS + 60
# Follower exit codes: 0 = terminal line seen (sentinel, interruption, or
# runner aborted);
# 2 = usage error; 3 = no council activity; 4 = the runner appears to have
# died without a terminal line.
FOLLOW_EXIT_NO_ACTIVITY = 3
FOLLOW_EXIT_RUNNER_GONE = 4
FOLLOW_LINE_PREFIX = "[codex-council"
FOLLOW_DONE_PATTERN = re.compile(
    r"^\[codex-council\] CODEX_COUNCIL_DONE ok=\d+ total=\d+ "
    r"elapsed=[\d.]+s exit=\d+ version=\S+\Z"
)
FOLLOW_INTERRUPTED_PATTERN = re.compile(
    r"^\[codex-council\] interrupted by \S+\Z"
)
# Printed when the runner exits after dispatch without a report: stdout was
# dead at report time, or an unhandled exception escaped the council.
FOLLOW_ABORTED_PATTERN = re.compile(
    r"^\[codex-council\] runner aborted exit=\d+: \S.*\Z"
)
FOLLOW_DISPATCH_PREFIX = "[codex-council] dispatching "
# A wall-clock jump this much larger than the monotonic advance between two
# follower polls is treated as a system suspend (see _follow).
FOLLOW_SUSPEND_SLACK_SECS = 60
REQUIRED_SCOPE_PHRASE = "nothing material"
REQUIRED_CADENCE_SENTENCE = "Thoroughness beats speed."
# Count-neutral on purpose: a council may have exactly one role. Verifier
# framing: the user's requirements are authoritative, while Claude's account
# of the work is a set of claims to check against the workspace. The role
# instruction bookends the prompt (see _compose_prompt), so this brief stays
# short and the lens-specific instruction is the last thing the model reads.
COLLABORATION_BRIEF = (
    "You are working as one role in a Claude-orchestrated Codex council, an "
    "independent cross-model check on Claude Code's work; you may be the "
    "only role, or one of several covering other lenses in parallel. In the "
    "shared working context below, the user's goal, requirements, and "
    "constraints are authoritative; Claude's account of the project state, "
    "its conclusions, and what has already been tried are claims to verify "
    "against the workspace, not facts to accept. This run is "
    "non-interactive: do not ask the user questions or wait for input; "
    "state the assumptions you make and list any decision that needs the "
    "user as an open question. Do not spawn subagents unless your role "
    "instruction asks for them. Stay within your role's lens, and stop when "
    "its deliverable is complete. Keep verified evidence (file:line "
    "references, command output) separate from inference, and say what you "
    "checked and what remains unverified. Size any testing to the change. "
    "Finish with plain paragraphs Claude can reconcile: the result first, "
    "then the evidence, dependencies on other work, risks, and open "
    "questions."
)
STAGING_PATH_HINT = (
    "Staging hint: use the exact directory printed by `mktemp -d` for "
    "both roles.json and context.md in this invocation; keep roles, context, "
    "out.md, and err.log under the same mktemp directory. Shell variables do "
    "not persist across Claude Code Bash calls."
)
# STAGING_DIR_RECOVERY's action, phrased for the stdin launch mode, where
# roles.json is the only on-disk input: no context.md and no preflight
# exist to mention.
STDIN_DIR_RECOVERY = (
    "Recovery: abandon this directory — do not chmod it, do not mkdir it, "
    "and do not reuse its name. Run `mktemp -d` again, copy the NEW printed "
    "absolute path, rewrite the roles file into that new directory, and "
    "re-run the direct command against it."
)

# No run-level deadline, by design: neither this script nor `codex exec`
# bounds a role's total duration, so a role may think for hours while its
# subprocess keeps producing output bytes. The only liveness control here is
# the per-subprocess OUTPUT-INACTIVITY watchdog (CODEX_COUNCIL_STALL_SECS),
# which measures council-visible bytes, not progress: current codex exec
# --json suppresses agent-message/reasoning item.started events and all
# token/exec-output deltas, so a healthy role can be byte-silent for long
# stretches. codex's own per-PROVIDER stream-idle timeout
# (`model_providers.<id>.stream_idle_timeout_ms`, 5 min default, bounded
# retries) is a separate provider-side control left to the user's Codex
# configuration: it is provider-scoped and the active provider id varies,
# so the council cannot target it portably.


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


@dataclass
class CodexRun:
    """Structured outcome of one codex subprocess attempt.

    `stalled` is the watchdog verdict and outranks any text classification of
    stdout/stderr. `turn_completed` / `unsafe_to_replay` are derived from the
    buffered JSONL events so the stall policy can tell a wedged shutdown from
    an interrupted turn, and a replay-safe attempt from one whose tool work
    may have had side effects.
    """
    returncode: Optional[int]
    stdout: str
    stderr: str
    stalled: bool = False
    turn_completed: bool = False
    unsafe_to_replay: bool = False


def _append_warning(existing, new):
    """Compose role warnings without overwriting earlier (higher-value) ones."""
    if not new:
        return existing
    if not existing:
        return new
    return f"{existing}; {new}"


def _stall_secs():
    """Output-inactivity watchdog threshold in seconds; 0 means disabled."""
    raw = os.environ.get(STALL_SECS_ENV, "").strip()
    if not raw:
        return DEFAULT_STALL_SECS
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value < 0:
        _usage_exit(
            f"{STALL_SECS_ENV} must be a positive integer (or 0 to disable "
            f"the watchdog); got {raw!r}."
        )
    return value


def _watchdog_desc(stall_secs):
    """Human/heartbeat-facing rendering of the watchdog threshold."""
    return f"{stall_secs}s" if stall_secs > 0 else "disabled"


def _heartbeat_secs(stall_secs):
    """Adaptive heartbeat cadence: denser while the watchdog is armed."""
    if stall_secs <= 0:
        return PROGRESS_HEARTBEAT_SECS
    return max(
        HEARTBEAT_FLOOR_SECS, min(PROGRESS_HEARTBEAT_SECS, stall_secs // 3)
    )


# Per-role liveness the heartbeat reads. Written by the subprocess pumps and
# the retry loop; module-level because run_council and the pumps are far
# apart, and same-role concurrency is already excluded by the continuity lock.
# Values: a monotonic stamp of the last output byte, or "retry-wait" while a
# role sleeps out a retry backoff (a stale quiet value would be misleading).
_ROLE_LIVENESS = {}


def _role_liveness_desc(role_id, now):
    """One heartbeat fragment for an active role."""
    state = _ROLE_LIVENESS.get(role_id)
    if state == "retry-wait":
        return f"{role_id} retry-wait"
    if isinstance(state, (int, float)):
        return f"{role_id} quiet={max(0.0, now - state):.0f}s"
    return role_id


# \Z, not $: in Python `$` also matches just before a trailing "\n", so
# "architect\n" would pass and inject a newline into state filenames, the
# report summary line, and stderr progress. \Z anchors the true end of string.
ROLE_ID_PATTERN = re.compile(r"^[a-z0-9_-]+\Z")


def _state_role_component(role_id):
    """Return a filename-safe, bounded component for an unrestricted role ID.

    IDs of 32 characters or fewer keep the literal ID (existing state paths
    stay valid); longer IDs are hashed to a fixed-size component so the
    filename never exceeds the platform's per-component limit. Used for both
    the continuity state file and the per-role reply file.
    """
    if len(role_id) <= 32:
        return role_id
    digest = hashlib.sha256(role_id.encode("utf-8")).hexdigest()
    return f"role-sha256-{digest}"


# ---------- project / session state (sync) ----------

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


def _configured_codex_max_threads():
    """Read the user-level Codex agents.max_threads preference if available.

    Current Codex documentation lists agents.max_threads as a legacy alias
    of agents.max_concurrent_threads_per_session (not read here) and leaves
    the unset default to Codex; DEFAULT_MAX_PARALLEL=6 was its documented
    default when the council adopted it. The council launches separate
    `codex exec` processes rather than Codex's in-process subagents, so this is
    a conservative local concurrency signal, not a provider-capacity promise.
    Invalid, absent, or unreadable config falls back cleanly.
    """
    codex_home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    path = os.path.join(codex_home, "config.toml")
    if tomllib is None:
        # Python 3.10 and older have no stdlib TOML parser. Read only the one
        # integer setting we need; keep the fallback deliberately strict so a
        # complex or malformed value cannot accidentally raise concurrency.
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
        except (OSError, UnicodeDecodeError):
            return None
        in_agents = False
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("["):
                in_agents = bool(
                    re.fullmatch(r"\[\s*agents\s*\](?:\s*#.*)?", line)
                )
                continue
            match = re.fullmatch(
                r"(?:agents\.)?max_threads\s*=\s*([1-9][0-9_]*)"
                r"(?:\s*#.*)?",
                line,
            )
            if match and (in_agents or line.startswith("agents.")):
                return int(match.group(1).replace("_", ""))
        return None
    try:
        with open(path, "rb") as f:
            config = tomllib.load(f)
    except (OSError, ValueError):
        return None
    agents = config.get("agents")
    if not isinstance(agents, dict):
        return None
    value = agents.get("max_threads")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _max_parallel_roles():
    """Return the positive active-role limit for this council invocation."""
    override = os.environ.get(MAX_PARALLEL_ENV, "").strip()
    if override:
        try:
            value = int(override)
        except ValueError:
            value = 0
        if value <= 0:
            _usage_exit(
                f"{MAX_PARALLEL_ENV} must be a positive integer; got "
                f"{override!r}."
            )
        return value
    return _configured_codex_max_threads() or DEFAULT_MAX_PARALLEL


def _project_key(role_id):
    """Stable state key for (project, role, optional session-key)."""
    base = hashlib.sha256(_project_root().encode()).hexdigest()[:16]
    role_component = _state_role_component(role_id)
    session_key = _session_key()
    if session_key:
        suffix = hashlib.sha256(session_key.encode()).hexdigest()[:16]
        return f"{base}-{suffix}__{role_component}"
    return f"{base}__{role_component}"


def _state_path(role_id):
    """Per-(project, role) state path; long IDs use a fixed-size hash key."""
    return os.path.join(STATE_DIR, f"{_project_key(role_id)}.json")


def _state_lock_path(role_id):
    """Per-state-file lock path used across council processes."""
    return _state_path(role_id) + ".lock"


def _try_role_state_lock(role_id):
    """Return a held role-state lock, or None without waiting."""
    os.makedirs(STATE_DIR, exist_ok=True)
    lock_path = _state_lock_path(role_id)
    lock_file = open(lock_path, "a+")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_file
    except BlockingIOError:
        lock_file.close()
        return None
    except BaseException:
        lock_file.close()
        raise


def _release_role_state_lock(lock_file):
    """Release a lock returned by _try_role_state_lock."""
    try:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
    finally:
        lock_file.close()


def load_session(role_id):
    """Return (session_id, meta) for this role's stored thread, or (None, None)."""
    try:
        with open(_state_path(role_id)) as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, None
    # Corrupt state that is valid JSON but not an object (e.g. "[]") must
    # degrade to a fresh start like any other corruption, not crash the role.
    if isinstance(meta, dict):
        sid = meta.get("session_id")
        if isinstance(sid, str) and sid:
            return sid, meta
    return None, None


def save_session(role_id, session_id):
    """Persist session metadata through the shared atomic writer.

    _atomic_write_private: a 0600 temp file in STATE_DIR, fsync, then
    os.replace, so a crash or power loss never leaves a truncated state
    file that load_session would read as no thread. An OSError propagates
    (the temp file already removed) so each caller keeps its own
    reply-first warning.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    meta = {
        "session_id": session_id,
        "role_id": role_id,
        "project_path": _project_root(),
        "updated_at": _utc_iso(time.time()),
    }
    session_key = _session_key()
    if session_key:
        meta["session_key"] = session_key
    _atomic_write_private(
        _state_path(role_id), json.dumps(meta, indent=2).encode("utf-8")
    )


def clear_session(role_id):
    """Remove this role's stored thread state.

    Ignores only a missing file. Any other OSError propagates so callers can
    attach a warning — a swallowed failure here would make stale state
    silently immortal.
    """
    try:
        os.remove(_state_path(role_id))
    except FileNotFoundError:
        pass


# ---------- JSONL parsing ----------

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


def extract_item_errors(jsonl_output):
    """Pull non-fatal ``item.completed`` error items from Codex JSONL stdout.

    Codex reports some advisories this way on runs that still succeed, e.g.
    "This session was recorded with model X but is resuming with Y" when a
    resumed thread runs on a model other than the one it was recorded with
    (for example, no override after the native configuration changed, or a
    different override).
    """
    messages = []
    for event in _iter_json_objects(jsonl_output):
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "error":
            message = item.get("message")
            if isinstance(message, str) and message.strip():
                messages.append(message.strip())
    return _dedupe_preserve_order(messages)


def _with_item_error_warning(warning, jsonl_output):
    """Append any codex item-level error advisories to a role warning."""
    for message in extract_item_errors(jsonl_output):
        warning = _append_warning(warning, f"codex reported: {message}")
    return warning


# ---------- prompt composition ----------

def _compose_prompt(role, body):
    """Frame intact shared context for one role and reinforce its lens."""
    return (
        f"{role.instruction}\n\n"
        f"{COLLABORATION_BRIEF}\n\n"
        f"## Shared working context\n\n{body}\n\n"
        f"{role.instruction}"
    )


# ---------- async codex invocation ----------

def _model_overrides(model=None, effort=None):
    """Parent `codex exec` options for one invocation's dispatched values.

    Callers pass only a SelectionDecision's dispatch_model/dispatch_effort.
    Empty when neither is sent, so an inheriting role gets no override at
    all and Codex resolves its native configuration in the worker's
    execution context. Placed with `-C` BEFORE any `resume` subcommand
    (verified against codex-cli 0.157.1: parent-placed `-m`/`-c` apply to
    both fresh and resumed turns). The overrides are per-invocation, not
    sticky: a resumed turn without them runs on the current native
    configuration, not the thread's recorded model. Both values match
    SELECTION_VALUE_PATTERN (checked at parse time, and for a pinned native
    model at resolution), so neither can start with "-" or hold whitespace,
    quotes, backslashes, or control characters: `-m <model>` stays one argv
    value, and the TOML basic string in model_reasoning_effort="<effort>"
    needs no escaping and cannot be broken out of.
    """
    opts = []
    if model:
        opts += ["-m", model]
    if effort:
        opts += ["-c", f'model_reasoning_effort="{effort}"']
    return opts


def _resume_cmd(root, session_id, model=None, effort=None):
    # `-C` is a parent option of `codex exec` and must precede `resume`; the
    # dispatched model/effort overrides (none when the role inherits) sit
    # with it on the parent, so they apply to the resumed turn.
    return [
        "codex", "exec", "-C", root, *_model_overrides(model, effort),
        "resume", session_id,
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "--json", "-",
    ]


def _fresh_cmd(root, model=None, effort=None):
    return [
        "codex", "exec", "-C", root, *_model_overrides(model, effort),
        "--dangerously-bypass-approvals-and-sandbox",
        "--json", "--skip-git-repo-check", "-",
    ]


class _EventFlagScanner:
    """Derive replay-safety flags from the stdout JSONL as chunks arrive.

    Splits strictly on b"\\n" (JSONL's record separator) for the same reason
    as _iter_json_objects: U+2028/U+2029/U+0085 are legal unescaped inside a
    JSON string. Only items that do no work are replay-safe: the pure-text
    agent_message and reasoning, and Codex's own `error` notices (message
    only — for example the advisory that a resumed thread was recorded
    with another model, routine once roles are routed). Every other type
    (command executions, MCP tool calls, file changes, web searches, to-do
    lists, collab tool calls, and any unknown/future type) marks the attempt
    unsafe to replay — conservative by default, since replaying such a turn
    could duplicate side effects.
    """

    _SAFE_ITEM_TYPES = frozenset({"agent_message", "reasoning", "error"})

    def __init__(self):
        self.turn_completed = False
        self.unsafe_to_replay = False
        self._pending = b""

    def feed(self, chunk):
        data = self._pending + chunk
        lines = data.split(b"\n")
        self._pending = lines.pop()
        for line in lines:
            self._scan_line(line)

    def finish(self):
        """Scan any final unterminated line (a kill can truncate the stream)."""
        pending, self._pending = self._pending, b""
        self._scan_line(pending)

    def _scan_line(self, line):
        line = line.strip()
        if not line:
            return
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(event, dict):
            return
        event_type = event.get("type")
        if event_type == "turn.completed":
            self.turn_completed = True
        elif event_type in ("item.started", "item.completed"):
            item = event.get("item")
            item_type = item.get("type") if isinstance(item, dict) else None
            if item_type not in self._SAFE_ITEM_TYPES:
                self.unsafe_to_replay = True


async def _run_codex_subprocess(cmd, prompt, role_id=""):
    """Run codex exec async with incremental readers and a stall watchdog.

    start_new_session=True puts codex in its own process group so a
    SIGTERM to the group also reaches any shell commands codex itself
    spawned for tool calls. Without it, those grandchildren leak.

    Returns a CodexRun. All termination paths — the watchdog, outer
    cancellation, and any post-spawn failure — converge on one idempotent
    termination task, so duplicate teardowns never race.
    """
    # Encode BEFORE spawning. A prompt carrying a char UTF-8 cannot encode
    # (e.g. a lone surrogate from an escaped "\uD800" in roles.json) would
    # otherwise raise AFTER the child exists, before any pump/teardown task
    # ran — leaking a codex process left blocked forever on the stdin it
    # never received. Failing here, pre-spawn, means there is no child to leak.
    prompt_bytes = prompt.encode("utf-8")
    stall_secs = _stall_secs()
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

    scanner = _EventFlagScanner()
    stdout_buf = bytearray()
    stderr_buf = bytearray()
    # Shared last-activity stamp: any byte on either stream resets the
    # watchdog. Raw bytes are buffered per stream and decoded once after the
    # pumps join, so a UTF-8 sequence split across chunks survives.
    activity = {"at": time.monotonic()}
    if role_id:
        _ROLE_LIVENESS[role_id] = activity["at"]

    def _record_activity():
        activity["at"] = time.monotonic()
        if role_id:
            _ROLE_LIVENESS[role_id] = activity["at"]

    async def _pump(stream, buf, feed_scanner):
        # read(chunk), never readline()/readuntil(): asyncio's stream limit
        # would cap unrestricted JSONL line sizes.
        while True:
            chunk = await stream.read(_READ_CHUNK_BYTES)
            if not chunk:
                return
            _record_activity()
            buf.extend(chunk)
            if feed_scanner:
                scanner.feed(chunk)

    async def _feed_stdin():
        try:
            proc.stdin.write(prompt_bytes)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            # The child exited or closed stdin early; its buffered output and
            # exit status still tell the story.
            pass
        finally:
            with contextlib.suppress(OSError):
                proc.stdin.close()

    termination = {"task": None}

    def _begin_termination():
        """Single idempotent termination owner; every caller awaits it."""
        if termination["task"] is None:
            termination["task"] = asyncio.ensure_future(
                _terminate_process_group(proc, pgid)
            )
        return termination["task"]

    stalled = {"flag": False}

    async def _watchdog():
        # Hand-rolled on purpose: an asyncio.sleep loop over time.monotonic,
        # never asyncio.wait_for/asyncio.timeout — there is no deadline on the
        # subprocess itself, only on its output inactivity.
        while True:
            remaining = stall_secs - (time.monotonic() - activity["at"])
            if remaining > 0:
                await asyncio.sleep(remaining)
                continue
            # Threshold reached: yield once and re-check, so a reader chunk
            # scheduled in this same event-loop turn is not misread as a stall.
            await asyncio.sleep(0)
            quiet = time.monotonic() - activity["at"]
            if quiet < stall_secs:
                continue
            stalled["flag"] = True
            _diag(
                f"[codex-council:{role_id}] stall threshold reached "
                f"(quiet={quiet:.0f}s, watchdog={stall_secs}s); "
                "terminating attempt"
            )
            _begin_termination()
            return

    # Pumps start BEFORE the prompt is written: a child that writes output
    # before consuming an unrestricted-size stdin must not deadlock on full
    # pipes.
    pump_out = asyncio.create_task(_pump(proc.stdout, stdout_buf, True))
    pump_err = asyncio.create_task(_pump(proc.stderr, stderr_buf, False))
    feeder = asyncio.create_task(_feed_stdin())
    watchdog = (
        asyncio.create_task(_watchdog()) if stall_secs > 0 else None
    )
    try:
        await proc.wait()
    except BaseException:
        # Reap on ANY failure or cancellation while the child may be alive.
        if watchdog is not None:
            watchdog.cancel()
        for task in (feeder, pump_out, pump_err):
            task.cancel()
        await _begin_termination()
        raise
    # The process is gone: the watchdog must not fire while the remaining
    # pipe bytes are drained (post-exit data is data, not a stall).
    if watchdog is not None:
        watchdog.cancel()
    if termination["task"] is not None:
        await termination["task"]
    await asyncio.gather(feeder, pump_out, pump_err)
    scanner.finish()
    return CodexRun(
        returncode=proc.returncode,
        stdout=bytes(stdout_buf).decode("utf-8", errors="replace"),
        stderr=bytes(stderr_buf).decode("utf-8", errors="replace"),
        stalled=stalled["flag"],
        turn_completed=scanner.turn_completed,
        unsafe_to_replay=scanner.unsafe_to_replay,
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


def _save_session_reply_first(role_id, session_id, warning):
    """Persist continuity state; a failed save must never cost the reply."""
    try:
        save_session(role_id, session_id)
    except OSError as e:
        warning = _append_warning(
            warning,
            f"reply completed; session state could not be persisted ({e})",
        )
    return warning


def _stalled_role_result(role, run, stored_id, attempt, started, warning=None):
    """Apply the stall policy to a watchdog-terminated attempt.

    The structured stall verdict outranks text sniffing: partial stale/auth
    text in a killed run's stderr must neither classify the failure nor clear
    resume state. Policy:
      * turn.completed AND a final agent_message buffered: the reply is
        definitively complete — the kill hit a wedged shutdown. Success with
        a warning; state saved best-effort; no retry.
      * no side-effect-capable item started: replay is safe — retriable
        through the ordinary shared retry budget. The text says why, not
        that a retry follows: the same text is the final error once the
        budget is spent (the retry itself is logged by _run_role_attempts).
      * otherwise: terminal — replaying could duplicate tool side effects.
        An agent_message without turn.completed is quoted but never
        auto-promoted to success.
    """
    stall = _stall_secs()
    elapsed = time.monotonic() - started
    msg = extract_final_message(run.stdout)
    if run.turn_completed and msg:
        thread_id = extract_session_id(run.stdout) or stored_id
        warning = _append_warning(
            warning,
            "codex wedged after completing its turn; process terminated",
        )
        if thread_id:
            warning = _save_session_reply_first(role.id, thread_id, warning)
        return RoleResult(
            role=role, ok=True, text=msg, thread_id=thread_id,
            elapsed_seconds=elapsed, attempts=attempt, warning=warning,
        )
    if not run.unsafe_to_replay:
        return RoleResult(
            role=role, ok=False,
            error=(
                f"[retriable:stall] no output for {stall}s (watchdog "
                f"{stall}s); no tool work had begun, so replay is safe"
            ),
            thread_id=stored_id, elapsed_seconds=elapsed, attempts=attempt,
            warning=warning,
        )
    error = (
        f"[stall] no output for {stall}s (watchdog {stall}s); not "
        "automatically retried because tool work had begun — re-invoke the "
        "role manually if needed"
    )
    if msg:
        error += f"\nlast (possibly incomplete) agent_message: {msg}"
    return RoleResult(
        role=role, ok=False, error=error, thread_id=stored_id,
        elapsed_seconds=elapsed, attempts=attempt, warning=warning,
    )


def _start_line(role, phase, attempt, stall_secs):
    return (
        f"[codex-council] {role.id}: started ({phase}) "
        f"attempt={attempt}/{MAX_RETRY_ATTEMPTS} "
        f"watchdog={_watchdog_desc(stall_secs)}"
    )


async def _run_role_once(role, prompt, attempt):
    """One codex invocation for one role. No retry logic here.

    The command carries only the role's resolved dispatch values; the
    requested values and the selection never reach codex or saved state.
    """
    started = time.monotonic()
    root = _project_root()
    stall_secs = _stall_secs()
    decision = _role_decision(role)
    model, effort = decision.dispatch_model, decision.dispatch_effort
    session_id, meta = load_session(role.id)
    warning = None

    if session_id:
        _diag(_start_line(role, "resume", attempt, stall_secs))
        run = await _run_codex_subprocess(
            _resume_cmd(root, session_id, model, effort), prompt,
            role_id=role.id,
        )
        # The structured stall verdict is handled BEFORE any text
        # classification: a killed run's partial stderr could look stale or
        # auth-shaped, and must not clear resume state.
        if run.stalled:
            return _stalled_role_result(role, run, session_id, attempt, started)
        failure_text = _failure_text(run.stdout, run.stderr)

        if run.returncode == 0:
            # Resume-footgun mitigation. `codex exec resume <id>` parses <id>
            # as a UUID first (UUIDs take precedence if it parses). On current
            # codex-cli a valid-but-unknown UUID ERRORS ("no rollout found ...
            # -32600", exit 1) and is handled by the stale-resume branch below;
            # only a value that is NOT a valid UUID is treated as a thread NAME
            # and silently starts a NEW thread (rc==0, fresh thread.started).
            # Stored ids are always real UUIDs, so silent-spawn is unreachable
            # via normal state — this check is defense-in-depth (corrupt/manual
            # state, or future CLI drift). Detect by comparing the emitted
            # thread.started.thread_id to what we asked to resume; if mismatched,
            # adopt the new id (no benefit re-running an already-completed turn)
            # and warn — the role lost its prior accumulated framing.
            # The reply is extracted BEFORE any state write so persistence
            # failures can never cost a completed reply.
            msg = extract_final_message(run.stdout)
            emitted_id = extract_session_id(run.stdout)
            adopted_from = None
            if emitted_id and emitted_id != session_id:
                adopted_from = session_id
                warning = (
                    f"resume returned thread_id {emitted_id} != stored "
                    f"{session_id}; adopted new id (prior continuity lost)"
                )
                # emitted_id is codex-controlled text: escape it so it cannot
                # start a forged err.log line or hide this one from --follow.
                _diag(f"[codex-council:{role.id}] {_log_inline(warning)}")
                session_id = emitted_id
            # ONE save covers both the reply-success case and the adoption
            # case. An adoption is persisted even without an agent_message:
            # the stored id is proven wrong (codex ran this turn on a
            # different thread), and leaving it would repeat the
            # silent-spawn footgun on every subsequent call.
            if msg or adopted_from is not None:
                try:
                    save_session(role.id, session_id)
                except OSError as e:
                    if msg:
                        warning = _append_warning(
                            warning,
                            "reply completed; session state could not be "
                            f"persisted ({e})",
                        )
                    else:
                        warning = _append_warning(
                            warning,
                            f"session state could not be persisted ({e})",
                        )
                    if adopted_from is not None:
                        # The stored id is proven wrong; best-effort clear it
                        # so the next invocation does not resume it again.
                        try:
                            clear_session(role.id)
                        except OSError as clear_err:
                            warning = _append_warning(
                                warning,
                                "obsolete session state for "
                                f"{adopted_from} remains and may repeat "
                                f"adoption next invocation ({clear_err})",
                            )
            elapsed = time.monotonic() - started
            if msg:
                warning = _with_item_error_warning(warning, run.stdout)
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

        # rc != 0 on resume. Order (_failure_verdict): auth (never clear
        # state) -> quota (terminal even with HTTP 429) -> ANCHORED-status
        # retriable (a real API 429/5xx, by JSON status / `HTTP NNN` / reason
        # phrase, beats a stale-looking message) -> model rejected (terminal;
        # keeps state even when its text also looks stale) -> stale-resume
        # (clear + restart fresh) -> SUBSTRING retriable fallback. The
        # substring fallback sits after the stale check so a stale error that
        # merely contains a bare digit run (e.g. "...thread id stale-429-sid")
        # still restarts fresh. The verdict is computed once and formatted
        # as-is, so the tag always matches the branch taken here.
        records = _failure_records(run.stdout)
        verdict = _failure_verdict(failure_text, records, model, resume=True)
        if verdict.kind != "stale":
            err = _classify_failure(
                failure_text, run.returncode, "resume", records, decision,
                verdict,
            )
            return RoleResult(
                role=role, ok=False, error=err,
                elapsed_seconds=time.monotonic() - started, attempts=attempt,
            )

        # Stale: log, clear, fall through to fresh. A failed clear is only
        # worth a warning in the outcomes where stale state actually remains
        # on disk (a later successful save atomically replaces it anyway).
        updated = (meta or {}).get("updated_at", "unknown")
        _diag(
            f"[codex-council:{role.id}] session {_log_inline(session_id)} "
            f"(last used {_log_inline(updated)}) "
            f"is stale ({_log_inline(failure_text)}) — starting fresh."
        )
        stale_clear_error = None
        current_id, _ = load_session(role.id)
        if current_id == session_id:
            try:
                clear_session(role.id)
            except OSError as e:
                stale_clear_error = e

    else:
        stale_clear_error = None

    def _with_stale_clear_warning(existing):
        if stale_clear_error is None:
            return existing
        return _append_warning(
            existing,
            "stale session state could not be cleared and remains on disk "
            f"({stale_clear_error})",
        )

    # Fresh path.
    _diag(_start_line(role, "fresh", attempt, stall_secs))
    run = await _run_codex_subprocess(
        _fresh_cmd(root, model, effort), prompt, role_id=role.id
    )
    if run.stalled:
        return _stalled_role_result(
            role, run, None, attempt, started,
            warning=_with_stale_clear_warning(warning),
        )
    failure_text = _failure_text(run.stdout, run.stderr)

    if run.returncode != 0:
        # Same order as the resume path, minus the stale branch.
        return RoleResult(
            role=role, ok=False,
            error=_classify_failure(
                failure_text, run.returncode, "exec",
                _failure_records(run.stdout), decision,
            ),
            elapsed_seconds=time.monotonic() - started, attempts=attempt,
            warning=_with_stale_clear_warning(warning),
        )

    msg = extract_final_message(run.stdout)
    new_id = extract_session_id(run.stdout)
    elapsed = time.monotonic() - started
    if msg:
        # Persist only when both halves of session continuity are
        # present; an agent_message without a thread.started is still a
        # valid reply but cannot be resumed, so skip the save — don't
        # persist a thread that produced no agent_message, but don't
        # drop a reply either.
        if new_id:
            try:
                save_session(role.id, new_id)
            except OSError as e:
                warning = _append_warning(
                    warning,
                    f"reply completed; session state could not be persisted ({e})",
                )
                warning = _with_stale_clear_warning(warning)
        else:
            warning = _with_stale_clear_warning(warning)
        warning = _with_item_error_warning(warning, run.stdout)
        return RoleResult(
            role=role, ok=True, text=msg, thread_id=new_id,
            elapsed_seconds=elapsed, attempts=attempt, warning=warning,
        )
    return RoleResult(
        role=role, ok=False,
        error=_format_clean_exit_no_message(failure_text),
        thread_id=new_id, elapsed_seconds=elapsed, attempts=attempt,
        warning=_with_stale_clear_warning(warning),
    )


async def _run_role_attempts(role, prompt):
    """Run one already-locked role with retry on rate-limit/5xx."""
    last_result = None
    backoff = INITIAL_BACKOFF_SECS

    for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
        result = await _run_role_once(role, prompt, attempt)
        last_result = result
        if result.ok or not (result.error or "").startswith("[retriable:"):
            return result
        if attempt >= MAX_RETRY_ATTEMPTS:
            return result
        _diag(
            f"[codex-council:{role.id}] retriable error on attempt "
            f"{attempt}/{MAX_RETRY_ATTEMPTS}; sleeping {backoff}s."
        )
        # A stale quiet value would be misleading while no subprocess runs.
        _ROLE_LIVENESS[role.id] = "retry-wait"
        await asyncio.sleep(backoff)
        backoff *= 2

    return last_result  # type: ignore[return-value]


def _exception_result(role, exc, elapsed):
    """The RoleResult a crashed role task is reported as.

    Shared by the completion callback (reply file) and the post-gather
    assembly (out.md) so both render the identical section.
    """
    return RoleResult(
        role=role, ok=False,
        error=f"[orchestrator-exception] {type(exc).__name__}: {exc}",
        elapsed_seconds=elapsed, attempts=1,
    )


async def run_council(roles, body, max_parallel=None, replies_dir=None):
    """Fan out the roles (one or many) in parallel and wait for all to finish.

    `roles` is an unrestricted-size list of Role objects supplied by the
    caller via --roles-file. There is no built-in role registry. At most
    `max_parallel` roles are active at once; additional roles remain queued.

    `return_exceptions=True` ensures one role's crash does not cancel
    its siblings — every role gets its turn and its result in the report.
    No run-level deadline: a role runs as long as its codex subprocess
    keeps producing output bytes (see the output-inactivity watchdog).

    Per-role completion progress is emitted to stderr (in completion
    order) as each role settles; stdout stays the report. When
    `replies_dir` is given (only main()'s launch path passes it), each
    settled role's report section is first written atomically to
    `<replies_dir>/<key>.md` and its completion line then ends in
    ` reply=<path>`; a failed write only costs the file, never the result.
    """
    total = len(roles)
    counter = {"done": 0}
    active = set()
    if max_parallel is None:
        max_parallel = _max_parallel_roles()
    if (
        not isinstance(max_parallel, int)
        or isinstance(max_parallel, bool)
        or max_parallel <= 0
    ):
        raise ValueError("max_parallel must be a positive integer")
    semaphore = asyncio.Semaphore(max_parallel)
    started = time.monotonic()

    async def _run_bounded(role):
        probe_backoff = LOCK_PROBE_INITIAL_BACKOFF_SECS
        while True:
            async with semaphore:
                # A nonblocking probe lets a same-role lock waiter yield this
                # execution permit immediately. It also avoids holding one
                # lock file descriptor per queued role in a very large panel.
                lock_file = _try_role_state_lock(role.id)
                if lock_file is None:
                    pass
                else:
                    active.add(role.id)
                    _ROLE_LIVENESS[role.id] = time.monotonic()
                    try:
                        return await _run_role_attempts(
                            role, _compose_prompt(role, body)
                        )
                    finally:
                        active.discard(role.id)
                        _ROLE_LIVENESS.pop(role.id, None)
                        _release_role_state_lock(lock_file)
            # Stay off the execution permit while another council owns this
            # role's continuity lock; unrelated roles get their turn. The
            # probe interval backs off to a small cap: the other council has
            # no run-level deadline, so a fixed 0.1s poll could spin the event
            # loop 10x/second for hours while staying responsive gains nothing.
            await asyncio.sleep(probe_backoff)
            probe_backoff = min(probe_backoff * 2, LOCK_PROBE_MAX_BACKOFF_SECS)

    stall_secs = _stall_secs()
    heartbeat_secs = _heartbeat_secs(stall_secs)

    async def _heartbeat():
        while counter["done"] < total:
            await asyncio.sleep(heartbeat_secs)
            if counter["done"] >= total:
                return
            now = time.monotonic()
            active_desc = ", ".join(
                _role_liveness_desc(rid, now) for rid in sorted(active)
            ) or "none"
            queued = max(0, total - counter["done"] - len(active))
            elapsed = now - started
            _diag(
                f"[codex-council] still running after {elapsed:.0f}s: "
                f"completed={counter['done']}/{total}; active={len(active)} "
                f"({active_desc}); queued={queued}; "
                f"watchdog={_watchdog_desc(stall_secs)}; "
                f"version={_plugin_version()}."
            )

    def _reply_suffix(result):
        # The reply file is written BEFORE the completion line, so a reader
        # that reacts to the line always finds the finished file.
        if replies_dir is None:
            return ""
        path = _write_reply_file(replies_dir, result)
        return f"{REPLY_MARKER}{path}" if path else ""

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
            suffix = _reply_suffix(_exception_result(
                role, exc, time.monotonic() - started
            ))
            _diag(
                f"[codex-council] {n}/{total} {role.id}: crashed "
                f"({type(exc).__name__}){suffix}"
            )
            return
        res = task.result()
        if isinstance(res, RoleResult):
            status = "ok" if res.ok else "FAILED"
            suffix = _reply_suffix(res)
            _diag(
                f"[codex-council] {n}/{total} {role.id}: {status} "
                f"({res.elapsed_seconds:.1f}s){suffix}"
            )
        else:
            _diag(f"[codex-council] {n}/{total} {role.id}: done")

    tasks = []
    for role in roles:
        t = asyncio.create_task(_run_bounded(role))
        t.add_done_callback(lambda task, role=role: _on_role_done(role, task))
        tasks.append(t)
    heartbeat_task = asyncio.create_task(_heartbeat())
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    finally:
        heartbeat_task.cancel()
        # Progress reporting must never turn an otherwise successful council
        # into a failure (for example if stderr was closed by the host).
        with contextlib.suppress(asyncio.CancelledError, OSError):
            await heartbeat_task
    elapsed = time.monotonic() - started

    out = []
    for role, r in zip(roles, results):
        if isinstance(r, RoleResult):
            out.append(r)
        elif isinstance(r, BaseException):
            out.append(_exception_result(role, r, elapsed))
        else:
            out.append(RoleResult(
                role=role, ok=False,
                error=f"[orchestrator-bug] unexpected result {type(r).__name__}",
                elapsed_seconds=elapsed, attempts=1,
            ))
    return out


# ---------- report ----------

def _format_role_section(r):
    """Render one role's report section (heading, model selection, warning,
    reply/failure).

    The single renderer for both out.md and the per-role reply files, so the
    two can never drift apart.
    """
    label = _report_inline(r.role.label)
    selection = _selection_section_text(_role_decision(r.role))
    lines = [
        f"## {label} ({r.role.id})", "",
        f"_Model selection: {_report_inline(selection)}_", "",
    ]
    if r.warning:
        lines.append(f"_Warning: {_report_inline(r.warning)}_")
        lines.append("")
    if r.ok:
        lines.append((r.text or "").rstrip())
    else:
        lines.append(f"_Failed: {_report_inline(r.error)}_")
    lines.append("")
    return lines


def _format_report(results, total_elapsed, discovery_sentence=None):
    """Render results as a single markdown report for Claude to reconcile.

    `discovery_sentence` describes launch discovery for the Model selection
    paragraph (see _discovery_sentence); None means it did not run because
    no role carried a runtime-grounded selection.
    """
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
        note = _report_inline(_selection_summary_note(_role_decision(r.role)))
        warn_note = " — WARNING" if r.warning else ""
        label = _report_inline(r.role.label)
        lines.append(
            f"- **{label}** [{r.role.id}]: {status}{attempts}{note}"
            f"{warn_note} — {r.elapsed_seconds:.1f}s"
        )
    lines.append("")
    sentence = discovery_sentence or (
        f"discovery not run ({NO_AUTOMATIC_SELECTIONS})"
    )
    lines.append(_report_inline(
        f"Model selection: {sentence}. {MODEL_SELECTION_CAVEAT}"
    ))
    lines.append("")

    for r in results:
        lines.extend(_format_role_section(r))

    return "\n".join(lines).rstrip() + "\n"


def _format_reply_file(r):
    """One role's reply file: a one-line status header plus its section.

    model=/effort= are the values SENT (omitted when not sent); selection=
    is the role's provenance; a fallback also names what was requested.
    """
    status = "ok" if r.ok else "FAILED"
    decision = _role_decision(r.role)
    fields = [
        f"id={r.role.id}",
        f"status={status}",
        f"elapsed={r.elapsed_seconds:.1f}s",
        f"attempts={r.attempts}",
        f"selection={decision.provenance}",
    ]
    if decision.dispatch_model:
        fields.append(f"model={decision.dispatch_model}")
    if decision.dispatch_effort:
        fields.append(f"effort={decision.dispatch_effort}")
    if decision.provenance == "fallback":
        if decision.requested_model:
            fields.append(f"requested_model={decision.requested_model}")
        if decision.requested_effort:
            fields.append(f"requested_effort={decision.requested_effort}")
    if r.warning:
        fields.append("warning=yes")
    header = f"<!-- codex-council reply {' '.join(fields)} -->"
    body = "\n".join(_format_role_section(r)).rstrip()
    return f"{header}\n\n{body}\n"


def _reply_file_path(replies_dir, role_id):
    """Deterministic reply-file path; long ids reuse the state-file hash."""
    return os.path.join(replies_dir, f"{_state_role_component(role_id)}.md")


def _write_reply_file(replies_dir, result):
    """Atomically write one settled role's reply file; return its path.

    Uses _atomic_write_private (0600 temp file, fsync, os.replace), so a
    reader never sees a partial file. Advisory by design: any failure
    returns None with one diagnostic and never changes the role's result
    (out.md still carries the section).
    """
    path = _reply_file_path(replies_dir, result.role.id)
    try:
        data = _format_reply_file(result).encode("utf-8", errors="replace")
        _atomic_write_private(path, data)
        return path
    except Exception as e:  # advisory: never let a reply file cost a result
        _diag(
            f"[codex-council:{result.role.id}] reply file not written "
            f"({_report_inline(e)}); the section is still in the final report"
        )
        return None


def _prepare_replies_dir(run_dir):
    """Create (or accept) RUNDIR/replies; return its path or None.

    run_dir is the lexical, launch-validated private staging directory. The
    replies directory is created 0700 with os.mkdir; an existing entry is
    accepted only if lstat shows a real directory (not a symlink) owned by
    this user with no group/other bits. Anything else skips reply files for
    this run with one diagnostic — early results are a convenience and must
    never fail the council. A path containing a line-break character is
    also refused, since it is printed inside single-line progress output.
    """
    path = os.path.join(os.path.normpath(os.path.abspath(run_dir)),
                        REPLIES_SUBDIR)
    if any(ch in path for ch in LINEBREAK_CHARS):
        _diag(
            "[codex-council] reply files disabled: run directory path "
            "contains a line break; the final report is unaffected"
        )
        return None
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError as e:
        _diag(
            f"[codex-council] reply files disabled: cannot create {path!r} "
            f"({_report_inline(e)}); the final report is unaffected"
        )
        return None
    try:
        st = os.lstat(path)
    except OSError as e:
        _diag(
            f"[codex-council] reply files disabled: cannot inspect {path!r} "
            f"({_report_inline(e)}); the final report is unaffected"
        )
        return None
    problem = _private_stat_problem(st, directory=True)
    if problem is not None:
        kind, fragment = problem
        ending = ", not private" if kind == "mode" else ""
        _diag(
            f"[codex-council] reply files disabled: {path!r} "
            f"{fragment}{ending}; the final report is unaffected"
        )
        return None
    return path


# ---------- CLI / entry point ----------

def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description=(
            "Coordinate context-grounded, role-framed Codex collaborators "
            "around a shared implementation, research, or problem-solving "
            "goal. Roles are caller-supplied per invocation via --roles-file; "
            "there is no built-in catalog."
        ),
        epilog=(
            "v1.0.0: --discover RUNDIR records this run's model snapshot "
            "(metadata only; no thread or turn is started), and role "
            "objects accept a 'selection' object next to the optional "
            "'model' and 'effort': mode 'user' for an explicit pin "
            "(forwarded unchanged), 'routed' or 'native_effort' for a "
            "runtime-grounded choice validated against that snapshot and "
            "revalidated at launch. Omit model, effort, and selection to "
            "inherit native Codex configuration. "
            "CODEX_COUNCIL_MODEL_ROUTING=off disables automatic selection. "
            "A model Codex rejects fails the role as [model-rejected] and a "
            "usage or credit limit as [quota]; neither is retried. SKILL "
            "contract epoch 3.\n\n"
            "Direct CLI use: every on-disk input's parent directory must be "
            "private (0700, user-owned, non-symlink) at launch as well as "
            "preflight, e.g. one created by `mktemp -d`, and --discover and "
            "the preflight refuse a directory that already launched. Without "
            "--skill-contract, a model or effort with no 'selection' is "
            "still an explicit user pin. Each settled role's section is also "
            "written to <RUNDIR>/replies/<key>.md before its completion line "
            "(which then ends in ' reply=<path>'); --follow RUNDIR streams "
            "the council's err.log progress."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--roles-file", default=None, metavar="PATH",
        help=(
            "Path to a JSON file holding the role panel: a list of "
            "[{\"id\":..,\"label\":..,\"instruction\":..}] objects, each "
            "optionally with \"model\" (sent as -m), \"effort\" (sent as "
            "-c model_reasoning_effort=...), and a \"selection\" object "
            "declaring their provenance ('user', 'routed', or "
            "'native_effort'); omit all three to inherit native Codex "
            "configuration. Keeping "
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
            "launching Codex, printing one selection-plan line per role. "
            "Automatic selections are checked against DIR/"
            f"{SNAPSHOT_FILENAME} here and revalidated at launch; no "
            "discovery runs. A DIR that already holds a launch (out.md, "
            "err.log, or replies/) is refused: every launch needs its own "
            "directory. Use this after writing the per-run staging files "
            "and before the background council launch."
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
    parser.add_argument(
        "--follow", default=None, metavar="RUNDIR",
        help=(
            "Read-only follower: stream every '[codex-council' line of "
            "RUNDIR/err.log to stdout (one line per event, flushed) and exit "
            "0 after the CODEX_COUNCIL_DONE sentinel, an interruption "
            "line, or a 'runner aborted' line. Exits 3 if no council "
            f"activity appears within {FOLLOW_START_SECS}s and 4 if a "
            "dispatched council's err.log stays byte-silent for "
            f"{FOLLOW_SILENCE_SECS}s (runner presumed gone). "
            "Restarting it re-emits earlier lines. Cannot be combined with "
            "--roles-file, --context-file, --check-staging-dir, or "
            "--discover."
        ),
    )
    parser.add_argument(
        "--discover", default=None, metavar="RUNDIR",
        help=(
            "Metadata-only model discovery for this run: validate RUNDIR as "
            "a private directory (roles.json and context.md need not exist "
            "yet), query `codex app-server` for account type, native "
            "configuration, managed defaults, and the model catalog (no "
            "thread or turn is started; bounded by "
            f"{DISCOVERY_TIMEOUT_SECS}s), write RUNDIR/{SNAPSHOT_FILENAME} "
            "atomically (0600), and print a compact summary. Exits 0 "
            "whenever RUNDIR is valid, even when discovery is unavailable "
            "(130 on Ctrl+C, 1 when stdout is closed); exits 2 when RUNDIR "
            "already holds a launch. "
            "Cannot be combined with --roles-file, --context-file, "
            "--check-staging-dir, or --follow."
        ),
    )
    parser.add_argument(
        "--skill-contract", default=None, type=int, metavar="EPOCH",
        help=(
            "Contract epoch the invoking SKILL text was written against. "
            "Optional (bare/direct invocations stay valid); when present it "
            f"must equal this script's epoch ({SKILL_CONTRACT_EPOCH}), "
            "otherwise the invocation is refused as a stale SKILL/script "
            "pair, and every role's model or effort must declare a "
            "'selection' object."
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
    if args.follow == "":
        parser.error("--follow must be non-empty")
    if args.follow is not None:
        for flag, value in (
            ("--roles-file", args.roles_file),
            ("--context-file", args.context_file),
            ("--check-staging-dir", args.check_staging_dir),
        ):
            if value is not None:
                parser.error(f"--follow cannot be combined with {flag}")
    if args.discover == "":
        parser.error("--discover must be non-empty")
    if args.discover is not None:
        for flag, value in (
            ("--roles-file", args.roles_file),
            ("--context-file", args.context_file),
            ("--check-staging-dir", args.check_staging_dir),
            ("--follow", args.follow),
        ):
            if value is not None:
                parser.error(f"--discover cannot be combined with {flag}")
    if (
        args.skill_contract is not None
        and args.skill_contract != SKILL_CONTRACT_EPOCH
    ):
        parser.error(
            f"--skill-contract {args.skill_contract} does not match this "
            f"script's contract epoch {SKILL_CONTRACT_EPOCH}: stale "
            "SKILL/script pair. Installed plugin: update it from its "
            "marketplace, then reload plugins or start a fresh session. "
            "Development checkout: re-run scripts/dev-link.sh and restart "
            "the session. Never change the epoch to get past it."
        )
    return args


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
    if os.path.islink(path):
        return (
            f"{arg_name}: cannot read {path!r}; symbolic links are not "
            f"accepted for staged inputs ({details})"
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


def _usage_exit_if_codex_missing(
        prefix, next_step="re-run --check-staging-dir and launch."):
    """Exit 2 with an install-method-neutral recovery if codex is absent.

    Shared by the --check-staging-dir preflight and the launch path: a
    missing binary must fail loudly BEFORE a background launch, where the
    error would otherwise surface only inside err.log. PATH is included
    because the failing Bash invocation's PATH is the diagnostic that
    matters — Claude Code Bash calls do not share environment mutations.
    `next_step` (a lowercase clause ending in a period) follows the PATH
    fix: the preflight default re-runs it, and the launch passes its own
    (STAGED_LAUNCH_RESTART for a staged launch, whose directory already
    holds this launch).
    """
    if shutil.which("codex"):
        return
    _usage_exit(
        f"{prefix}Codex CLI not found on PATH for this Bash invocation. "
        "Recovery: make `codex --version` work in the same environment "
        f"that will run the council launch, then {next_step} "
        "Do not rely on PATH changes from a previous Claude "
        "Code Bash call. If Codex is already installed, add its install "
        "directory to PATH (for example /opt/homebrew/bin, /usr/local/bin, "
        "or your npm global bin); otherwise install Codex with your chosen "
        f"install method. Current PATH: {os.environ.get('PATH', '')!r}"
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

    Passing the unrestricted-size panel as a path lets the caller write the
    JSON with a real editor/tool instead of escaping a large blob through the
    shell, where a stray quote or unbalanced brace would break the call.
    Read and decode errors exit 2 like other usage errors; JSON validity is left to
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
    """Reject malformed role IDs with a SystemExit citing context."""
    if not isinstance(rid, str) or not rid:
        _roles_usage_exit(f"--roles-file {ctx}: 'id' must be a non-empty string.")
    if not ROLE_ID_PATTERN.match(rid):
        _roles_usage_exit(
            f"--roles-file {ctx}: id {rid!r} must match {ROLE_ID_PATTERN.pattern}."
        )


def _validate_role_label(label, ctx):
    """Reject labels that can break report structure."""
    if any(ch in label for ch in LINEBREAK_CHARS):
        _roles_usage_exit(
            f"--roles-file {ctx}: label must not contain newlines."
        )


ROLE_FIELDS = ("id", "label", "instruction")
# Optional per-role selection keys; omitting all three inherits Codex's
# native configuration.
OPTIONAL_ROLE_FIELDS = ("model", "effort", "selection")


def _normalize_instruction_list(value, ctx):
    """Join a list-form instruction into one whitespace-normalized paragraph.

    The list form exists because the only production writer of roles.json
    is an LLM using a file-Write tool: multi-kilobyte single-line JSON
    string literals are exactly where such writes corrupt. Sentence-sized
    items on separate physical lines remove that failure surface; the
    script reassembles the paragraph Codex actually sees.
    """
    if not value:
        _roles_usage_exit(
            f"--roles-file {ctx}: instruction list must not be empty "
            "(one sentence per list item)."
        )
    items = []
    for i, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            _roles_usage_exit(
                f"--roles-file {ctx}: instruction list item {i} must be a "
                "non-empty string (one sentence per list item)."
            )
        # split() collapses every Unicode whitespace run, including all
        # LINEBREAK_CHARS, so the joined paragraph is single-line by
        # construction.
        items.append(" ".join(item.split()))
    return " ".join(items)


def _validate_role_instruction(instruction, ctx):
    """Validate the joined instruction paragraph against the role contract.

    Runs on the output of _normalize_instruction_list, which is
    single-line by construction, so no linebreak check is needed here.
    """
    lowered = instruction.lower()
    if REQUIRED_SCOPE_PHRASE not in lowered:
        _roles_usage_exit(
            f"--roles-file {ctx}: instruction must include "
            f"{REQUIRED_SCOPE_PHRASE!r}."
        )
    if not instruction.rstrip().endswith(REQUIRED_CADENCE_SENTENCE):
        _roles_usage_exit(
            f"--roles-file {ctx}: instruction must end with "
            f"{REQUIRED_CADENCE_SENTENCE!r}."
        )


def _parse_roles_json(raw, require_selection=False):
    """Parse the --roles-file blob into a list of Role objects.

    Validates each entry has exactly the id/label/instruction fields plus
    the optional model/effort/selection keys (instruction is a list of
    sentence-sized strings, normalized and joined to one paragraph), id is
    well-formed, model/effort (when present) match SELECTION_VALUE_PATTERN,
    the selection object is well-formed for its mode (see
    _parse_role_selection; require_selection is the skill path, where an
    untagged model or effort is refused), instructions follow the
    documented contract, and ids are unique within the JSON. A key repeated
    at any object level is rejected too: it would hide one value behind
    another. Unknown keys are rejected, not ignored: stray filler fields
    like '"_": ""' are the signature of a corrupted LLM write, so surfacing
    them forces a clean rewrite instead of silently launching from a file
    that already glitched once. Every validation defect carries the same
    full-rewrite recovery (ROLES_REWRITE_RECOVERY, or the staged launch's
    new-directory form it scopes in with _roles_recovery). Whether an automatic
    selection is supported by discovery evidence is checked separately
    (_validate_selection_authoring).
    """
    try:
        data = _strict_json_loads(raw)
    except (ValueError, RecursionError) as e:
        _roles_usage_exit(f"--roles-file: invalid JSON ({e}).")
    if not isinstance(data, list):
        _roles_usage_exit("--roles-file: top-level value must be a JSON list.")
    if not data:
        _roles_usage_exit("--roles-file: role panel must not be empty.")
    roles = []
    seen = set()
    for idx, entry in enumerate(data):
        ctx = f"entry {idx}"
        if not isinstance(entry, dict):
            _roles_usage_exit(f"--roles-file {ctx}: each entry must be an object.")
        unknown = sorted(
            set(entry) - set(ROLE_FIELDS) - set(OPTIONAL_ROLE_FIELDS)
        )
        if unknown:
            _roles_usage_exit(
                f"--roles-file {ctx}: unknown field(s) "
                f"{', '.join(repr(k) for k in unknown)}. Each role object "
                "must have exactly 'id', 'label', and 'instruction', plus "
                "optionally 'model', 'effort', and 'selection' — no filler "
                "keys."
            )
        for field in ROLE_FIELDS:
            if field not in entry:
                _roles_usage_exit(f"--roles-file {ctx}: missing field {field!r}.")
            value = entry[field]
            if field == "instruction":
                if not isinstance(value, list):
                    _roles_usage_exit(
                        f"--roles-file {ctx}: field 'instruction' must be a "
                        "JSON array of non-empty strings, one sentence per "
                        "item."
                    )
                continue
            if not isinstance(value, str) or not value.strip():
                _roles_usage_exit(
                    f"--roles-file {ctx}: field {field!r} must be a non-empty string."
                )
        rid = entry["id"]
        label = entry["label"]
        instruction = _normalize_instruction_list(entry["instruction"], ctx)
        _validate_role_id(rid, ctx)
        _validate_role_label(label, ctx)
        _validate_role_instruction(instruction, ctx)
        model, effort, selection = _parse_role_selection(
            entry, ctx, require_selection
        )
        if rid in seen:
            _roles_usage_exit(
                f"--roles-file {ctx}: duplicate id {rid!r} within JSON payload."
            )
        seen.add(rid)
        roles.append(Role(rid, label, instruction, model, effort, selection))
    return roles


def _resolve_roles(custom_roles):
    """Validate and return the list of caller-supplied Role objects.

    Errors if empty. Deduplicates by id preserving first occurrence —
    defense in depth; `_parse_roles_json` already rejects duplicate ids
    within a single JSON payload. Panel size is unrestricted.
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

    return ordered


def _read_body_or_problem(stream):
    """Read an unrestricted prompt body from a binary stream.

    The plugin imposes no byte ceiling and never truncates. Decoding remains
    strict UTF-8 because a prompt is text; invalid bytes are a clear error
    rather than being silently replaced.

    Returns (body, None) on success or (None, (kind, detail)) where kind
    is "not-utf8" or "empty". Exit codes are the CALLER's decision: stdin
    defects are runtime input errors (exit 1), while a staged context.md
    defect is a usage/staging error (exit 2) like every other staging defect.
    """
    raw = stream.read()
    try:
        body = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        return None, ("not-utf8", e)
    if not body.strip():
        return None, ("empty", None)
    return body, None


def _read_stdin_body(stream):
    """Read the prompt body from stdin; defects exit 1 (runtime input)."""
    body, problem = _read_body_or_problem(stream)
    if problem is None:
        return body
    kind, detail = problem
    if kind == "not-utf8":
        print(f"Input is not valid UTF-8 ({detail}) — pipe text.", file=sys.stderr)
    else:
        print("Empty input — pipe a complete prompt instead.", file=sys.stderr)
    sys.exit(1)


def _read_context_file(path, staged_launch=False):
    """Read a staged context file; content defects are usage errors (exit 2).

    Empty and non-UTF-8 staged context files exit 2 like every other staging
    defect, so 'exit 2 = fix the staged inputs' holds uniformly; exit 1
    stays for runtime failures (stdin defects, every role failing). The
    preflight's recovery re-runs it; the launch passes `staged_launch`,
    whose recovery starts over in a new directory, since the launch's own
    redirections already made this one a launched directory the preflight
    refuses. There is no plugin-imposed context size ceiling.
    """
    problem = _file_arg_problem("--context-file", path)
    if problem:
        _usage_exit(f"{problem}. {STAGING_PATH_HINT}")
    try:
        with open(path, "rb") as f:
            body, body_problem = _read_body_or_problem(f)
    except OSError as e:
        _usage_exit(f"--context-file: cannot read {path!r} ({e}). {STAGING_PATH_HINT}")
    if body_problem is None:
        return body
    kind, detail = body_problem
    if kind == "not-utf8":
        problem = f"is not valid UTF-8 ({detail})"
        fix = "as UTF-8 text"
    else:
        problem = "is empty or whitespace-only"
        fix = ("with the decision-complete working context or a "
               "self-contained question")
    if staged_launch:
        recovery = f"{STAGED_LAUNCH_RESTART} Write the new context.md {fix}."
    else:
        recovery = (
            f"rewrite context.md {fix}, then re-run --check-staging-dir."
        )
    _usage_exit(
        f"--context-file: Context file {path!r} {problem}. "
        f"Recovery: {recovery}"
    )


def _usage_exit_unless_parent_private(arg_name, path, recovery):
    """Launch-side privacy gate on a staged input's LEXICAL parent.

    dirname(abspath(...)) on purpose — never realpath before the check:
    resolving first would launder a symlink parent into its (possibly
    private) target before _check_private_dir lstats it. Realpath
    comparison happens only AFTER both lexical parents validate (the
    same-directory rule).
    """
    parent = os.path.dirname(os.path.abspath(path)) or "."
    _check_private_dir(parent, prefix=f"{arg_name}: ", recovery=recovery)


def _check_staging_dir(path, require_selection=False):
    """Validate the per-run staging dir before launching Codex.

    A directory that already holds a launch (out.md, err.log, or replies/)
    is refused first: the launch command's redirections would truncate a
    running council's files before the runner could object. Every check
    the launch makes before dispatch runs here too, so a
    directory, roles file, context, missing codex binary, or environment
    override (CODEX_COUNCIL_MAX_PARALLEL, CODEX_COUNCIL_STALL_SECS,
    CODEX_COUNCIL_MODEL_ROUTING) the launch would refuse never reports
    "staging OK". Prints the staging-OK line, then one selection-plan line
    per role. No discovery runs here: automatic selections are validated
    against this run's planning snapshot (DIR/model-snapshot.json) through
    the orchestration the launch uses (_resolve_run_selections), and are
    revalidated by a fresh discovery at launch. require_selection is the
    skill path (--skill-contract was passed).
    """
    if path == "":
        _usage_exit("--check-staging-dir must be non-empty.")
    path = _check_private_dir(path)
    _usage_exit_if_launched(path, "--check-staging-dir: ")
    roles_path = os.path.join(path, "roles.json")
    context_path = os.path.join(path, "context.md")
    _usage_exit_if_file_arg_problems(
        ("--roles-file", roles_path),
        ("--context-file", context_path),
    )
    roles = _resolve_roles(
        _parse_roles_json(_read_roles_file(roles_path), require_selection)
    )
    _read_context_file(context_path)
    # The codex binary is the one hard external dependency; a preflight
    # that says "staging OK" while codex is missing defers the failure to
    # a background launch whose error lands only in err.log.
    _usage_exit_if_codex_missing("--check-staging-dir: ")
    max_parallel = _max_parallel_roles()
    _stall_secs()  # the launch refuses an invalid watchdog override (exit 2)
    routing_mode = _model_routing_mode()
    roles, _ = _resolve_run_selections(
        roles, path, routing_mode, at_launch=False
    )
    print(
        f"[codex-council] staging OK: {os.path.abspath(path)} "
        f"({len(roles)} roles; max parallel {max_parallel}) "
        f"version={_plugin_version()}"
    )
    for role in roles:
        print(_report_inline(
            f"[codex-council] selection plan: {role.id}: "
            f"{_selection_plan_text(role.decision)}"
        ))


# Recovery for a rejected --follow directory. The follower only reads, so
# the fix is always "point it at the real run directory", never chmod/mkdir.
FOLLOW_DIR_RECOVERY = (
    "Recovery: pass the exact absolute mktemp directory the council was "
    "launched from (the directory holding roles.json, out.md, and err.log). "
    "--follow only reads DIR/err.log; do not chmod or mkdir anything for it."
)


def _follow_emit(line):
    """Print one follower event (one stdout line = one Monitor event).

    A dead stdout means nobody is listening any more: stop quietly with
    exit 1 rather than raising a traceback.
    """
    _print_stdout(line)


def _follow_open(log_path):
    """Open err.log read-only if it exists; None while it does not.

    O_NONBLOCK so a FIFO planted at the path cannot hang the open, and
    O_NOFOLLOW so a symlink is refused; anything but a regular file is a
    usage error (the run directory is not a council run directory).
    """
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(log_path, flags)
    except FileNotFoundError:
        return None
    except OSError as e:
        _usage_exit(
            f"--follow: cannot open {log_path!r} ({e.strerror or e}). "
            f"{FOLLOW_DIR_RECOVERY}"
        )
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        _usage_exit(
            f"--follow: {log_path!r} is not a regular file. "
            f"{FOLLOW_DIR_RECOVERY}"
        )
    return fd


def _follow_reply_path_ok(line, replies_dir):
    """True unless the line names a reply= path outside replies_dir.

    Lexical only: the runner always prints abspath(RUNDIR)/replies/<key>.md,
    so any other shape was not written by the runner. This keeps a forged
    line from pointing Claude at an arbitrary file; it cannot authenticate
    a same-uid writer (see _follow).
    """
    marker = line.rfind(REPLY_MARKER)
    if marker < 0:
        return True
    path = line[marker + len(REPLY_MARKER):]
    return (
        os.path.normpath(path) == path
        and os.path.dirname(path) == replies_dir
        and path.endswith(".md")
    )


def _follow(run_dir):
    """Stream a council's err.log progress lines; return the exit code.

    Read-only by construction: it opens nothing for writing and relays only
    complete lines that begin with "[codex-council". Reply text never
    reaches err.log and runner diagnostics escape codex-controlled text, so
    role output cannot forge a line through the runner; but roles run
    unsandboxed as the same user and can append to err.log directly, which
    no same-uid check can authenticate. The follower therefore drops any
    completion line whose reply= path is not directly inside this run's
    replies/ directory, and SKILL.md bases the final verdict on the tracked
    background-task completion rather than on the sentinel. Every run
    re-reads err.log from the start, so a re-armed Monitor re-emits earlier
    lines; consumers dedupe.

    Exit 0 after printing the CODEX_COUNCIL_DONE sentinel, an interruption
    line, or a "runner aborted" line. Exit 3 (no council activity) when
    err.log is absent, or holds no dispatch line, FOLLOW_START_SECS after
    the follower started — a typo'd path or a launch that failed validation
    must not leave a Monitor silently stuck. Exit 4 when a dispatched
    council's err.log has been byte-silent for FOLLOW_SILENCE_SECS (measured
    from the file mtime, so it survives re-arming; a detected system suspend
    restarts the count): a live runner heartbeats far more often, so the
    runner is presumed dead. A Python traceback in err.log is reported once
    as an advisory event but is not terminal, since the runner can log a
    traceback and keep going. Usage errors exit 2.
    """
    run_dir = _check_private_dir(
        run_dir, prefix="--follow: ", recovery=FOLLOW_DIR_RECOVERY
    )
    log_path = os.path.join(run_dir, "err.log")
    # Same construction as _prepare_replies_dir, so genuine lines match.
    replies_dir = os.path.join(
        os.path.normpath(os.path.abspath(run_dir)), REPLIES_SUBDIR
    )
    started = time.monotonic()
    fd = None
    inode = None
    offset = 0
    pending = b""
    dispatched = False
    traceback_noted = False
    # Silence is measured on the wall clock (err.log mtime), but the
    # runner's heartbeat sleeps on the monotonic clock, which stops while
    # the machine is suspended. After a detected suspend, count silence
    # from the resume instead of from the pre-suspend mtime.
    silence_floor = 0.0
    last_wall = time.time()
    last_mono = time.monotonic()
    try:
        while True:
            now_wall = time.time()
            now_mono = time.monotonic()
            if (now_wall - last_wall) - (now_mono - last_mono) > (
                FOLLOW_SUSPEND_SLACK_SECS
            ):
                silence_floor = now_wall
            last_wall, last_mono = now_wall, now_mono
            if fd is None:
                fd = _follow_open(log_path)
                if fd is not None:
                    inode = os.fstat(fd).st_ino
                    offset = 0
                    pending = b""
            else:
                # A relaunch into the same directory replaces or truncates
                # err.log; start over on the new content.
                try:
                    current = os.lstat(log_path)
                except FileNotFoundError:
                    current = None
                if current is not None and current.st_ino != inode:
                    os.close(fd)
                    fd = None
                    continue
                if os.fstat(fd).st_size < offset:
                    os.lseek(fd, 0, os.SEEK_SET)
                    offset = 0
                    pending = b""
            if fd is not None:
                while True:
                    data = os.read(fd, _READ_CHUNK_BYTES)
                    if not data:
                        break
                    offset += len(data)
                    # Only complete lines: a read can land mid-write.
                    lines = (pending + data).split(b"\n")
                    pending = lines.pop()
                    for raw in lines:
                        line = raw.decode("utf-8", errors="replace")
                        line = line.rstrip("\r")
                        if (
                            not traceback_noted
                            and line.startswith(
                                "Traceback (most recent call last):"
                            )
                        ):
                            traceback_noted = True
                            _follow_emit(
                                "[codex-council-follow] err.log shows a "
                                "Python traceback; the runner may have "
                                f"crashed — read {log_path}"
                            )
                            continue
                        if not line.startswith(FOLLOW_LINE_PREFIX):
                            continue
                        if not _follow_reply_path_ok(line, replies_dir):
                            continue
                        _follow_emit(line)
                        if line.startswith(FOLLOW_DISPATCH_PREFIX):
                            dispatched = True
                        if (
                            FOLLOW_DONE_PATTERN.match(line)
                            or FOLLOW_INTERRUPTED_PATTERN.match(line)
                            or FOLLOW_ABORTED_PATTERN.match(line)
                        ):
                            return 0
            if not dispatched:
                if time.monotonic() - started >= FOLLOW_START_SECS:
                    if fd is None:
                        detail = (
                            f"{log_path} did not appear within "
                            f"{FOLLOW_START_SECS}s; check the run directory "
                            "path"
                        )
                    else:
                        detail = (
                            f"{log_path} has no dispatch line after "
                            f"{FOLLOW_START_SECS}s; the launch may have "
                            "failed — read err.log"
                        )
                    _follow_emit(
                        f"[codex-council-follow] no council activity: {detail}"
                    )
                    return FOLLOW_EXIT_NO_ACTIVITY
            elif fd is not None:
                silent = time.time() - max(
                    os.fstat(fd).st_mtime, silence_floor
                )
                if silent >= FOLLOW_SILENCE_SECS:
                    _follow_emit(
                        "[codex-council-follow] runner presumed gone: "
                        f"{log_path} silent for {silent:.0f}s with no "
                        "CODEX_COUNCIL_DONE line — check the background "
                        "task and err.log"
                    )
                    return FOLLOW_EXIT_RUNNER_GONE
            time.sleep(FOLLOW_POLL_SECS)
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)


async def _run_council_with_signals(roles, body, max_parallel, replies_dir=None):
    """Run the council and translate POSIX termination signals into cleanup."""
    loop = asyncio.get_running_loop()
    council_task = asyncio.create_task(
        run_council(
            roles, body, max_parallel=max_parallel, replies_dir=replies_dir
        )
    )
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


def _force_utf8_streams():
    """Pin stdout/stderr to UTF-8 regardless of the process locale.

    The report header and role replies carry non-ASCII text (e.g. the em dash
    in "# Codex Council — N/M"). Under a strict C locale with UTF-8 mode and
    C-locale coercion both disabled (LC_ALL=C PYTHONUTF8=0 PYTHONCOERCECLOCALE=0),
    the default stream encoding is ASCII, so printing the report raises
    UnicodeEncodeError and loses BOTH the report and the CODEX_COUNCIL_DONE
    sentinel the recovery contract depends on. The report and prompts are UTF-8
    by contract, so make the streams agree. errors="replace" guarantees the
    sentinel still writes even if some field is not encodable.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main():
    _force_utf8_streams()
    args = _parse_args(sys.argv[1:])
    # --skill-contract marks the skill path, where every model or effort
    # must declare its selection mode.
    require_selection = args.skill_contract is not None

    if args.check_staging_dir is not None:
        _check_staging_dir(args.check_staging_dir, require_selection)
        return

    if args.discover is not None:
        try:
            _discover_command(args.discover)
        except KeyboardInterrupt:
            # The app-server group was already torn down by its session's
            # finally; no new snapshot was written.
            _diag("[codex-council] --discover interrupted by user")
            sys.exit(130)
        return

    if args.follow is not None:
        try:
            code = _follow(args.follow)
        except KeyboardInterrupt:
            code = 130
        sys.exit(code)

    # Launch-side privacy gate: validate each on-disk input's LEXICAL parent
    # BEFORE any content read or parse — a public directory holding bad roles
    # must produce "abandon this exposed directory", never "rewrite roles".
    # In stdin mode only roles.json is on disk; piped context has no directory
    # and is validated below as UTF-8/non-empty only.
    if args.roles_file is not None:
        recovery = (
            STAGING_DIR_RECOVERY if args.context_file is not None
            else STDIN_DIR_RECOVERY
        )
        _usage_exit_unless_parent_private("--roles-file", args.roles_file, recovery)
    if args.context_file is not None:
        _usage_exit_unless_parent_private(
            "--context-file", args.context_file, STAGING_DIR_RECOVERY
        )
    _usage_exit_if_staging_dirs_differ(args.roles_file, args.context_file)
    _usage_exit_if_file_arg_problems(
        ("--roles-file", args.roles_file),
        ("--context-file", args.context_file),
    )

    # A staged launch's own redirections created out.md and err.log before
    # it started, so the pre-flight now refuses its directory: every
    # refusal from here to dispatch starts over in a new directory instead
    # of asking for a pre-flight re-run in this one.
    staged = args.context_file is not None
    roles_recovery = (
        STAGED_LAUNCH_ROLES_RECOVERY if staged else ROLES_REWRITE_RECOVERY
    )

    # Parse and validate staged inputs before requiring Codex. This catches
    # temp-path mismatches without launching or depending on any Codex state.
    if args.roles_file is not None:
        with _roles_recovery(roles_recovery):
            custom_roles = _parse_roles_json(
                _read_roles_file(args.roles_file), require_selection
            )
    else:
        custom_roles = []
    roles = _resolve_roles(custom_roles)

    if staged:
        body = _read_context_file(args.context_file, staged_launch=True)
    else:
        if sys.stdin.isatty():
            print(
                "No input piped. Usage: echo 'context' | "
                "python3 codex_council.py --roles-file roles.json",
                file=sys.stderr,
            )
            sys.exit(1)
        body = _read_stdin_body(sys.stdin.buffer)

    _usage_exit_if_codex_missing(
        "", STAGED_LAUNCH_RESTART if staged else "re-run the direct command."
    )
    max_parallel = _max_parallel_roles()
    _stall_secs()  # fail fast on an invalid watchdog override (usage exit 2)
    routing_mode = _model_routing_mode()

    # The run directory is the launch-validated private directory of the
    # on-disk inputs (context.md's in staged mode, roles.json's in stdin
    # mode — the same directory when both exist). Lexical parent, as
    # validated above. It holds the planning snapshot and the reply files.
    run_dir = os.path.dirname(os.path.abspath(
        args.context_file if args.context_file is not None else args.roles_file
    ))
    # Authoring defects exit 2 here, before any worker; automatic
    # selections are then revalidated by one fresh discovery, and every
    # role gets the decision its commands are built from.
    try:
        with _roles_recovery(roles_recovery):
            roles, launch = _resolve_run_selections(
                roles, run_dir, routing_mode, at_launch=True
            )
    except KeyboardInterrupt:
        _diag("\n[codex-council] interrupted by user")
        sys.exit(130)
    state, reason = _launch_discovery_state(
        routing_mode, any(_is_automatic(r) for r in roles), launch
    )
    # A problem here only disables reply files.
    replies_dir = _prepare_replies_dir(run_dir)

    _diag(
        f"[codex-council] dispatching {len(roles)} roles "
        f"with max parallel {max_parallel} "
        f"({', '.join(r.id for r in roles)}); version={_plugin_version()}."
    )
    for line in _model_selection_lines(roles, routing_mode, state, reason):
        _diag(line)

    started = time.monotonic()
    try:
        results, signum = asyncio.run(
            _run_council_with_signals(
                roles, body, max_parallel, replies_dir=replies_dir
            )
        )
    except KeyboardInterrupt:
        _diag("\n[codex-council] interrupted by user")
        sys.exit(130)
    except Exception as e:
        # Leave a terminal line so --follow stops instead of waiting out
        # the silence threshold; the traceback follows on stderr.
        _diag(
            "\n[codex-council] runner aborted exit=1: unhandled "
            f"{type(e).__name__}; no report was written"
        )
        raise
    if signum is not None:
        signame = signal.Signals(signum).name
        _diag(f"\n[codex-council] interrupted by {signame}")
        sys.exit(128 + int(signum))

    elapsed = time.monotonic() - started
    try:
        print(_format_report(
            results, elapsed, _discovery_sentence(state, reason, launch)
        ), end="")
        sys.stdout.flush()
    except (OSError, ValueError):
        # stdout is dead: the report was not delivered, so no sentinel may
        # claim it was. Close stdout so an interpreter-shutdown flush of the
        # broken stream cannot rewrite the exit code (never exit 120).
        with contextlib.suppress(Exception):
            sys.stdout.close()
        _diag(
            "[codex-council] runner aborted exit=1: stdout unavailable; "
            "the report was not delivered"
        )
        sys.exit(1)

    successes = sum(1 for r in results if r.ok)
    total = len(results)
    exit_code = 0 if successes else 1

    # Final, uniquely-shaped, LAST stderr line. Its presence means the stdout
    # report is fully written; it carries the exit status so a lost/orphaned but
    # redirected run is fully recoverable from `err.log` (tail until this line).
    # Best-effort by design: a dead stderr loses the sentinel but must not
    # change the exit code.
    _diag(
        f"[codex-council] CODEX_COUNCIL_DONE ok={successes} total={total} "
        f"elapsed={elapsed:.1f}s exit={exit_code} version={_plugin_version()}"
    )

    if exit_code:
        sys.exit(1)


if __name__ == "__main__":
    main()
