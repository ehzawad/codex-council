#!/usr/bin/env python3
"""Coordinate an adaptive, context-driven council of Codex agents.

Each role runs in its own `codex exec` subprocess with a distinct
framing instruction. Sessions are isolated per (project, host session,
role) when a terminal/session identifier is available, and persist
across calls from that host session: a role id used again resumes its
thread, a new id starts fresh, and a saved thread Codex no longer has
restarts the role fresh with a warning in its result.

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
disk survive an interruption. The launch also publishes RUNDIR/status.json
(runner identity and state, a tick at least every 15s, per-role state and
codex process group). `--follow RUNDIR` is a read-only follower for a
Claude Code Monitor: it relays the actionable `[codex-council` lines of
RUNDIR/err.log and reports a runner that is gone or stopped ticking;
`--status RUNDIR` prints a short snapshot and `--reap RUNDIR` ends the codex
process groups of a runner that is gone (see council_liveness.py).

Detached launch: `--start RUNDIR` validates the staged directory exactly as
`--check-staging-dir` does, claims it atomically (RUNDIR/supervisor.lock,
err.log, and out.md, each created exclusively and 0600), and starts the
unchanged staged launch as a supervisor in its own session, with stdout on
out.md and stderr on err.log. That supervisor holds the lock for its whole
life and writes RUNDIR/supervisor.json about itself before any other work;
`--start` returns once it has, so the council runs outside any host
background task. `--cancel RUNDIR` stops a verified supervisor.

One launch per RUNDIR: `--discover`, `--check-staging-dir`, and `--start`
refuse a directory that already holds out.md, err.log, replies/, or a
supervisor file, because a tracked launch command's own redirections would
truncate a running council's files before this script could object.

Usage:
    python3 codex_council.py --discover RUNDIR
    python3 codex_council.py --check-staging-dir RUNDIR
    python3 codex_council.py --start RUNDIR
    python3 codex_council.py --roles-file roles.json --context-file context.md
    python3 codex_council.py --follow RUNDIR [--verbose]
    python3 codex_council.py --status RUNDIR
    python3 codex_council.py --cancel RUNDIR
    python3 codex_council.py --reap RUNDIR

Env vars:
    CODEX_COUNCIL_SESSION_KEY     explicit council thread scope override
    CODEX_COUNCIL_MAX_PARALLEL    positive active-role concurrency limit
                                   (default 6)
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
already completed, terminal otherwise. Setting 0 permits an indefinitely
silent role. Ctrl+C tears down every in-flight codex process group. Each
codex process group belongs to one attempt: after codex exits its pipes get
a bounded drain, and whatever is left in the group is then terminated. The
group signals reach codex and any child that stays in its group; current
codex starts each tool command in its own session and each MCP server in
its own process group, so a tool command still running when codex is
terminated keeps running until it ends.

The optional `--skill-contract <int>` flag pins the SKILL/script contract
epoch: absent it is ignored; present it must equal this script's epoch or
the launch is refused as a stale SKILL/script pair. A model or effort
without a `selection` object is refused on every path. The --discover
summary's first line and the staging-OK, dispatch, heartbeat, and
CODEX_COUNCIL_DONE lines carry `version=<plugin version>` for postmortem
visibility (it does not prevent skew; the contract epoch does).

This file is the only entry point. It imports the sibling modules in
its directory: council_common.py (shared primitives),
council_discovery.py (--discover and the model snapshot),
council_selection.py (Role, the `selection` contract, and its resolver),
council_failures.py (failure classification), and council_liveness.py
(status.json, process identity, --follow, --status, and --reap).

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
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass

# python3 -P and PYTHONSAFEPATH=1 leave this script's directory off
# sys.path; put it first (once) so the sibling modules below resolve.
_SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
if sys.path[:1] != [_SCRIPT_DIR]:
    sys.path.insert(0, _SCRIPT_DIR)

# A run writes nothing into the installed plugin directory (Python never
# caches bytecode for the script it runs), so import the siblings with
# bytecode writes off too.
_DONT_WRITE_BYTECODE = sys.dont_write_bytecode
sys.dont_write_bytecode = True
from council_common import (  # noqa: E402
    _READ_CHUNK_BYTES,
    LAUNCHED_DIR_RECOVERY,
    LINEBREAK_CHARS,
    REPLIES_SUBDIR,
    REPLY_MARKER,
    ROLES_REWRITE_RECOVERY,
    STAGED_LAUNCH_RESTART,
    STAGED_LAUNCH_ROLES_RECOVERY,
    STAGING_DIR_RECOVERY,
    SUPERVISOR_FILENAME,
    SUPERVISOR_LOCK_FILENAME,
    _atomic_write_private,
    _check_private_dir,
    _dedupe_preserve_order,
    _diag,
    _iter_json_objects,
    _log_inline,
    _plugin_version,
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
from council_liveness import (  # noqa: E402
    FOLLOW_START_SECS,
    STATUS_FILENAME,
    STATUS_TICK_SECS,
    TICK_GIVE_UP_SECS,
    TICK_WARN_SECS,
    RunStatus,
    cancel_command,
    descendant_targets,
    follow,
    read_supervisor,
    reap_command,
    signal_targets,
    status_command,
    supervisor_record,
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
RETRY_BACKOFF_SECS = 5
TERMINATION_GRACE_SECS = 0.2
# After codex exits, its pipes get this long to reach EOF; a descendant still
# holding them then gets the attempt's process group terminated.
POST_EXIT_DRAIN_SECS = 10
POST_EXIT_DRAIN_WARNING = (
    "codex exited but its process group kept its output open; the group was "
    "terminated"
)
# An output pump or the prompt writer ended with an exception (named in {}):
# output may be missing, so the attempt is never treated as replay-safe.
IO_FAILED_WARNING = (
    "an output reader or the prompt writer failed ({}); the output may be "
    "incomplete, so a stall is not retried"
)
# A resume whose saved thread Codex no longer has reruns the role fresh
# with the same prompt; the role's result says its earlier turns are gone.
STALE_RESUME_WARNING = (
    "saved Codex thread unavailable; started fresh with the current context "
    "(prior continuity lost)"
)
# How often an attempt looks for its codex process's exit while the pipes
# are still open (Process.wait() may also wait for them; see _process_exit).
EXIT_POLL_SECS = 0.1
LOCK_PROBE_INITIAL_BACKOFF_SECS = 0.1
LOCK_PROBE_MAX_BACKOFF_SECS = 2.0
DEFAULT_MAX_PARALLEL = 6
MAX_PARALLEL_ENV = "CODEX_COUNCIL_MAX_PARALLEL"
STALL_SECS_ENV = "CODEX_COUNCIL_STALL_SECS"
DEFAULT_STALL_SECS = 1800
# The err.log heartbeat's cadence, whatever CODEX_COUNCIL_STALL_SECS is
# (enabled, disabled with 0, or very large): a long council shows progress
# at least every five minutes. The heartbeat is advisory; it never resets a
# role's watchdog. At the default watchdog (1800s) a silent role gets five
# heartbeats with a rising quiet value before the watchdog fires.
PROGRESS_HEARTBEAT_SECS = 300
# Contract epoch for the optional --skill-contract handshake. Bump only when
# SKILL.md's launch/preflight command contract changes incompatibly (3: the
# `selection` object and --discover; 4: the detached --start launch with
# --cancel, and the completion rule that a detached run has ended only once
# its supervisor lock is free and its runner identity is gone).
SKILL_CONTRACT_EPOCH = 4
# --start waits at most this long for its supervisor to write
# supervisor.json (or exit early). It bounds the start command only, never
# a running council.
START_WAIT_SECS = 10
START_POLL_SECS = 0.05
REQUIRED_SCOPE_PHRASE = "nothing material"
REQUIRED_CADENCE_SENTENCE = "Thoroughness beats speed."
# Count-neutral on purpose: a council may have exactly one role. Verifier
# framing: the user's requirements are authoritative, while Claude's account
# of the work is a set of claims to check against the workspace. A resumed
# role also holds its earlier turns, so the brief ranks them below the
# current shared context and the workspace. The role instruction bookends
# the prompt (see _compose_prompt), so this brief stays short and the
# lens-specific instruction is the last thing the model reads.
COLLABORATION_BRIEF = (
    "You are working as one role in a Claude-orchestrated Codex council, an "
    "independent cross-model check on Claude Code's work; you may be the "
    "only role, or one of several covering other lenses in parallel. In the "
    "shared working context below, the user's goal, requirements, and "
    "constraints are authoritative; Claude's account of the project state, "
    "its conclusions, and what has already been tried are claims to verify "
    "against the workspace, not facts to accept. If this conversation "
    "already holds earlier turns, treat them as background; where they "
    "conflict with the shared working context below or the workspace, the "
    "current context and workspace win. This run is "
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
# STAGING_PATH_HINT's staged-launch form, for an input or path defect found
# before the launch reads its inputs (a missing, unreadable, or misplaced
# roles.json or context.md). The launch command's own redirections already
# created out.md and err.log, so the fix goes into a NEW directory, exactly
# as for every later staged-launch refusal; the pre-flight and stdin mode
# keep the plain hint.
STAGED_LAUNCH_PATH_HINT = (
    f"{STAGING_PATH_HINT} Recovery: {STAGED_LAUNCH_RESTART}"
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


@dataclass
class RoleResult:
    """One role's outcome. `retriable` is the retry decision for a failed
    attempt (a rate limit, a 5xx, or a replay-safe stall), set from the
    structured verdict and never inferred from the error text."""
    role: Role
    ok: bool
    text: str | None = None
    error: str | None = None
    elapsed_seconds: float = 0.0
    attempts: int = 1
    warning: str | None = None
    retriable: bool = False


@dataclass
class CodexRun:
    """Structured outcome of one codex subprocess attempt.

    `stalled` is the watchdog verdict and outranks any text classification of
    stdout/stderr. `turn_completed` / `unsafe_to_replay` are derived from the
    buffered JSONL events so the stall policy can tell a wedged shutdown from
    an interrupted turn, and a replay-safe attempt from one whose tool work
    may have had side effects. `warning` notes what the role's result
    should carry: a process-group cleanup (POST_EXIT_DRAIN_WARNING) or a
    failed output reader or prompt writer (IO_FAILED_WARNING).
    """
    returncode: int | None
    stdout: str
    stderr: str
    stalled: bool = False
    turn_completed: bool = False
    unsafe_to_replay: bool = False
    warning: str | None = None


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
    """Heartbeat cadence: PROGRESS_HEARTBEAT_SECS for every watchdog value.

    Independent of CODEX_COUNCIL_STALL_SECS on purpose: a disabled or very
    long watchdog must not make a long council's err.log go quiet.
    """
    del stall_secs
    return PROGRESS_HEARTBEAT_SECS


# The run's live state: role transitions from the scheduler and retry loop,
# codex process groups and last-output stamps from the subprocess pumps. The
# heartbeat reads it, and on the launch path it is published as status.json.
# Module-level because run_council and the pumps are far apart; same-role
# concurrency is already excluded by the continuity lock.
_RUN = RunStatus()
# A detached runner's supervisor.lock descriptor (see _become_supervisor):
# held open, and so locked, for the runner's whole life, and never
# inherited by a codex worker.
_SUPERVISOR = {"lock_fd": None}


# \Z, not $: in Python `$` also matches just before a trailing "\n", so
# "architect\n" would pass and inject a newline into state filenames, the
# report summary line, and stderr progress. \Z anchors the true end of string.
ROLE_ID_PATTERN = re.compile(r"^[a-z0-9_-]+\Z")


def _state_role_component(role_id):
    """Return a filename-safe, bounded component for an unrestricted role ID.

    An ID of 32 characters or fewer is the component itself, so state and
    reply filenames stay readable; a longer ID is hashed to a fixed-size
    component so the filename never exceeds the platform's per-component
    limit. Used for both the continuity state file and the per-role reply
    file.
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


def _session_key():
    """The key scoping council threads: CODEX_COUNCIL_SESSION_KEY, else the
    detected host session, else "" (project-wide state)."""
    explicit = os.environ.get(SESSION_KEY_ENV, "").strip()
    if explicit:
        return explicit
    return _auto_session_key()


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
    return DEFAULT_MAX_PARALLEL


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
    """State path for (project, session key, role); see _project_key."""
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
    except (OSError, ValueError, RecursionError):
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
    execution context. Placed with `-C` BEFORE any `resume` subcommand,
    where parent-placed `-m`/`-c` apply to both fresh and resumed turns.
    The overrides are per-invocation, not sticky: a resumed turn without
    them runs on the current native configuration, not the thread's
    recorded model. Both values match
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
    lists, collab tool calls, any unknown/future type, and a type that is
    not a string) marks the attempt unsafe to replay — conservative by
    default, since replaying such a turn could duplicate side effects. A
    non-blank line that is not a JSON object (it does not decode, is nested
    too deeply, holds an out-of-range number, or is another JSON value)
    could hide such an item, so it marks the attempt unsafe to replay too,
    and so does anything else scanning a line raises: feed() and finish()
    never raise, so scanning never stops the stdout pump.
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
            replay_safe = self._replay_safe(json.loads(line))
        except Exception:
            # Undecodable, or any surprise at all: unknown work.
            replay_safe = False
        if not replay_safe:
            self.unsafe_to_replay = True

    def _replay_safe(self, event):
        """False when `event` is, or could hide, side-effect-capable work."""
        if not isinstance(event, dict):
            return False
        event_type = event.get("type")
        if event_type == "turn.completed":
            self.turn_completed = True
        if event_type not in ("item.started", "item.completed"):
            return True
        item = event.get("item")
        item_type = item.get("type") if isinstance(item, dict) else None
        return isinstance(item_type, str) and item_type in self._SAFE_ITEM_TYPES


async def _run_codex_subprocess(cmd, prompt, role_id=""):
    """Run codex exec async with incremental readers and a stall watchdog.

    start_new_session=True puts codex in its own process group, which
    belongs to this attempt alone. The group signals reach codex and any
    child that stays in that group, not a tool command or MCP server that
    codex starts in its own session or group. Once codex exits, its pipes
    get POST_EXIT_DRAIN_SECS to reach EOF; if they are still open, the group
    is terminated and the pumps stop, and the output already read is kept
    with POST_EXIT_DRAIN_WARNING. Either way the group is swept when the
    attempt ends. A pump or the prompt writer that ended with an exception
    makes the attempt unsafe to replay, with IO_FAILED_WARNING.

    Returns a CodexRun. All termination paths — the watchdog, the drain
    bound, outer cancellation, and any post-spawn failure — converge on one
    idempotent termination task, so duplicate teardowns never race.
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
    _RUN.spawned(role_id, proc.pid, pgid)

    scanner = _EventFlagScanner()
    stdout_buf = bytearray()
    stderr_buf = bytearray()
    # Shared last-activity stamp: any byte on either stream resets the
    # watchdog. Raw bytes are buffered per stream and decoded once after the
    # pumps join, so a UTF-8 sequence split across chunks survives.
    activity = {"at": time.monotonic()}

    def _record_activity():
        activity["at"] = time.monotonic()
        _RUN.output(role_id, activity["at"])

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
        except OSError:
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
    helpers = (feeder, pump_out, pump_err)
    watchdog = (
        asyncio.create_task(_watchdog()) if stall_secs > 0 else None
    )
    warning = None
    unsafe_to_replay = False
    try:
        try:
            await _process_exit(proc)
            # The process is gone: the watchdog must not fire while the
            # remaining pipe bytes are drained (post-exit data is data, not
            # a stall), and the drain itself is bounded.
            if watchdog is not None:
                watchdog.cancel()
            _, held = await asyncio.wait(helpers, timeout=POST_EXIT_DRAIN_SECS)
            if held:
                warning = POST_EXIT_DRAIN_WARNING
                _diag(f"[codex-council:{role_id}] {warning}")
                await _begin_termination()
                for task in held:
                    task.cancel()
            failed = sorted({
                type(outcome).__name__
                for outcome in await asyncio.gather(
                    *helpers, return_exceptions=True)
                if isinstance(outcome, BaseException)
                and not isinstance(outcome, asyncio.CancelledError)
            })
            if failed:
                # Output the scanner never saw could have held tool work.
                unsafe_to_replay = True
                failure = IO_FAILED_WARNING.format(", ".join(failed))
                warning = _append_warning(warning, failure)
                _diag(f"[codex-council:{role_id}] {failure}")
            if termination["task"] is not None:
                await termination["task"]
            await _sweep_process_group(proc, pgid)
        except BaseException:
            # Reap on ANY failure or cancellation while the group may be
            # alive, the drain and the sweep included.
            if watchdog is not None:
                watchdog.cancel()
            for task in helpers:
                task.cancel()
            await _begin_termination()
            raise
    finally:
        _RUN.exited(role_id)
    scanner.finish()
    return CodexRun(
        returncode=proc.returncode,
        stdout=bytes(stdout_buf).decode("utf-8", errors="replace"),
        stderr=bytes(stderr_buf).decode("utf-8", errors="replace"),
        stalled=stalled["flag"],
        turn_completed=scanner.turn_completed,
        unsafe_to_replay=unsafe_to_replay or scanner.unsafe_to_replay,
        warning=warning,
    )


async def _process_exit(proc, timeout=None):
    """Wait until proc has exited, not until its pipes close.

    Process.wait() on some Python versions also waits for the pipes, which a
    descendant holding them can postpone forever, so the returncode (set as
    soon as the child is reaped) is watched too. At most `timeout` seconds
    when given; a failure of the wait itself propagates.
    """
    if proc.returncode is not None:
        return
    deadline = None if timeout is None else time.monotonic() + timeout
    waiter = asyncio.ensure_future(proc.wait())
    try:
        while proc.returncode is None:
            step = EXIT_POLL_SECS
            if deadline is not None:
                step = min(step, deadline - time.monotonic())
                if step <= 0:
                    return
            done, _ = await asyncio.wait({waiter}, timeout=step)
            if done:
                waiter.result()
                return
    finally:
        if not waiter.done():
            waiter.cancel()


async def _terminate_process_group(proc, pgid=None):
    """Best-effort SIGTERM then SIGKILL to the codex process group and to
    the tool sessions a still-running codex started (descendant_targets)."""
    tools = ([], [])
    if proc.returncode is None:
        with contextlib.suppress(Exception):
            tools = await asyncio.to_thread(descendant_targets, proc.pid)

    def _signal_group(sig):
        signal_targets(*tools, sig)
        if pgid is not None:
            try:
                os.killpg(pgid, sig)
                return
            except OSError:
                pass
        if proc.returncode is None:
            try:
                if sig == signal.SIGTERM:
                    proc.terminate()
                else:
                    proc.kill()
            except OSError:
                pass

    _signal_group(signal.SIGTERM)
    try:
        await asyncio.sleep(TERMINATION_GRACE_SECS)
    finally:
        _signal_group(signal.SIGKILL)
    with contextlib.suppress(Exception):
        await _process_exit(proc, timeout=POST_EXIT_DRAIN_SECS)


async def _sweep_process_group(proc, pgid):
    """Terminate whatever an exited attempt left in its process group.

    A group with no live member (the usual case) costs one signal-0 probe.
    """
    try:
        os.killpg(pgid, 0)
    except OSError:
        return
    await _terminate_process_group(proc, pgid)


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
            role=role, ok=True, text=msg, elapsed_seconds=elapsed,
            attempts=attempt, warning=warning,
        )
    if not run.unsafe_to_replay:
        return RoleResult(
            role=role, ok=False,
            error=(
                f"[retriable:stall] no output for {stall}s (watchdog "
                f"{stall}s); no tool work had begun, so replay is safe"
            ),
            elapsed_seconds=elapsed, attempts=attempt, warning=warning,
            retriable=True,
        )
    error = (
        f"[stall] no output for {stall}s (watchdog {stall}s); not "
        "automatically retried because tool work may have begun — re-invoke "
        "the role manually if needed"
    )
    if msg:
        error += f"\nlast (possibly incomplete) agent_message: {msg}"
    return RoleResult(
        role=role, ok=False, error=error, elapsed_seconds=elapsed,
        attempts=attempt, warning=warning,
    )


def _start_line(role, phase, attempt, stall_secs):
    return (
        f"[codex-council] {role.id}: started ({phase}) "
        f"attempt={attempt}/{MAX_RETRY_ATTEMPTS} "
        f"watchdog={_watchdog_desc(stall_secs)}"
    )


async def _run_role_once(role, prompt, attempt):
    """One attempt for one role (see _run_role_invocation).

    A warning any of its codex runs reports (POST_EXIT_DRAIN_WARNING,
    IO_FAILED_WARNING) is added to the role's result.
    """
    runs = []

    async def run_codex(cmd):
        runs.append(await _run_codex_subprocess(cmd, prompt, role_id=role.id))
        return runs[-1]

    result = await _run_role_invocation(role, attempt, run_codex)
    for run in runs:
        result.warning = _append_warning(result.warning, run.warning)
    return result


async def _run_role_invocation(role, attempt, run_codex):
    """One attempt for one role: a fresh run, or a resume that restarts
    fresh, with STALE_RESUME_WARNING, when its saved thread is stale. No
    retry logic here.

    The command carries only the role's resolved dispatch values; the
    requested values and the selection never reach codex or saved state.
    `run_codex(cmd)` runs one codex subprocess and returns its CodexRun.
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
        run = await run_codex(_resume_cmd(root, session_id, model, effort))
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
            # The runner stores whatever non-empty id thread.started emitted
            # and does not check that it is a UUID; Codex emits UUIDs, so
            # silent-spawn needs an unexpected id or a hand-edited state
            # file. Detect it by comparing a non-empty emitted
            # thread.started.thread_id to what we asked to resume; if it
            # differs, adopt the new id (no benefit re-running an
            # already-completed turn) and warn — the role lost its prior
            # accumulated framing.
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
                    role=role, ok=True, text=msg, elapsed_seconds=elapsed,
                    attempts=attempt, warning=warning,
                )
            return RoleResult(
                role=role, ok=False,
                error=_format_clean_exit_no_message(failure_text),
                elapsed_seconds=elapsed, attempts=attempt, warning=warning,
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
        # as-is, so the tag always matches the branch taken here, and the
        # retry decision is the verdict's, never the tag text's.
        records = _failure_records(run.stdout)
        verdict = _failure_verdict(failure_text, records, model, resume=True)
        if verdict.kind != "stale":
            err = _classify_failure(
                failure_text, run.returncode, "resume", decision, verdict,
            )
            return RoleResult(
                role=role, ok=False, error=err,
                elapsed_seconds=time.monotonic() - started, attempts=attempt,
                retriable=verdict.retriable,
            )

        # Stale: log, warn, clear, fall through to fresh. The warning rides
        # every outcome of the fresh run, so the report and the reply file
        # show that the role lost its prior continuity. A failed clear is
        # only worth a warning in the outcomes where stale state actually
        # remains on disk (a later successful save atomically replaces it
        # anyway).
        updated = (meta or {}).get("updated_at", "unknown")
        _diag(
            f"[codex-council:{role.id}] session {_log_inline(session_id)} "
            f"(last used {_log_inline(updated)}) "
            f"is stale ({_log_inline(failure_text)}) — starting fresh."
        )
        warning = _append_warning(warning, STALE_RESUME_WARNING)
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
    run = await run_codex(_fresh_cmd(root, model, effort))
    if run.stalled:
        return _stalled_role_result(
            role, run, None, attempt, started,
            warning=_with_stale_clear_warning(warning),
        )
    failure_text = _failure_text(run.stdout, run.stderr)

    if run.returncode != 0:
        # Same order as the resume path, minus the stale branch.
        records = _failure_records(run.stdout)
        verdict = _failure_verdict(failure_text, records, model)
        return RoleResult(
            role=role, ok=False,
            error=_classify_failure(
                failure_text, run.returncode, "exec", decision, verdict,
            ),
            elapsed_seconds=time.monotonic() - started, attempts=attempt,
            warning=_with_stale_clear_warning(warning),
            retriable=verdict.retriable,
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
            role=role, ok=True, text=msg, elapsed_seconds=elapsed,
            attempts=attempt, warning=warning,
        )
    return RoleResult(
        role=role, ok=False,
        error=_format_clean_exit_no_message(failure_text),
        elapsed_seconds=elapsed, attempts=attempt,
        warning=_with_stale_clear_warning(warning),
    )


async def _run_role_attempts(role, prompt):
    """Run one already-locked role, retrying a rate limit, a 5xx, or a
    replay-safe stall after RETRY_BACKOFF_SECS, up to MAX_RETRY_ATTEMPTS.

    A failed attempt is retried only when its RoleResult says so
    (`retriable`, from the structured verdict or the stall policy); the
    error text is never consulted, so Codex text that merely starts with
    "[retriable:" cannot forge a retry. A stale thread recovered on an
    earlier attempt stays lost, so every later result keeps
    STALE_RESUME_WARNING.
    """
    attempt = 1
    lost = False
    while True:
        _RUN.update(role.id, state="active", attempt=attempt)
        result = await _run_role_once(role, prompt, attempt)
        if STALE_RESUME_WARNING in (result.warning or ""):
            lost = True
        elif lost:
            result.warning = _append_warning(
                STALE_RESUME_WARNING, result.warning,
            )
        if result.ok or not result.retriable or attempt >= MAX_RETRY_ATTEMPTS:
            return result
        _diag(
            f"[codex-council:{role.id}] retriable error on attempt "
            f"{attempt}/{MAX_RETRY_ATTEMPTS}; sleeping {RETRY_BACKOFF_SECS}s."
        )
        # A stale quiet value would be misleading while no subprocess runs.
        _RUN.update(role.id, state="retry-wait")
        await asyncio.sleep(RETRY_BACKOFF_SECS)
        attempt += 1


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


async def run_council(roles, body, max_parallel, replies_dir=None):
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
                if lock_file is not None:
                    active.add(role.id)
                    # Quiet counts from activation until the first output.
                    _RUN.output(role.id, time.monotonic())
                    try:
                        return await _run_role_attempts(
                            role, _compose_prompt(role, body)
                        )
                    finally:
                        active.discard(role.id)
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
                _RUN.describe(rid, now) for rid in sorted(active)
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
            outcome = "crashed"
            suffix = _reply_suffix(_exception_result(
                role, exc, time.monotonic() - started
            ))
            _diag(
                f"[codex-council] {n}/{total} {role.id}: crashed "
                f"({type(exc).__name__}){suffix}"
            )
        else:
            res = task.result()
            outcome = "ok" if res.ok else "failed"
            suffix = _reply_suffix(res)
            _diag(
                f"[codex-council] {n}/{total} {role.id}: "
                f"{'ok' if res.ok else 'FAILED'} "
                f"({res.elapsed_seconds:.1f}s){suffix}"
            )
        # After the reply file and its completion line.
        _RUN.update(role.id, state="settled", outcome=outcome)

    async def _status_tick():
        # Proof the event loop turns, between role transitions.
        while True:
            await asyncio.sleep(STATUS_TICK_SECS)
            _RUN.publish()

    _RUN.begin([role.id for role in roles])
    tasks = []
    for role in roles:
        t = asyncio.create_task(_run_bounded(role))
        t.add_done_callback(lambda task, role=role: _on_role_done(role, task))
        tasks.append(t)
    progress_tasks = (asyncio.create_task(_heartbeat()),
                      asyncio.create_task(_status_tick()))
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    finally:
        # Progress reporting must never turn an otherwise successful council
        # into a failure (for example if stderr was closed by the host).
        for task in progress_tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, OSError):
                await task
    elapsed = time.monotonic() - started

    return [
        _exception_result(role, r, elapsed) if isinstance(r, BaseException)
        else r
        for role, r in zip(roles, results)
    ]


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
        f"launch discovery not run ({NO_AUTOMATIC_SELECTIONS})"
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
            "--discover RUNDIR records this run's model snapshot "
            "(metadata only; no thread or turn is started), and role "
            "objects accept a 'selection' object, required whenever the "
            "optional 'model' or 'effort' is present: mode 'user' for an "
            "explicit pin "
            "(forwarded unchanged), 'routed' or 'native_effort' for a "
            "runtime-grounded choice validated against that snapshot and "
            "revalidated at launch. Omit model, effort, and selection to "
            "inherit native Codex configuration. "
            "CODEX_COUNCIL_MODEL_ROUTING=off disables automatic selection. "
            "A model Codex rejects fails the role as [model-rejected] and a "
            "usage or credit limit as [quota]; neither is retried. SKILL "
            f"contract epoch {SKILL_CONTRACT_EPOCH}.\n\n"
            "Direct CLI use: every on-disk input's parent directory must be "
            "private (0700, user-owned, non-symlink) at launch as well as "
            "preflight, e.g. one created by `mktemp -d`, and --discover and "
            "the preflight refuse a directory that already launched. Each "
            "settled role's section is also "
            "written to <RUNDIR>/replies/<key>.md before its completion line "
            "(which then ends in ' reply=<path>'). The launch publishes "
            f"<RUNDIR>/{STATUS_FILENAME}; --follow RUNDIR relays the "
            "council's actionable err.log progress, --status RUNDIR prints "
            "a snapshot, and --reap RUNDIR ends the codex process groups of "
            "a runner that is gone. --start RUNDIR launches the staged "
            "council detached (outside any host background task), and "
            "--cancel RUNDIR stops it."
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
            "directory. Use this after writing the per-run staging files, "
            "as its own call, and launch the background council in a "
            "separate call only after it exits 0 (the launch itself does "
            "not check for an earlier launch)."
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
            "Read-only follower: relay the actionable '[codex-council' lines "
            "of RUNDIR/err.log to stdout (dispatch, model selection, "
            "completions, retries, stalls, warnings, and the terminal line; "
            "one flushed line per event) and exit 0 after the "
            "CODEX_COUNCIL_DONE sentinel, an interruption line, or a "
            "'runner aborted' line. Per-attempt start lines and the "
            "heartbeat stay in err.log unless --verbose. Exits 3 if no "
            f"dispatch line appears within {FOLLOW_START_SECS}s, 4 with one "
            f"line when the runner recorded in RUNDIR/{STATUS_FILENAME} is "
            f"gone or has not ticked for {TICK_GIVE_UP_SECS}s (one warning "
            f"at {TICK_WARN_SECS}s), 5 when the "
            "follower's own parent is gone, and 1 when its stdout reader is "
            "gone. A new follower reads err.log from the start. Cannot be "
            "combined with --roles-file, --context-file, "
            "--check-staging-dir, --status, --reap, or --discover."
        ),
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="With --follow: also relay start lines and heartbeats.",
    )
    parser.add_argument(
        "--status", default=None, metavar="RUNDIR",
        help=(
            "Read-only snapshot of a launched run from RUNDIR/"
            f"{STATUS_FILENAME}: the runner's state (running, not "
            "responding, gone, done, interrupted, aborted), up to five "
            "unfinished roles (state, attempt, quiet seconds, and codex pid) "
            "and a count of the rest, live codex process groups when the "
            "runner is gone, and one next action. Always exits 0."
        ),
    )
    parser.add_argument(
        "--start", default=None, metavar="RUNDIR",
        help=(
            "Detached launch of the staged council in RUNDIR (roles.json and "
            "context.md): validate it exactly as --check-staging-dir does "
            "(a refusal exits 2 and leaves RUNDIR untouched), claim it "
            f"atomically ({SUPERVISOR_LOCK_FILENAME}, err.log, and out.md, "
            "each created exclusively and 0600; a directory that already "
            "launched exits 2), and start the council as a supervisor in "
            "its own session that holds the lock for its whole life and "
            f"writes {SUPERVISOR_FILENAME}. Exits 0 with one 'started' line "
            "and the --follow, --status, and --cancel commands once the "
            "supervisor is running, or 1 when it exited at once (read "
            "err.log; the directory is used up). Run it as an ordinary "
            "foreground command, never in a background task and never "
            "with &, nohup, or setsid. Never retry it in the same RUNDIR."
        ),
    )
    parser.add_argument(
        "--cancel", default=None, metavar="RUNDIR",
        help=(
            "Stop a council launched with --start: only while its "
            f"supervisor lock is held and {SUPERVISOR_FILENAME} (and "
            f"{STATUS_FILENAME}) name the same live runner, SIGTERM that "
            "runner, which tears down its codex process groups; after a "
            "grace, SIGKILL it if it is still the same process (then run "
            "--reap). Exits 0 once the runner has ended, 1 when refused "
            "(nothing is signalled) or when it did not end."
        ),
    )
    parser.add_argument(
        "--supervisor-lock-fd", default=None, type=int,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--reap", default=None, metavar="RUNDIR",
        help=(
            f"Only when RUNDIR/{STATUS_FILENAME} shows the runner is "
            "gone: SIGTERM, then SIGKILL, each recorded codex process group "
            "whose leader is still this run's codex, and the process groups "
            "and processes that codex started, then print what was done "
            "(exit 0). Refused with exit 1 otherwise. Never touches saved "
            "threads or files."
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
            "(130 on Ctrl+C, 128 + the signal number on SIGTERM or SIGHUP, "
            "1 when stdout is closed); exits 2 when RUNDIR already holds a "
            "launch. "
            "Cannot be combined with --roles-file, --context-file, "
            "--check-staging-dir, --follow, --status, or --reap."
        ),
    )
    parser.add_argument(
        "--skill-contract", default=None, type=int, metavar="EPOCH",
        help=(
            "Contract epoch the invoking SKILL text was written against. "
            "Optional (bare/direct invocations stay valid); when present it "
            f"must equal this script's epoch ({SKILL_CONTRACT_EPOCH}), "
            "otherwise the invocation is refused as a stale SKILL/script "
            "pair."
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
    # Each standalone command takes its own RUNDIR and excludes every other
    # mode.
    launch_flags = (
        ("--roles-file", args.roles_file),
        ("--context-file", args.context_file),
        ("--check-staging-dir", args.check_staging_dir),
    )
    commands = (
        ("--discover", args.discover),
        ("--start", args.start),
        ("--follow", args.follow),
        ("--status", args.status),
        ("--cancel", args.cancel),
        ("--reap", args.reap),
    )
    for name, value in commands:
        if value == "":
            parser.error(f"{name} must be non-empty")
        if value is None:
            continue
        for flag, other in (*launch_flags, *commands):
            if flag != name and other is not None:
                parser.error(f"{name} cannot be combined with {flag}")
    if args.verbose and args.follow is None:
        parser.error("--verbose requires --follow")
    # The supervised child of --start only: never a public launch route.
    if args.supervisor_lock_fd is not None and (
            args.roles_file is None or args.context_file is None
            or args.supervisor_lock_fd < 3):
        parser.error("unrecognized arguments: --supervisor-lock-fd")
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


def _usage_exit_if_file_arg_problems(*arg_pairs, hint=STAGING_PATH_HINT):
    """Aggregate missing staged-input errors before attempting reads.

    `hint` closes the message: the plain staging hint, or at a staged
    launch STAGED_LAUNCH_PATH_HINT, which starts over in a new directory.
    """
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
            + f"\n{hint}"
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


def _usage_exit_if_staging_dirs_differ(roles_file, context_file,
                                      hint=STAGING_PATH_HINT):
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
            f"{hint}"
        )


def _read_roles_file(path, hint=STAGING_PATH_HINT):
    """Read the raw roles JSON from a file.

    Passing the unrestricted-size panel as a path lets the caller write the
    JSON with a real editor/tool instead of escaping a large blob through the
    shell, where a stray quote or unbalanced brace would break the call.
    Read and decode errors exit 2 like other usage errors; JSON validity is left to
    _parse_roles_json. A path or read problem ends with `hint` (see
    _usage_exit_if_file_arg_problems); non-UTF-8 content is a roles defect
    and carries the scoped whole-file rewrite recovery.
    """
    problem = _file_arg_problem("--roles-file", path)
    if problem:
        _usage_exit(f"{problem}. {hint}")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        _usage_exit(f"--roles-file: cannot read {path!r} ({e}). {hint}")
    except UnicodeDecodeError as e:
        _roles_usage_exit(f"--roles-file: {path!r} is not valid UTF-8 ({e}).")


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


def _parse_roles_json(raw):
    """Parse the --roles-file blob into a list of Role objects.

    Validates each entry has exactly the id/label/instruction fields plus
    the optional model/effort/selection keys (instruction is a list of
    sentence-sized strings, normalized and joined to one paragraph), id is
    well-formed, model/effort (when present) match SELECTION_VALUE_PATTERN,
    the selection object is well-formed for its mode and present whenever
    model or effort is (see _parse_role_selection), instructions follow the
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
        model, effort, selection = _parse_role_selection(entry, ctx)
        if rid in seen:
            _roles_usage_exit(
                f"--roles-file {ctx}: duplicate id {rid!r} within JSON payload."
            )
        seen.add(rid)
        roles.append(Role(rid, label, instruction, model, effort, selection))
    return roles


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
    refuses; the same holds for a path or read problem, which ends with
    STAGED_LAUNCH_PATH_HINT there. There is no plugin-imposed context size
    ceiling.
    """
    hint = STAGED_LAUNCH_PATH_HINT if staged_launch else STAGING_PATH_HINT
    problem = _file_arg_problem("--context-file", path)
    if problem:
        _usage_exit(f"{problem}. {hint}")
    try:
        with open(path, "rb") as f:
            body, body_problem = _read_body_or_problem(f)
    except OSError as e:
        _usage_exit(f"--context-file: cannot read {path!r} ({e}). {hint}")
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


def _validate_staging_dir(path, prefix="--check-staging-dir: ",
                          next_step="re-run --check-staging-dir and launch."):
    """Every check the launch makes before dispatch; (path, roles, max
    parallel) or a usage exit (2) that leaves the directory untouched.

    A directory that already holds a launch (out.md, err.log, replies/, or
    a supervisor file) is refused first: a tracked launch command's
    redirections would truncate a running council's files before the
    runner could object. A directory, roles file, context, missing codex
    binary, or environment override (CODEX_COUNCIL_MAX_PARALLEL,
    CODEX_COUNCIL_STALL_SECS, CODEX_COUNCIL_MODEL_ROUTING) the launch would
    refuse is refused here too. No discovery runs: automatic selections are
    validated against this run's planning snapshot (DIR/model-snapshot.json)
    through the orchestration the launch uses (_resolve_run_selections),
    and are revalidated by a fresh discovery at launch. `prefix` names the
    command (the pre-flight or --start) and `next_step` follows a missing
    codex's PATH fix. Returns the roles with their plan decisions.
    """
    path = _check_private_dir(path, prefix=prefix)
    _usage_exit_if_launched(path, prefix)
    roles_path = os.path.join(path, "roles.json")
    context_path = os.path.join(path, "context.md")
    _usage_exit_if_file_arg_problems(
        ("--roles-file", roles_path),
        ("--context-file", context_path),
    )
    roles = _parse_roles_json(_read_roles_file(roles_path))
    _read_context_file(context_path)
    # The codex binary is the one hard external dependency; a preflight
    # that says "staging OK" while codex is missing defers the failure to
    # a background launch whose error lands only in err.log.
    _usage_exit_if_codex_missing(prefix, next_step)
    max_parallel = _max_parallel_roles()
    _stall_secs()  # the launch refuses an invalid watchdog override (exit 2)
    routing_mode = _model_routing_mode()
    roles, _ = _resolve_run_selections(
        roles, path, routing_mode, at_launch=False
    )
    return path, roles, max_parallel


def _check_staging_dir(path):
    """Validate the per-run staging dir before launching Codex.

    Runs _validate_staging_dir, so a directory the launch would refuse
    never reports "staging OK", then prints the staging-OK line and one
    selection-plan line per role.
    """
    if path == "":
        _usage_exit("--check-staging-dir must be non-empty.")
    path, roles, max_parallel = _validate_staging_dir(path)
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


class _TerminationSignal(BaseException):
    """SIGTERM or SIGHUP, raised where synchronous work held the main
    thread (see _termination_raises). A BaseException, like
    KeyboardInterrupt, so discovery's catch-all cannot swallow it."""

    def __init__(self, signum):
        super().__init__(signum)
        self.signum = signum


@contextlib.contextmanager
def _termination_raises():
    """Turn SIGTERM and SIGHUP into _TerminationSignal inside the block.

    Discovery runs synchronously, before the council's event loop installs
    its own handlers, and it owns process groups: the `codex --version`
    probe and the app-server with anything it spawned. With the default
    action a SIGTERM or SIGHUP would kill the runner and orphan them;
    raising instead unwinds through their `finally` teardown, as
    KeyboardInterrupt does for SIGINT. Only the first signal raises, so a
    second cannot cut that teardown short; a signal the process already
    ignores (SIGHUP under nohup) stays ignored; the previous handlers are
    restored on exit.
    """
    received = []

    def _raise(signum, _frame):
        if not received:
            received.append(signum)
            raise _TerminationSignal(signum)

    previous = {}
    try:
        for signum in (signal.SIGTERM, signal.SIGHUP):
            if signal.getsignal(signum) is not signal.SIG_IGN:
                previous[signum] = signal.signal(signum, _raise)
        yield
    finally:
        for signum, handler in previous.items():
            # None: a handler not installed from Python; default is closest.
            if handler is None:
                handler = signal.SIG_DFL
            signal.signal(signum, handler)


TERMINATION_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def _ignore_termination_signals():
    """After the first termination signal has been handled: ignore later
    ones, so a repeated signal cannot cut the exit path short or change
    the exit code the first one set."""
    for signum in TERMINATION_SIGNALS:
        with contextlib.suppress(OSError, RuntimeError, ValueError):
            signal.signal(signum, signal.SIG_IGN)


def _exit_interrupted(signum, line):
    """Write the one-line interruption notice and exit 128 + signum."""
    _ignore_termination_signals()
    _diag(f"{line} {signal.Signals(signum).name}")
    sys.exit(128 + int(signum))


async def _run_council_with_signals(roles, body, max_parallel, replies_dir=None):
    """Run the council and translate POSIX termination signals into cleanup.

    The first SIGINT, SIGTERM, or SIGHUP is latched: it cancels the council
    once and decides the exit code (128 + that signal). A repeated signal
    is ignored, so it can neither cancel the cleanup the first one started
    (process-group teardown, reply files) nor change the exit code.
    """
    loop = asyncio.get_running_loop()
    council_task = asyncio.create_task(
        run_council(
            roles, body, max_parallel=max_parallel, replies_dir=replies_dir
        )
    )
    interrupted = {"signum": None}
    registered = []

    def _cancel_for_signal(signum):
        if interrupted["signum"] is not None:
            return
        interrupted["signum"] = signum
        council_task.cancel()

    for signum in TERMINATION_SIGNALS:
        try:
            loop.add_signal_handler(signum, _cancel_for_signal, signum)
            registered.append(signum)
        except (RuntimeError, ValueError):
            pass

    try:
        return await council_task, None
    except asyncio.CancelledError:
        return None, interrupted["signum"] or signal.SIGINT
    finally:
        for signum in registered:
            with contextlib.suppress(RuntimeError, ValueError):
                loop.remove_signal_handler(signum)
        if interrupted["signum"] is not None:
            # Removing a handler restores the default action; after the
            # latch a late signal must not kill the exit path instead.
            _ignore_termination_signals()


# ---------- detached launch: --start and its supervised child ----------

def _start_command(run_dir):
    """--start RUNDIR: launch the staged council detached; exit 0, 1, or 2.

    1. Validate exactly as the pre-flight does; a refusal exits 2 before
       anything is created, so the directory stays untouched.
    2. Claim the directory, in this order and each exclusively (O_EXCL,
       O_NOFOLLOW, 0600): supervisor.lock, locked with flock(LOCK_EX), then
       err.log and out.md. A concurrent --start (or a tracked launch) that
       got there first makes this one exit 2 having created and truncated
       nothing. The lock file is never removed or replaced.
    3. Start this script's staged launch as the supervisor, in its own
       session, with stdout on out.md, stderr on err.log, stdin on
       /dev/null, the same cwd and environment, and the locked descriptor
       as its only extra one (--supervisor-lock-fd). The context stays in
       its file; the command line carries only paths.
    4. Close this process's copies (the lock stays held by the child) and
       wait up to START_WAIT_SECS for the child to write supervisor.json,
       or to exit early (exit 1: read err.log; the directory is used up).
    """
    prefix = "--start: "
    path, _, _ = _validate_staging_dir(run_dir, prefix, "re-run --start.")
    abs_dir = os.path.abspath(path)
    claimed = []
    flags = os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC

    def refuse(name, e):
        for fd in claimed:
            os.close(fd)
        if isinstance(e, FileExistsError):
            why = f"{name} already exists"
        else:
            why = f"cannot create {name}: {e.strerror or e}"
        _usage_exit(
            f"{prefix}{abs_dir!r} already holds a council launch or cannot "
            f"be claimed ({why}); nothing here was started, truncated, or "
            f"replaced. {LAUNCHED_DIR_RECOVERY}"
        )

    try:
        lock_fd = os.open(os.path.join(abs_dir, SUPERVISOR_LOCK_FILENAME),
                          os.O_RDWR | flags, 0o600)
    except OSError as e:
        refuse(SUPERVISOR_LOCK_FILENAME, e)
    claimed.append(lock_fd)
    # A reader's momentary shared lock (--status, --follow) may be in the
    # way for an instant; nobody else can hold a file this call created.
    lock_deadline = time.monotonic() + 1
    while True:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError as e:
            if time.monotonic() >= lock_deadline:
                refuse(SUPERVISOR_LOCK_FILENAME, e)
            time.sleep(0.01)
    for name, mode in (("err.log", os.O_WRONLY | os.O_APPEND),
                       ("out.md", os.O_WRONLY)):
        try:
            claimed.append(os.open(os.path.join(abs_dir, name),
                                   mode | flags, 0o600))
        except OSError as e:
            refuse(name, e)
    _, err_fd, out_fd = claimed
    argv = [
        sys.executable, os.path.realpath(__file__),
        "--roles-file", os.path.join(abs_dir, "roles.json"),
        "--context-file", os.path.join(abs_dir, "context.md"),
        "--skill-contract", str(SKILL_CONTRACT_EPOCH),
        "--supervisor-lock-fd", str(lock_fd),
    ]
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=out_fd, stderr=err_fd,
            start_new_session=True, close_fds=True, pass_fds=(lock_fd,),
        )
    except OSError as e:
        print(
            f"[codex-council] start failed: cannot start the supervisor "
            f"({_report_inline(e)}); {abs_dir!r} is used up. "
            f"{LAUNCHED_DIR_RECOVERY}",
            file=sys.stderr,
        )
        sys.exit(1)
    finally:
        # Never LOCK_UN: the child shares this lock and keeps it.
        for fd in claimed:
            os.close(fd)
    sup_path = os.path.join(abs_dir, SUPERVISOR_FILENAME)
    deadline = time.monotonic() + START_WAIT_SECS
    ready = False
    while True:
        code = proc.poll()
        if code is not None:
            print(
                f"[codex-council] start failed: the supervisor (pid "
                f"{proc.pid}) exited with status {code} before the council "
                f"was running; read {_report_inline(abs_dir)}/err.log. Do "
                "not retry --start in this directory: it is used up. "
                f"{LAUNCHED_DIR_RECOVERY}",
                file=sys.stderr,
            )
            sys.exit(1)
        record = read_supervisor(sup_path)
        if record is not None and record.pid == proc.pid:
            ready = True
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(START_POLL_SECS)
    command = (f"{shlex.quote(sys.executable)} "
               f"{shlex.quote(os.path.realpath(__file__))}")
    quoted = shlex.quote(abs_dir)
    contract = f"--skill-contract {SKILL_CONTRACT_EPOCH}"
    print(f"[codex-council] started: pid={proc.pid} "
          f"dir={_report_inline(abs_dir)} version={_plugin_version()}")
    if not ready:
        print(f"note: {SUPERVISOR_FILENAME} is not written yet; the "
              "supervisor is still starting; run --status")
    print(f"follow: {command} --follow {quoted} {contract}")
    print(f"status: {command} --status {quoted} {contract}")
    print(f"cancel: {command} --cancel {quoted} {contract}")


def _become_supervisor(run_dir, lock_fd):
    """The --start child's first step; exits 2 (or 1) before any work.

    Verifies that the inherited descriptor is RUNDIR/supervisor.lock (same
    device and inode, a private regular file), that this process holds its
    lock, and that no supervisor.json exists yet; stops the descriptor
    from reaching any codex worker and keeps it open for this runner's
    life; then writes supervisor.json (0600, atomically) about itself.
    """
    lock_path = os.path.join(run_dir, SUPERVISOR_LOCK_FILENAME)
    sup_path = os.path.join(run_dir, SUPERVISOR_FILENAME)
    prefix = "--supervisor-lock-fd: "
    try:
        held = os.fstat(lock_fd)
        on_disk = os.lstat(lock_path)
    except OSError as e:
        _usage_exit(f"{prefix}cannot verify {lock_path!r} "
                    f"({e.strerror or e}); only --start runs this. "
                    f"{LAUNCHED_DIR_RECOVERY}")
    if (_private_stat_problem(on_disk, directory=False) is not None
            or not stat.S_ISREG(held.st_mode)
            or (held.st_dev, held.st_ino) != (on_disk.st_dev, on_disk.st_ino)):
        _usage_exit(f"{prefix}the descriptor is not this run's private "
                    f"{SUPERVISOR_LOCK_FILENAME}; only --start runs this. "
                    f"{LAUNCHED_DIR_RECOVERY}")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _usage_exit(f"{prefix}another process holds {lock_path!r}. "
                    f"{LAUNCHED_DIR_RECOVERY}")
    if os.path.lexists(sup_path):
        _usage_exit(f"{prefix}{sup_path!r} already exists: this directory "
                    f"already had a supervisor. {LAUNCHED_DIR_RECOVERY}")
    os.set_inheritable(lock_fd, False)
    _SUPERVISOR["lock_fd"] = lock_fd
    record = supervisor_record(os.getpid(), held, _plugin_version(),
                               SKILL_CONTRACT_EPOCH, _utc_iso(time.time()))
    try:
        _atomic_write_private(sup_path, json.dumps(record).encode("utf-8"))
    except OSError as e:
        _diag(f"[codex-council] {SUPERVISOR_FILENAME} not written "
              f"({_log_inline(e)}); the supervisor stopped before any work")
        sys.exit(1)


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

    if args.check_staging_dir is not None:
        _check_staging_dir(args.check_staging_dir)
        return

    if args.discover is not None:
        try:
            with _termination_raises():
                _discover_command(args.discover)
        except KeyboardInterrupt:
            # The app-server group was already torn down by its session's
            # finally; no new snapshot was written.
            _diag("[codex-council] --discover interrupted by user")
            sys.exit(130)
        except _TerminationSignal as stop:
            # Unwound the same way: every discovery process group is gone.
            _exit_interrupted(stop.signum,
                              "[codex-council] --discover interrupted by")
        return

    if args.start is not None:
        _start_command(args.start)
        return
    if args.follow is not None:
        try:
            code = follow(args.follow, verbose=args.verbose)
        except KeyboardInterrupt:
            code = 130
        sys.exit(code)
    if args.status is not None:
        sys.exit(status_command(args.status))
    if args.cancel is not None:
        try:
            code = cancel_command(args.cancel)
        except KeyboardInterrupt:
            code = 130
        sys.exit(code)
    if args.reap is not None:
        sys.exit(reap_command(args.reap))

    # Launch-side privacy gate: validate each on-disk input's LEXICAL parent
    # BEFORE any content read or parse — a public directory holding bad roles
    # must produce "abandon this exposed directory", never "rewrite roles".
    # In stdin mode only roles.json is on disk; piped context has no directory
    # and is validated below as UTF-8/non-empty only.
    #
    # A staged launch's own redirections created out.md and err.log before
    # it started, so the pre-flight now refuses its directory: every
    # refusal from here to dispatch, the input and path checks included,
    # starts over in a new directory instead of asking for a pre-flight
    # re-run in this one. Stdin mode keeps its own wording.
    staged = args.context_file is not None
    path_hint = STAGED_LAUNCH_PATH_HINT if staged else STAGING_PATH_HINT
    roles_recovery = (
        STAGED_LAUNCH_ROLES_RECOVERY if staged else ROLES_REWRITE_RECOVERY
    )
    if args.roles_file is not None:
        recovery = STAGING_DIR_RECOVERY if staged else STDIN_DIR_RECOVERY
        _usage_exit_unless_parent_private("--roles-file", args.roles_file, recovery)
    if staged:
        _usage_exit_unless_parent_private(
            "--context-file", args.context_file, STAGING_DIR_RECOVERY
        )
    _usage_exit_if_staging_dirs_differ(args.roles_file, args.context_file,
                                      path_hint)
    if args.supervisor_lock_fd is not None:
        # The --start child: verify the claim and publish supervisor.json
        # before reading any input, discovering, or dispatching.
        _become_supervisor(os.path.dirname(os.path.abspath(args.context_file)),
                           args.supervisor_lock_fd)
    _usage_exit_if_file_arg_problems(
        ("--roles-file", args.roles_file),
        ("--context-file", args.context_file),
        hint=path_hint,
    )

    # Parse and validate staged inputs before requiring Codex. This catches
    # temp-path mismatches without launching or depending on any Codex state.
    if args.roles_file is None:
        _usage_exit(
            "No roles requested. Pass --roles-file with the role panel "
            "(Claude composes this per invocation; see SKILL.md)."
        )
    with _roles_recovery(roles_recovery):
        roles = _parse_roles_json(_read_roles_file(args.roles_file, path_hint))

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
    # role gets the decision its commands are built from. A termination
    # signal during that discovery unwinds through its teardown.
    try:
        with _roles_recovery(roles_recovery), _termination_raises():
            roles, launch = _resolve_run_selections(
                roles, run_dir, routing_mode, at_launch=True
            )
    except KeyboardInterrupt:
        _diag("\n[codex-council] interrupted by user")
        sys.exit(130)
    except _TerminationSignal as stop:
        _exit_interrupted(stop.signum, "\n[codex-council] interrupted by")
    state, reason = _launch_discovery_state(
        routing_mode, any(_is_automatic(r) for r in roles), launch
    )
    # A problem here only disables reply files.
    replies_dir = _prepare_replies_dir(run_dir)
    # From here on the run publishes status.json (a failed write only costs
    # the liveness view, never the council).
    _RUN.attach(os.path.join(run_dir, STATUS_FILENAME),
                mode="detached" if _SUPERVISOR["lock_fd"] is not None
                else None)

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
        _RUN.finish("interrupted", 130)
        _diag("\n[codex-council] interrupted by user")
        sys.exit(130)
    except Exception as e:
        # Leave a terminal line so --follow stops at once; the traceback
        # follows on stderr.
        _RUN.finish("aborted", 1)
        _diag(
            "\n[codex-council] runner aborted exit=1: unhandled "
            f"{type(e).__name__}; no report was written"
        )
        raise
    if signum is not None:
        _RUN.finish("interrupted", 128 + int(signum))
        _exit_interrupted(signum, "\n[codex-council] interrupted by")

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
        _RUN.finish("aborted", 1)
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
    # change the exit code (status.json still records the end).
    _RUN.finish("done", exit_code)
    _diag(
        f"[codex-council] CODEX_COUNCIL_DONE ok={successes} total={total} "
        f"elapsed={elapsed:.1f}s exit={exit_code} version={_plugin_version()}"
    )

    if exit_code:
        sys.exit(1)


if __name__ == "__main__":
    main()
