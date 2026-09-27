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
                                   otherwise use Codex agents.max_threads or 6
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
pin. The staging-OK,
dispatch, heartbeat, and CODEX_COUNCIL_DONE lines carry
`version=<plugin version>` for postmortem visibility (it does not prevent
skew; the contract epoch does).

POSIX-only: uses start_new_session and process-group signals.
"""

import argparse
import asyncio
import collections
import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from functools import cache
from pathlib import Path
from typing import Optional

try:
    import tomllib
except ImportError:  # Python < 3.11: keep the Codex default fallback.
    tomllib = None

STATE_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
    "codex-council",
)

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
# A failure naming one of these parameters is about the effort or service
# tier, not the model, so it is never read as a model rejection.
MODEL_REJECTION_EXCLUDED_PARAMS = (
    "reasoning.effort",
    "model_reasoning_effort",
    "service_tier",
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
_READ_CHUNK_BYTES = 65536
# Per-role reply files live in this subdirectory of the private RUNDIR.
REPLIES_SUBDIR = "replies"
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
# Model discovery (--discover; see the "model discovery" section). Routing
# is on unless this is "off"; any value other than auto/off is a usage error.
MODEL_ROUTING_ENV = "CODEX_COUNCIL_MODEL_ROUTING"
# ONE monotonic budget covers the `codex --version` probe (itself capped),
# the app-server spawn, the handshake, and every request; interleaved
# notifications never extend it. Teardown then adds at most three
# DISCOVERY_CLOSE_GRACE_SECS waits (stdin EOF, SIGTERM, SIGKILL).
DISCOVERY_TIMEOUT_SECS = 20
DISCOVERY_VERSION_TIMEOUT_SECS = 5
DISCOVERY_CLOSE_GRACE_SECS = 0.5
# Council-side resource bounds, not Codex guarantees: reaching a catalog bound
# with more pages outstanding marks the catalog incomplete (never "absent").
DISCOVERY_MAX_LINE_BYTES = 8 * 1024 * 1024
DISCOVERY_MAX_STDOUT_BYTES = 32 * 1024 * 1024
DISCOVERY_MAX_UNSOLICITED = 10000
DISCOVERY_PAGE_LIMIT = 100
DISCOVERY_MAX_PAGES = 10
DISCOVERY_MAX_MODELS = 1000
DISCOVERY_STDERR_TAIL_BYTES = 4096
DISCOVERY_STDERR_EXCERPT_CHARS = 200
SNAPSHOT_FILENAME = "model-snapshot.json"
SNAPSHOT_SCHEMA = "codex-council/model-snapshot@1"
SNAPSHOT_MAX_BYTES = 64 * 1024 * 1024
REQUIRED_SCOPE_PHRASE = "nothing material"
REQUIRED_CADENCE_SENTENCE = "Thoroughness beats speed."
# Every line-boundary character str.splitlines() recognizes (beyond the plain
# space): CR, LF, VT, FF, FS, GS, RS, NEL, LS, PS. Labels reject the full set
# and _report_inline escapes the same set, so the two contracts agree.
LINEBREAK_CHARS = (
    "\r", "\n", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e",
    "\x85", "\u2028", "\u2029",
)
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
# Action-first recovery text for a rejected staging DIRECTORY. The orchestrator
# is an LLM; the cheapest literal reading of "create it with mktemp -d" is
# satisfiable by mkdir/chmod on the same predictable path, so the recovery must
# forbid exactly those moves and demand a NEW path.
STAGING_DIR_RECOVERY = (
    "Recovery: abandon this directory — do not chmod it, do not mkdir it, "
    "and do not reuse its name. Run `mktemp -d` again, copy the NEW printed "
    "absolute path, re-Write BOTH roles.json and context.md into that new "
    "directory, and re-run --check-staging-dir on it."
)
# Same action, phrased for the stdin launch mode, where roles.json is the only
# on-disk input: no context.md and no preflight exist to mention.
STDIN_DIR_RECOVERY = (
    "Recovery: abandon this directory — do not chmod it, do not mkdir it, "
    "and do not reuse its name. Run `mktemp -d` again, copy the NEW printed "
    "absolute path, rewrite the roles file into that new directory, and "
    "re-run the direct command against it."
)
# Uniform recovery appended to EVERY roles-file validation failure. The only
# production writer of roles.json is an LLM; partial patches of a file that
# already glitched once are the corruption vector, so every defect demands one
# complete rewrite. The suffix stays mode-neutral because parse-time code
# cannot know whether the caller staged a context file or piped stdin.
ROLES_REWRITE_RECOVERY = (
    "Recovery: rewrite the entire file passed to --roles-file in one "
    "complete Write operation; do not patch, append, or replace a "
    "substring. Do not launch until the rewritten file validates, then "
    "re-run the pre-flight or the direct command you used."
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


@dataclass(frozen=True)
class Selection:
    """A role's validated `selection` object (see _parse_selection).

    mode "user" is an explicit user pin; "routed" and "native_effort" are
    runtime-grounded choices bound to this run's discovery snapshot.
    """
    mode: str
    snapshot_id: Optional[str] = None
    reason: Optional[str] = None


@dataclass(frozen=True)
class SelectionDecision:
    """How one role's model and effort resolve (see _resolve_selection).

    `mode` is what the role asked for (inherit, user, routed,
    native_effort); `provenance` is what the council does (native, user,
    routed, native_effort, or fallback when an automatic request resolved
    to native inheritance). Dispatch uses ONLY dispatch_model and
    dispatch_effort (None = that override is not sent); the requested
    values are kept for reporting. `note` is a fallback reason or a user-pin
    advisory.
    """
    mode: str
    provenance: str
    requested_model: Optional[str] = None
    requested_effort: Optional[str] = None
    dispatch_model: Optional[str] = None
    dispatch_effort: Optional[str] = None
    reason: Optional[str] = None
    note: Optional[str] = None


@dataclass(frozen=True)
class Role:
    id: str
    label: str
    instruction: str
    # Requested per-role Codex overrides exactly as authored (None = not
    # requested; omitting model, effort, and selection inherits Codex's
    # native configuration) and the selection object declaring their
    # provenance. What is actually sent is `decision`, resolved once at
    # launch before fan-out (see _role_decision).
    model: Optional[str] = None
    effort: Optional[str] = None
    selection: Optional[Selection] = None
    decision: Optional[SelectionDecision] = None


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


class _NullDiagnostics:
    """Write sink used after the real stderr is confirmed dead.

    Implements just enough of the text-stream surface (write/flush/isatty/
    encoding) for print() and interpreter-shutdown flushing; cheaper than
    holding a devnull descriptor open (no EMFILE risk).
    """

    encoding = "utf-8"

    def write(self, s):
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return False


# Diagnostics path for every advisory stderr write (progress, notices, stall
# diagnostics, interruption and runner-aborted messages, and the
# CODEX_COUNCIL_DONE sentinel).
# None means "use the live sys.stderr"; a dead stderr swaps in the no-op sink.
_diagnostics = {"stream": None}


def _diag(message):
    """Best-effort advisory stderr write.

    stderr is advisory: its death must never change role results or the
    process exit code (never exit 120). Terminal failures — BrokenPipeError,
    EBADF, or a closed-stream ValueError — permanently redirect all further
    diagnostics to a no-op sink (and close the dead stream so an
    interpreter-shutdown flush cannot fail); any other one-off OSError (e.g.
    a transient EAGAIN) is suppressed while the stream stays in use, so a
    hiccup does not silently swallow the sentinel.
    """
    if _diagnostics["stream"] is not None:
        return
    try:
        print(message, file=sys.stderr, flush=True)
    except (BrokenPipeError, ValueError):
        _retire_diagnostics_stream()
    except OSError as e:
        if e.errno == errno.EBADF:
            _retire_diagnostics_stream()


def _retire_diagnostics_stream():
    """Permanently stop writing diagnostics to the dead stderr."""
    _diagnostics["stream"] = _NullDiagnostics()
    with contextlib.suppress(Exception):
        sys.stderr.close()
    # Replace the module-visible stderr too so interpreter-shutdown flushing
    # and stray writers (e.g. asyncio's exception handler) cannot raise into
    # the exit path.
    sys.stderr = _diagnostics["stream"]


def _append_warning(existing, new):
    """Compose role warnings without overwriting earlier (higher-value) ones."""
    if not new:
        return existing
    if not existing:
        return new
    return f"{existing}; {new}"


def _plugin_version():
    """Best-effort plugin version for postmortem visibility; never raises.

    The manifest lives three directory levels above scripts/:
    plugins/codex-council/.claude-plugin/plugin.json.
    """
    try:
        manifest = (
            Path(__file__).resolve().parents[3] / ".claude-plugin" / "plugin.json"
        )
        with open(manifest, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, IndexError):
        return "unknown"
    version = data.get("version") if isinstance(data, dict) else None
    if isinstance(version, str) and version:
        return version
    return "unknown"


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


def _configured_codex_max_threads():
    """Read the user-level Codex agents.max_threads preference if available.

    Codex currently defaults this setting to 6. The council launches separate
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

def _iter_json_objects(jsonl_output):
    """Yield JSON object lines from a JSONL stream, skipping malformed lines.

    Split strictly on "\\n" (JSONL's record separator), never str.splitlines():
    splitlines also breaks on U+2028, U+2029, and U+0085, which are legal
    *unescaped* inside a JSON string. codex/serde_json can emit an agent_message
    containing one of those literally, and splitting there would tear the record
    into two invalid fragments — silently dropping a completed reply and turning
    a successful role into a failure.
    """
    for line in jsonl_output.split("\n"):
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


def extract_item_errors(jsonl_output):
    """Pull non-fatal ``item.completed`` error items from Codex JSONL stdout.

    Codex reports some advisories this way on runs that still succeed, e.g.
    "This session was recorded with model X but is resuming with Y" when a
    resumed thread runs under a different model override.
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


def _is_retriable_error(stderr_text):
    return _is_rate_limit_error(stderr_text) or _is_transient_5xx_error(stderr_text)


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


def _model_rejection_patterns(requested_model):
    """Codex's complete rejection sentences for the model this invocation
    sent (regex-escaped), or for any model when none was sent."""
    model = re.escape(requested_model) if requested_model else r"[^']+"
    return (
        # ChatGPT sign-in; the trailing account wording varies.
        re.compile(
            rf"The '{model}' model is not supported when using Codex with"
        ),
        # API wording: unavailable or inaccessible (the two are not told apart).
        re.compile(
            rf"The model '{model}' does not exist or you do not have access to it"
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


def _failure_verdict(failure_text, records, requested_model, resume=False):
    """Classify a non-zero codex exit; the stall verdict is decided earlier.

    Precedence, identical on the fresh and resume paths: auth -> quota ->
    anchored 429/5xx -> model rejected -> stale (resume only) -> substring
    retriable fallback -> None (untagged). Returns "auth", "quota",
    "rate-limit", "5xx", "model-rejected", "stale", or None.
    """
    if _is_auth_error(failure_text):
        return "auth"
    if _is_quota_error(failure_text, records):
        return "quota"
    anchored = _structured_retriable_class(failure_text)
    if anchored:
        return anchored
    if _model_rejection(failure_text, records, requested_model) is not None:
        return "model-rejected"
    if resume and _is_stale_resume_error(failure_text):
        return "stale"
    return _retriable_class(failure_text)


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
    JSON string. Item types other than the pure-text agent_message/reasoning
    (command executions, MCP tool calls, file changes, web searches, to-do
    lists, collab tool calls, and any unknown/future type) mark the attempt unsafe to replay —
    conservative by default, since replaying such a turn could duplicate
    side effects.
    """

    _SAFE_ITEM_TYPES = frozenset({"agent_message", "reasoning"})

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
        through the ordinary shared retry budget.
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
                f"{stall}s); no tool work had begun — retrying"
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


# One recovery action per provenance for a [model-rejected] failure. The
# runner never substitutes or replays: the host re-runs the role.
_MODEL_REJECTED_ACTIONS = {
    "routed": "Re-run this role with model, effort, and selection omitted "
              "to inherit native configuration.",
    "native_effort": "Re-run this role with model, effort, and selection "
                     "omitted to inherit native configuration.",
    "user": "Change or remove the explicit pin.",
    "native": "Update the Codex configuration (model) or pin an available "
              "model.",
    "fallback": "Update the Codex configuration (model) or pin an available "
                "model.",
}


def _model_rejected_error(decision, provider_message, phase):
    """The terminal [model-rejected] text: what was rejected, Codex's own
    message, and one action for this role's provenance."""
    if decision.dispatch_model:
        subject = f"requested model '{decision.dispatch_model}'"
    else:
        subject = "natively configured model"
    # Only a resume has a saved thread this failure could have touched.
    kept = " and the saved thread was kept" if phase == "resume" else ""
    return (
        f"[model-rejected] Codex rejected the {subject} for this invocation: "
        f"{provider_message.rstrip(' .')}. No substitute model was "
        f"tried{kept}. {_MODEL_REJECTED_ACTIONS[decision.provenance]}"
    )


def _classify_failure(failure_text, rc, phase, records=(), decision=None):
    """Return a tagged error string for a non-zero codex exit.

    `records` are the attempt's _failure_records and `decision` the role's
    SelectionDecision (inheritance when omitted). A stale resume is never
    formatted here: the resume path consults _failure_verdict first and
    restarts fresh; every other verdict is the same on both paths.
    """
    decision = decision or _INHERIT_DECISION
    verdict = _failure_verdict(failure_text, records, decision.dispatch_model)
    detail = failure_text or f"codex {phase} exited {rc}"
    if verdict in ("auth", "quota"):
        return f"[{verdict}] {detail}"
    if verdict in ("rate-limit", "5xx"):
        return f"[retriable:{verdict}] {detail}"
    if verdict == "model-rejected":
        return _model_rejected_error(
            decision,
            _model_rejection(failure_text, records, decision.dispatch_model),
            phase,
        )
    return detail


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
                # emitted_id is codex-controlled text: escape linebreaks so
                # it cannot start a forged err.log line (--follow reads it).
                _diag(f"[codex-council:{role.id}] {_report_inline(warning)}")
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
        # still restarts fresh.
        records = _failure_records(run.stdout)
        verdict = _failure_verdict(failure_text, records, model, resume=True)
        if verdict != "stale":
            err = _classify_failure(
                failure_text, run.returncode, "resume", records, decision
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
            f"[codex-council:{role.id}] session {_report_inline(session_id)} "
            f"(last used {_report_inline(updated)}) "
            f"is stale ({_report_inline(failure_text)}) — starting fresh."
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
        return f" reply={path}" if path else ""

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

# The report states only what the council SENT: codex exec's JSON stream
# names neither the model nor the effort that served a turn.
NO_AUTOMATIC_SELECTIONS = "no runtime-grounded selections"
MODEL_SELECTION_CAVEAT = (
    "codex exec does not report the model or effort that served a turn; "
    "values above are what the council sent, and \"native inheritance\" "
    "means no override was sent."
)


def _sent_values(model, effort):
    """"model X, effort Y" for whichever of the two is present."""
    parts = []
    if model:
        parts.append(f"model {model}")
    if effort:
        parts.append(f"effort {effort}")
    return ", ".join(parts)


def _selection_summary_note(decision):
    """The Summary-line note for what one role sent ("" = inherited)."""
    sent = _sent_values(decision.dispatch_model, decision.dispatch_effort)
    if decision.provenance == "user":
        return f" (explicit: {sent})"
    if decision.provenance == "routed":
        return f" (routed: {sent})"
    if decision.provenance == "native_effort":
        return (f" (routed effort: {decision.dispatch_effort} on native "
                f"model {decision.dispatch_model})")
    if decision.provenance == "fallback":
        return " (native inheritance; routing fell back)"
    return ""


def _selection_section_text(decision):
    """The `_Model selection: ..._` text every role section carries."""
    sent = _sent_values(decision.dispatch_model, decision.dispatch_effort)
    if decision.provenance == "user":
        text = f"explicit override — sent {sent}"
    elif decision.provenance == "routed":
        text = f"routed — sent {sent}"
    elif decision.provenance == "native_effort":
        text = (
            "routed effort on the native model — sent model "
            f"{decision.dispatch_model} (pinned native model), effort "
            f"{decision.dispatch_effort}"
        )
    elif decision.provenance == "fallback":
        requested = _sent_values(
            decision.requested_model, decision.requested_effort
        )
        return (f"native inheritance — routing fell back: {decision.note}; "
                f"requested {requested}")
    else:
        return "native inheritance (no model or effort override sent)"
    if decision.reason:
        text += f"; reason: {decision.reason}"
    if decision.note:  # a user-pin advisory
        text += f"; {decision.note}"
    return text


def _selection_plan_text(decision):
    """One role's preflight plan; automatic choices are revalidated."""
    sent = _sent_values(decision.dispatch_model, decision.dispatch_effort)
    if decision.provenance == "user":
        text = f"explicit override ({sent})"
        return f"{text}; {decision.note}" if decision.note else text
    if decision.provenance == "routed":
        return f"routed ({sent}); revalidated at launch"
    if decision.provenance == "native_effort":
        return (f"native-model effort (effort {decision.dispatch_effort} on "
                f"native model {decision.dispatch_model}); revalidated at "
                "launch")
    if decision.provenance == "fallback":
        if decision.note == ROUTING_OFF_NOTE:
            return "native inheritance (routing off)"
        return f"native inheritance (routing fell back: {decision.note})"
    return "native inheritance"


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


def _atomic_write_private(path, data):
    """Atomically replace `path` with the bytes `data` as a 0600 file.

    Temp file in the same directory (O_CREAT|O_EXCL|O_NOFOLLOW, mode 0600),
    full write, fsync, then os.replace — a reader never sees a partial file.
    Any failure removes the temp file and propagates; each caller owns its
    failure policy (reply files are advisory, a snapshot must not go stale).
    """
    directory = os.path.dirname(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    tmp_path = None
    try:
        for _ in range(8):
            candidate = os.path.join(
                directory,
                f".{os.path.basename(path)}.{os.getpid()}."
                f"{os.urandom(6).hex()}.tmp",
            )
            try:
                fd = os.open(candidate, flags, 0o600)
            except FileExistsError:
                continue
            tmp_path = candidate
            break
        else:
            raise FileExistsError("could not allocate a unique temp file")
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                os.remove(tmp_path)


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
    problem = None
    if stat.S_ISLNK(st.st_mode):
        problem = "is a symlink"
    elif not stat.S_ISDIR(st.st_mode):
        problem = "is not a directory"
    elif st.st_uid != os.geteuid():
        problem = f"is owned by uid {st.st_uid}"
    elif stat.S_IMODE(st.st_mode) & 0o077:
        problem = f"is mode {stat.S_IMODE(st.st_mode):04o}, not private"
    if problem:
        _diag(
            f"[codex-council] reply files disabled: {path!r} {problem}; "
            "the final report is unaffected"
        )
        return None
    return path


# Escapes for the FULL str.splitlines() boundary set beyond the plain space:
# \r \n \x0b \x0c \x1c \x1d \x1e U+0085 U+2028 U+2029 (matches LINEBREAK_CHARS).
_REPORT_INLINE_ESCAPES = str.maketrans({
    "\r": "\\r",
    "\n": "\\n",
    "\x0b": "\\x0b",
    "\x0c": "\\x0c",
    "\x1c": "\\x1c",
    "\x1d": "\\x1d",
    "\x1e": "\\x1e",
    "\x85": "\\u0085",
    "\u2028": "\\u2028",
    "\u2029": "\\u2029",
})


def _report_inline(value):
    """Keep report metadata on one line under any splitlines-based consumer.

    Escapes every character str.splitlines() treats as a boundary: \\r \\n
    \\x0b \\x0c \\x1c \\x1d \\x1e U+0085 U+2028 U+2029.
    """
    return str(value).translate(_REPORT_INLINE_ESCAPES)


# ---------- model discovery (codex app-server; metadata only) ----------
#
# --discover asks the installed Codex which models this account is shown and
# how native configuration resolves for the project, without ever starting a
# thread or a turn. The adapter is deliberately narrow: stdio JSON-RPC to
# `codex app-server`, five read-only methods, one monotonic deadline, and a
# strict projection into a small snapshot. Wire-format names stay inside the
# _normalize_* helpers; everything downstream reads only the snapshot. Any
# failure yields status "unavailable", which always means "inherit".


class _DiscoveryFailure(Exception):
    """The app-server session cannot continue; `problem` is machine-safe."""

    def __init__(self, problem, detail=None):
        super().__init__(problem)
        self.problem = problem
        # Sanitized last stderr line of a server that went away, or None.
        self.detail = detail


class _RpcError(Exception):
    """One request drew a JSON-RPC error; the session itself stays usable."""

    def __init__(self, method, code):
        self.problem = f"rpc_error:{method}:{code}"
        super().__init__(self.problem)


def _model_routing_mode():
    """Return "auto" or "off" from CODEX_COUNCIL_MODEL_ROUTING.

    Unset or empty means "auto": per-role routing is the skill's default.
    "off" disables automatic selection (explicit user pins still apply).
    Any other value is a usage error (exit 2), never a silent default.
    """
    raw = os.environ.get(MODEL_ROUTING_ENV, "")
    value = raw.strip()
    if value in ("", "auto"):
        return "auto"
    if value == "off":
        return "off"
    _usage_exit(f"{MODEL_ROUTING_ENV} must be 'auto' or 'off'; got {raw!r}.")


def _execution_context():
    """The process context discovery shares with every worker.

    Workers run the PATH-resolved `codex` with this process's cwd and
    environment and `-C <project root>`; discovery spawns the app-server the
    same way (no cwd or env override) and passes the project root as
    config/read's cwd, the parameter that selects project config layers.
    The runner forwards no profile, so `profile` is always null.
    CODEX_API_KEY is recorded as presence only: `codex exec` honors it but
    the app-server does not, so the catalog may not match exec's auth.
    """
    executable = shutil.which("codex")
    return {
        "project_root": _project_root(),
        "launch_cwd": os.getcwd(),
        "codex_executable": os.path.abspath(executable) if executable else None,
        "codex_cli_version": None,
        "codex_home": None,
        "profile": None,
        "exec_api_key_env": bool(os.environ.get("CODEX_API_KEY", "").strip()),
    }


# Snapshot context for a discovery that failed before observing anything.
_EMPTY_DISCOVERY_CONTEXT = {
    "project_root": None,
    "launch_cwd": None,
    "codex_executable": None,
    "codex_cli_version": None,
    "codex_home": None,
    "profile": None,
    "exec_api_key_env": False,
}


def _read_until_eof(stream, until, limit):
    """Read a pipe until EOF, `limit` bytes, or the monotonic time `until`."""
    fd = stream.fileno()
    os.set_blocking(fd, False)
    data = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        while len(data) < limit:
            remaining = until - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                break
            try:
                chunk = os.read(fd, _READ_CHUNK_BYTES)
            except BlockingIOError:
                continue
            except OSError:
                break
            if not chunk:
                break
            data += chunk
    return bytes(data[:limit])


def _process_group_gone(proc, grace):
    """Wait up to `grace` seconds for proc AND its whole process group.

    The leader is reaped as soon as it exits; the group is then probed with
    signal 0, so a descendant still holding a pipe keeps it "alive".
    """
    until = time.monotonic() + grace
    while True:
        if proc.poll() is not None:
            try:
                os.killpg(proc.pid, 0)
            except OSError:  # ESRCH: no member left (EPERM: not ours)
                return True
        if time.monotonic() >= until:
            return False
        time.sleep(0.01)


def _stop_process_group(proc):
    """Close stdin, then escalate SIGTERM -> SIGKILL over the process group.

    start_new_session=True made proc a group leader (pgid == pid). Each step
    waits DISCOVERY_CLOSE_GRACE_SECS for the WHOLE group, so neither a
    grandchild holding a pipe nor a server ignoring SIGTERM outlives
    discovery. Best effort; never raises.
    """
    if proc.stdin is not None:
        with contextlib.suppress(OSError, ValueError):
            proc.stdin.close()
    for sig in (None, signal.SIGTERM, signal.SIGKILL):
        if sig is not None:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, sig)
        if _process_group_gone(proc, DISCOVERY_CLOSE_GRACE_SECS):
            return


_CODEX_VERSION_RE = re.compile(r"codex-cli ([0-9][0-9A-Za-z.+-]{0,63})")


def _parse_codex_version(output):
    """The <ver> of `codex --version` output ("codex-cli <ver>"), or None."""
    lines = output.decode("utf-8", errors="replace").strip().splitlines()
    match = _CODEX_VERSION_RE.fullmatch(lines[0].strip()) if lines else None
    return match.group(1) if match else None


def _probe_codex_version(executable, deadline):
    """Run `codex --version` inside the discovery deadline; None if unknown.

    Capped at DISCOVERY_VERSION_TIMEOUT_SECS of its own. An unparsable or
    missing version is informational, never a discovery failure.
    """
    budget = min(DISCOVERY_VERSION_TIMEOUT_SECS, deadline - time.monotonic())
    if budget <= 0:
        return None
    try:
        proc = subprocess.Popen(
            [executable, "--version"], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, ValueError):
        return None
    try:
        output = _read_until_eof(proc.stdout, time.monotonic() + budget, 4096)
    finally:
        _stop_process_group(proc)
        with contextlib.suppress(OSError):
            proc.stdout.close()
    return _parse_codex_version(output)


_EMAIL_LIKE_RE = re.compile(r"[^\s@]+@[^\s@]+")


def _stderr_excerpt(tail):
    """Sanitized last non-empty line of a stderr tail, or None.

    Single-line (non-printable characters become spaces), bounded, and with
    email-like tokens redacted: it reaches the snapshot and --discover's
    stdout, which must never carry account identity.
    """
    text = bytes(tail).decode("utf-8", errors="replace")
    lines = [line for line in text.split("\n") if line.strip()]
    if not lines:
        return None
    line = "".join(ch if ch.isprintable() else " " for ch in lines[-1])
    line = _EMAIL_LIKE_RE.sub("<redacted>", line).strip()
    return line[:DISCOVERY_STDERR_EXCERPT_CHARS] or None


_PROBLEM_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,63}")


def _problem_token(value):
    """A server-supplied name as a machine-safe problem fragment."""
    if isinstance(value, str) and _PROBLEM_TOKEN_RE.fullmatch(value):
        return value
    return "unrecognized"


def _rpc_error_code(error):
    """The integer code of a JSON-RPC error object, or "malformed"."""
    code = error.get("code") if isinstance(error, dict) else None
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    return "malformed"


class _AppServerTransport:
    """Newline-delimited JSON-RPC over a running app-server's stdio pipes.

    Synchronous and deadline-bound: every wait is one selector poll against
    the shared monotonic deadline, so a hung, chatty, or hostile server can
    cost at most the remaining budget. Responses are matched by id while
    unsolicited messages interleave: notifications are counted and dropped;
    a server-to-client request is answered with JSON-RPC -32601 and its
    method recorded in server_requests (discovery is then inconclusive).
    """

    def __init__(self, proc, deadline):
        self.server_requests = []
        self.stderr_tail = bytearray()
        self._deadline = deadline
        self._next_id = 0
        self._stdin_fd = proc.stdin.fileno()
        self._stdout_fd = proc.stdout.fileno()
        self._stderr_fd = proc.stderr.fileno()
        self._outbox = bytearray()
        self._partial = bytearray()
        self._inbox = collections.deque()
        self._stdout_bytes = 0
        self._unsolicited = 0
        self._stdout_open = True
        self._stderr_open = True
        self._writing = False
        for fd in (self._stdin_fd, self._stdout_fd, self._stderr_fd):
            os.set_blocking(fd, False)
        self._selector = selectors.DefaultSelector()
        self._selector.register(self._stdout_fd, selectors.EVENT_READ)
        self._selector.register(self._stderr_fd, selectors.EVENT_READ)

    def close(self):
        self._selector.close()

    def notify(self, method):
        """Send a parameterless notification."""
        self._send({"method": method}, method)

    def request(self, method, params):
        """Send one request and return its result.

        Raises _RpcError for a JSON-RPC error response and _DiscoveryFailure
        when the session fails (deadline, exit, protocol violation, bound).
        """
        self._next_id += 1
        request_id = self._next_id
        self._send(
            {"id": request_id, "method": method, "params": params}, method
        )
        while True:
            message = self._receive(method)
            if "method" in message:
                self._count_unsolicited()
                if "id" in message:
                    self.server_requests.append(
                        _problem_token(message["method"])
                    )
                    self._send({"id": message["id"], "error": {
                        "code": -32601,
                        "message": "codex-council discovery does not "
                                   "handle server requests",
                    }}, method)
                continue
            response_id = message.get("id")
            if isinstance(response_id, bool) or response_id != request_id:
                self._count_unsolicited()
                continue
            if message.get("error") is not None:
                raise _RpcError(method, _rpc_error_code(message["error"]))
            if "result" not in message:
                raise _DiscoveryFailure(f"schema_unsupported:{method}:result")
            return message["result"]

    def _count_unsolicited(self):
        self._unsolicited += 1
        if self._unsolicited > DISCOVERY_MAX_UNSOLICITED:
            raise _DiscoveryFailure("protocol_error:notification_limit")

    def _send(self, message, method):
        """Queue one message and write whatever the pipe accepts now.

        The rest (if the pipe is full) is flushed by later selector rounds,
        always ahead of anything queued after it.
        """
        # json.dumps escapes to ASCII by default, so even a lone surrogate
        # in an echoed server value (cursor, request id) stays encodable.
        self._outbox += json.dumps(message).encode("ascii") + b"\n"
        self._write_some(method)

    def _set_write_interest(self, wanted):
        if wanted and not self._writing:
            self._selector.register(self._stdin_fd, selectors.EVENT_WRITE)
        elif self._writing and not wanted:
            self._selector.unregister(self._stdin_fd)
        self._writing = wanted

    def _receive(self, method):
        while not self._inbox:
            if not self._stdout_open:
                raise _DiscoveryFailure(
                    f"server_exited:{method}", self._drain_stderr()
                )
            self._pump(method)
        return self._inbox.popleft()

    def _pump(self, method):
        """One selector round: flush queued stdin bytes, read stdout/stderr."""
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise _DiscoveryFailure(f"timeout:{method}")
        for key, _ in self._selector.select(remaining):
            if key.fd == self._stdin_fd:
                self._write_some(method)
            elif key.fd == self._stdout_fd:
                self._read_stdout()
            else:
                self._read_stderr()

    def _write_some(self, method):
        try:
            written = os.write(self._stdin_fd, self._outbox)
        except BlockingIOError:
            written = 0
        except OSError:  # EPIPE: the server closed stdin or exited
            self._outbox.clear()
            self._set_write_interest(False)
            raise _DiscoveryFailure(
                f"server_exited:{method}", self._drain_stderr()
            ) from None
        del self._outbox[:written]
        self._set_write_interest(bool(self._outbox))

    def _read_stdout(self):
        try:
            chunk = os.read(self._stdout_fd, _READ_CHUNK_BYTES)
        except BlockingIOError:
            return
        except OSError:
            chunk = b""
        if not chunk:
            self._stdout_open = False
            self._selector.unregister(self._stdout_fd)
            return
        self._stdout_bytes += len(chunk)
        if self._stdout_bytes > DISCOVERY_MAX_STDOUT_BYTES:
            raise _DiscoveryFailure("protocol_error:stdout_limit")
        # Scan only the new bytes: the buffered prefix holds no newline.
        scan_from = len(self._partial)
        self._partial += chunk
        while True:
            newline = self._partial.find(b"\n", scan_from)
            if newline < 0:
                break
            line = bytes(self._partial[:newline])
            del self._partial[:newline + 1]
            scan_from = 0
            self._accept_line(line)
        if len(self._partial) > DISCOVERY_MAX_LINE_BYTES:
            raise _DiscoveryFailure("protocol_error:line_limit")

    def _accept_line(self, line):
        """Decode one stdout line; anything but a JSON object is fatal."""
        if len(line) > DISCOVERY_MAX_LINE_BYTES:
            raise _DiscoveryFailure("protocol_error:line_limit")
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            raise _DiscoveryFailure("protocol_error:invalid_utf8") from None
        if not text.strip():
            return
        try:
            message = json.loads(text)
        except (ValueError, RecursionError):
            raise _DiscoveryFailure("protocol_error:malformed_line") from None
        if not isinstance(message, dict):
            raise _DiscoveryFailure("protocol_error:malformed_line")
        self._inbox.append(message)

    def _read_stderr(self):
        try:
            chunk = os.read(self._stderr_fd, _READ_CHUNK_BYTES)
        except BlockingIOError:
            return
        except OSError:
            chunk = b""
        if not chunk:
            self._stderr_open = False
            self._selector.unregister(self._stderr_fd)
            return
        self.stderr_tail += chunk
        del self.stderr_tail[:-DISCOVERY_STDERR_TAIL_BYTES]

    def _drain_stderr(self):
        """Briefly collect a departing server's stderr; return the excerpt."""
        until = min(
            self._deadline, time.monotonic() + DISCOVERY_CLOSE_GRACE_SECS
        )
        with selectors.DefaultSelector() as selector:
            if self._stderr_open:
                selector.register(self._stderr_fd, selectors.EVENT_READ)
            while self._stderr_open:
                remaining = until - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    break
                self._read_stderr()
        return _stderr_excerpt(self.stderr_tail)


@contextlib.contextmanager
def _app_server_session(executable, deadline):
    """Spawn `codex app-server --listen stdio://` and yield its transport.

    Launched like a worker — the same PATH-resolved binary, the inherited
    cwd and environment, no --profile — in its own process group, and
    always torn down (_stop_process_group) however the session ends.
    """
    try:
        proc = subprocess.Popen(
            [executable, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True,
        )
    except (OSError, ValueError) as e:
        name = errno.errorcode.get(getattr(e, "errno", None), type(e).__name__)
        raise _DiscoveryFailure(f"spawn_failed:{name}") from None
    transport = None
    try:
        transport = _AppServerTransport(proc, deadline)
        yield transport
    finally:
        if transport is not None:
            transport.close()
        _stop_process_group(proc)
        for stream in (proc.stdout, proc.stderr):
            with contextlib.suppress(OSError):
                stream.close()


# ----- pure normalization of app-server results (the only wire-name code) -----

def _nonempty_str(value):
    return isinstance(value, str) and bool(value)


def _normalize_initialize(result):
    """initialize -> (codexHome or None, problem or None)."""
    if not isinstance(result, dict):
        return None, "schema_unsupported:initialize:result"
    codex_home = result.get("codexHome")
    return (codex_home if _nonempty_str(codex_home) else None), None


def _normalize_account(result):
    """account/read -> ({type, requires_openai_auth}, None) or (None, problem).

    Reads exactly two fields. Email, plan, account ids, workspace routing,
    and tokens are never read, so they cannot reach the snapshot or output.
    """
    if not isinstance(result, dict):
        return None, "schema_unsupported:account/read:result"
    requires = result.get("requiresOpenaiAuth")
    if not isinstance(requires, bool):
        return None, "schema_unsupported:account/read:requiresOpenaiAuth"
    account = result.get("account")
    if account is None:
        kind = None
    elif isinstance(account, dict) and _nonempty_str(account.get("type")):
        kind = account["type"]
    else:
        return None, "schema_unsupported:account/read:account.type"
    return {"type": kind, "requires_openai_auth": requires}, None


# (wire key, snapshot key) of the config/read values the snapshot records;
# the first two also record the KIND of layer that supplied them.
_CONFIG_FIELDS = (
    ("model", "model"),
    ("model_reasoning_effort", "effort"),
    ("model_provider", "provider"),
)


def _normalize_config(result):
    """config/read -> (configured values, None) or (None, problem).

    The merged values Codex resolved for the project root (null stays null)
    plus the layer kind (user, project, system, ...) each came from — never
    a file path or layer contents.
    """
    if not isinstance(result, dict):
        return None, "schema_unsupported:config/read:result"
    config, origins = result.get("config"), result.get("origins")
    if not isinstance(config, dict):
        return None, "schema_unsupported:config/read:config"
    if not isinstance(origins, dict):
        return None, "schema_unsupported:config/read:origins"
    values = {}
    for wire, key in _CONFIG_FIELDS:
        value = config.get(wire)
        if value is not None and not isinstance(value, str):
            return None, f"schema_unsupported:config/read:config.{wire}"
        values[key] = value
    for wire, key in _CONFIG_FIELDS[:2]:
        origin, kind = origins.get(wire), None
        if origin is not None:
            name = origin.get("name") if isinstance(origin, dict) else None
            kind = name.get("type") if isinstance(name, dict) else None
            if not _nonempty_str(kind):
                return None, f"schema_unsupported:config/read:origins.{wire}"
        values[f"{key}_origin"] = kind
    return values, None


def _normalize_requirements(result):
    """configRequirements/read -> (managed observation, None) or (None, problem).

    requirements null means none are managed ("absent"). A non-null
    models.newThread model or effort is "present": Codex applies it to new
    threads and drops BOTH when either is overridden, so neither routing
    nor native-model effort adjustment is safe. provider_keys names managed
    settings that can make the catalog describe a different provider.
    """
    problem = "schema_unsupported:configRequirements/read:requirements"
    if not isinstance(result, dict) or "requirements" not in result:
        return None, problem
    requirements = result["requirements"]
    observed = {"status": "absent", "model": None, "effort": None,
                "provider_keys": []}
    if requirements is None:
        return observed, None
    if not isinstance(requirements, dict):
        return None, problem
    models = requirements.get("models")
    if models is not None and not isinstance(models, dict):
        return None, f"{problem}.models"
    new_thread = (models or {}).get("newThread")
    if new_thread is not None and not isinstance(new_thread, dict):
        return None, f"{problem}.models.newThread"
    for wire, key in (("model", "model"), ("modelReasoningEffort", "effort")):
        value = (new_thread or {}).get(wire)
        if value is not None and not isinstance(value, str):
            return None, f"{problem}.models.newThread.{wire}"
        observed[key] = value
    if observed["model"] is not None or observed["effort"] is not None:
        observed["status"] = "present"
    provider = requirements.get("modelProvider")
    providers = requirements.get("modelProviders")
    catalog_json = requirements.get("modelCatalogJson")
    for wire, value, kind in (
        ("modelProvider", provider, str),
        ("modelProviders", providers, dict),
        ("modelCatalogJson", catalog_json, str),
    ):
        if value is not None and not isinstance(value, kind):
            return None, f"{problem}.{wire}"
    if provider is not None and provider != "openai":
        observed["provider_keys"].append("modelProvider")
    if providers:
        observed["provider_keys"].append("modelProviders")
    if catalog_json is not None:
        observed["provider_keys"].append("modelCatalogJson")
    return observed, None


def _utc_iso(seconds):
    """Unix seconds as "YYYY-MM-DDTHH:MM:SSZ", or None when out of range."""
    try:
        parts = time.gmtime(seconds)
    except (OverflowError, OSError, ValueError):
        return None
    if not 1 <= parts.tm_year <= 9999:
        return None
    return "%04d-%02d-%02dT%02d:%02d:%02dZ" % tuple(parts[:6])


def _normalize_efforts(value):
    """supportedReasoningEfforts -> [{effort, description}], or None.

    Effort values are an open vocabulary (any non-empty string); a
    malformed option or a repeated value makes the whole list unusable.
    """
    if not isinstance(value, list):
        return None
    efforts = []
    for option in value:
        if not isinstance(option, dict):
            return None
        effort = option.get("reasoningEffort")
        description = option.get("description")
        if not _nonempty_str(effort) or not isinstance(description, str):
            return None
        if any(known["effort"] == effort for known in efforts):
            return None
        efforts.append({"effort": effort, "description": description})
    return efforts


def _normalize_upgrade(value):
    """upgradeInfo -> ({model, retirement_at} or None, bad field or None)."""
    if value is None:
        return None, None
    if not isinstance(value, dict):
        return None, "upgradeInfo"
    if not _nonempty_str(value.get("model")):
        return None, "upgradeInfo.model"
    seconds = value.get("retirementAt")
    retirement_at = None
    if seconds is not None:
        # bool is an int subclass; true is not a timestamp.
        if isinstance(seconds, bool) or not isinstance(seconds, int):
            return None, "upgradeInfo.retirementAt"
        retirement_at = _utc_iso(seconds)
        if retirement_at is None:
            return None, "upgradeInfo.retirementAt"
    return {"model": value["model"], "retirement_at": retirement_at}, None


def _normalize_model_entry(item):
    """One wire model record -> (entry, None) or (None, first bad field).

    Additive fields are ignored; wrong types are rejected, never coerced
    (hidden "false" or a boolean retirementAt make the entry unusable). The
    dispatch identity is `model` — what `-m` receives — kept distinct from
    the picker `id`, which is recorded as catalog_id.
    """
    if not isinstance(item, dict):
        return None, "entry"
    for field in ("id", "displayName", "description"):
        if not isinstance(item.get(field), str):
            return None, field
    for field in ("model", "defaultReasoningEffort"):
        if not _nonempty_str(item.get(field)):
            return None, field
    for field in ("hidden", "isDefault"):
        if not isinstance(item.get(field), bool):
            return None, field
    efforts = _normalize_efforts(item.get("supportedReasoningEfforts"))
    if efforts is None:
        return None, "supportedReasoningEfforts"
    upgrade, bad_field = _normalize_upgrade(item.get("upgradeInfo"))
    if bad_field:
        return None, bad_field
    return {
        "model": item["model"],
        "catalog_id": item["id"],
        "display_name": item["displayName"],
        "description": item["description"],
        "hidden": item["hidden"],
        "recommended": item["isDefault"],
        "default_effort": item["defaultReasoningEffort"],
        "efforts": efforts,
        "upgrade": upgrade,
    }, None


def _normalize_model_page(result):
    """One model/list result -> ({entries, next_cursor}, None) or (None, problem).

    entries holds (entry, bad_field, model) triples: a normalized entry, or
    None with the first malformed field and the raw dispatch id when one is
    readable (so a malformed record can still disqualify its model).
    """
    if not isinstance(result, dict):
        return None, "schema_unsupported:model/list:result"
    data, next_cursor = result.get("data"), result.get("nextCursor")
    if not isinstance(data, list):
        return None, "schema_unsupported:model/list:data"
    if next_cursor is not None and not isinstance(next_cursor, str):
        return None, "schema_unsupported:model/list:nextCursor"
    entries = []
    for item in data:
        entry, bad_field = _normalize_model_entry(item)
        model = item.get("model") if isinstance(item, dict) else None
        entries.append((entry, bad_field, model if _nonempty_str(model) else None))
    return {"entries": entries, "next_cursor": next_cursor}, None


def _new_catalog():
    """Empty catalog accumulator: usable entries by dispatch id, models made
    unusable (malformed or conflicting) with the reason, and gaps that make
    the catalog incomplete."""
    return {"models": {}, "unusable": {}, "gaps": [], "count": 0}


def _catalog_gap(catalog, problems, code, text):
    problems.append(f"catalog_incomplete:{code}")
    catalog["gaps"].append(text)


def _mark_unusable(catalog, model, why):
    catalog["models"].pop(model, None)
    catalog["unusable"].setdefault(model, why)


def _merge_model_page(catalog, page, problems):
    """Fold one normalized page into the catalog accumulator.

    A malformed entry or a conflicting duplicate makes that model unusable
    and the catalog incomplete; identical duplicates collapse. Returns False
    when DISCOVERY_MAX_MODELS cut the page short.
    """
    for entry, bad_field, model in page["entries"]:
        if catalog["count"] >= DISCOVERY_MAX_MODELS:
            _catalog_gap(catalog, problems, "entry_bound",
                         f"stopped at the {DISCOVERY_MAX_MODELS}-entry bound")
            return False
        catalog["count"] += 1
        if entry is None:
            problems.append(f"schema_unsupported:model/list:{bad_field}")
            catalog["gaps"].append(f"malformed entries ({bad_field})")
            if model is not None:
                _mark_unusable(catalog, model, "malformed")
            continue
        model = entry["model"]
        existing = catalog["models"].get(model)
        if model in catalog["unusable"] or existing == entry:
            continue
        if existing is None:
            catalog["models"][model] = entry
            continue
        problems.append("catalog_conflict")
        catalog["gaps"].append("conflicting duplicate entries")
        _mark_unusable(catalog, model, "conflicting duplicate entries")
    return True


# ----- discovery orchestration (I/O) and the pure snapshot builder -----

def _collect_catalog(session, problems):
    """Page through model/list within the discovery bounds.

    Returns None when the catalog is unavailable (an RPC error or an
    unusable page), otherwise the merged accumulator, whose gaps say why it
    is incomplete. The exact opaque cursor is echoed back; a repeated
    cursor is a cycle. An incomplete catalog never implies that a model it
    lacks is unavailable.
    """
    catalog = _new_catalog()
    sent = set()
    cursor = None
    for _ in range(DISCOVERY_MAX_PAGES):
        params = {"limit": DISCOVERY_PAGE_LIMIT, "includeHidden": True}
        if cursor is not None:
            params["cursor"] = cursor
        try:
            page, problem = _normalize_model_page(
                session.request("model/list", params)
            )
        except _RpcError as e:
            page, problem = None, e.problem
        if problem:
            problems.append(problem)
            return None
        if not _merge_model_page(catalog, page, problems):
            return catalog
        cursor = page["next_cursor"]
        if cursor is None:
            return catalog
        if cursor in sent:
            _catalog_gap(catalog, problems, "cursor_cycle",
                         "pagination cursor repeated")
            return catalog
        if catalog["count"] >= DISCOVERY_MAX_MODELS:
            _catalog_gap(catalog, problems, "entry_bound",
                         f"stopped at the {DISCOVERY_MAX_MODELS}-entry bound")
            return catalog
        sent.add(cursor)
    _catalog_gap(catalog, problems, "page_bound",
                 f"stopped at the {DISCOVERY_MAX_PAGES}-page bound")
    return catalog


def _request_source(session, method, params, normalize, problems):
    """One metadata request, normalized; None (plus a problem) if unusable."""
    try:
        value, problem = normalize(session.request(method, params))
    except _RpcError as e:
        value, problem = None, e.problem
    if problem:
        problems.append(problem)
    return value


def _observe_codex(deadline):
    """Run the discovery I/O; return raw observations for _build_snapshot.

    Sends only initialize/initialized, account/read, config/read,
    configRequirements/read, and model/list — never thread/start,
    thread/resume, turn/start, or any login or account-changing method. A
    session failure keeps what was already observed and ends the session.
    """
    context = _execution_context()
    observed = {
        "context": context, "problems": [], "conclusive": False,
        "account": None, "configured": None, "managed": None, "catalog": None,
    }
    problems = observed["problems"]
    executable = context["codex_executable"]
    if executable is None:
        problems.append("codex_missing")
        return observed
    context["codex_cli_version"] = _probe_codex_version(executable, deadline)
    if context["codex_cli_version"] is None:
        problems.append("codex_version_unavailable")
    session = None
    try:
        with _app_server_session(executable, deadline) as session:
            codex_home, problem = _normalize_initialize(session.request(
                "initialize", {
                    "clientInfo": {
                        "name": "codex-council", "title": "Codex Council",
                        "version": _plugin_version(),
                    },
                    "capabilities": {"experimentalApi": False},
                },
            ))
            if problem:
                raise _DiscoveryFailure(problem)
            context["codex_home"] = codex_home
            session.notify("initialized")
            observed["account"] = _request_source(
                session, "account/read", {"refreshToken": False},
                _normalize_account, problems,
            )
            observed["configured"] = _request_source(
                session, "config/read",
                {"cwd": context["project_root"], "includeLayers": False},
                _normalize_config, problems,
            )
            observed["managed"] = _request_source(
                session, "configRequirements/read", None,
                _normalize_requirements, problems,
            )
            observed["catalog"] = _collect_catalog(session, problems)
    except (_RpcError, _DiscoveryFailure) as failure:
        problems.append(failure.problem)
        detail = getattr(failure, "detail", None)
        if detail:
            problems.append(f"stderr: {detail}")
    server_requests = session.server_requests if session is not None else []
    problems.extend(f"server_request:{method}" for method in server_requests)
    observed["conclusive"] = not server_requests and all(
        observed[key] is not None
        for key in ("account", "configured", "managed", "catalog")
    )
    return observed


_EXEC_API_KEY_REASON = (
    "CODEX_API_KEY is set for codex exec but not visible to discovery"
)


def _provider_mismatch(configured, provider_keys):
    """Why the catalog may not describe the provider workers use, or None."""
    provider = configured["provider"]
    if provider not in (None, "openai"):
        return f"configured provider {provider!r} has no verified catalog"
    if provider_keys:
        return (
            f"managed requirements set {', '.join(provider_keys)}; "
            "provider correspondence unverified"
        )
    return None


def _routing_reasons(routing_mode, status_ok, problems, account, mismatch,
                     api_key_env, managed_status, catalog):
    """Every reason automatic routing is unavailable (empty = eligible)."""
    reasons = []
    if routing_mode == "off":
        reasons.append(f"{MODEL_ROUTING_ENV}=off")
    if not status_ok:
        reasons.append("discovery unavailable: " + ", ".join(problems))
        return reasons
    if catalog["gaps"]:
        reasons.append(
            "catalog incomplete: "
            + "; ".join(_dedupe_preserve_order(catalog["gaps"]))
        )
    if account["type"] is None:
        reasons.append("not signed in: catalog is not account-grounded")
    if mismatch:
        reasons.append(mismatch)
    if api_key_env:
        reasons.append(_EXEC_API_KEY_REASON)
    if managed_status != "absent":
        reasons.append(f"managed new-thread defaults {managed_status}")
    return reasons


def _native_resolution(status_ok, configured, managed_status, mismatch,
                       api_key_env, catalog):
    """Whether the model an override-free worker runs is proven, and which.

    Proven only when nothing can divert Codex from the configured model
    (no managed new-thread defaults, a corresponding provider, no exec-only
    API key) AND a well-formed catalog entry for that exact model exists
    (hidden allowed), so its advertised efforts are known.
    """
    model = configured["model"]
    if not status_ok:
        reason = "discovery unavailable"
    elif managed_status != "absent":
        reason = f"managed new-thread defaults {managed_status}"
    elif mismatch:
        reason = mismatch
    elif api_key_env:
        reason = _EXEC_API_KEY_REASON
    elif model is None:
        reason = ("no model is configured; Codex's built-in default is not "
                  "observable")
    elif model in catalog["unusable"]:
        reason = (f"the catalog entry for configured model {model!r} is "
                  f"{catalog['unusable'][model]}")
    elif model not in catalog["models"]:
        reason = f"configured model {model!r} is not in the discovered catalog"
        if catalog["gaps"]:
            reason += " (catalog incomplete)"
    else:
        return {"resolution": "proven", "model": model, "reason": None}
    return {"resolution": "unknown", "model": None, "reason": reason}


def _build_snapshot(*, snapshot_id, created_at, plugin_version, routing_mode,
                    context, problems, conclusive, account, configured,
                    managed, catalog):
    """Assemble the run's model snapshot from discovery observations (pure).

    Routing eligibility and native resolution are decided once, here, so
    the --discover summary, preflight, and launch read the same verdicts
    and reasons. An unavailable source becomes explicit nulls or "unknown";
    a missing observation never defaults to a permissive value.
    """
    problems = _dedupe_preserve_order(problems)
    account = account or {"type": None, "requires_openai_auth": None}
    configured = configured or dict.fromkeys(
        ("model", "effort", "provider", "model_origin", "effort_origin")
    )
    managed = managed or {"status": "unknown", "model": None, "effort": None,
                          "provider_keys": []}
    complete = catalog is not None and not catalog["gaps"]
    catalog = catalog or _new_catalog()
    mismatch = _provider_mismatch(configured, managed["provider_keys"])
    api_key_env = bool(context["exec_api_key_env"])
    reasons = _routing_reasons(
        routing_mode, conclusive, problems, account, mismatch, api_key_env,
        managed["status"], catalog,
    )
    return {
        "schema": SNAPSHOT_SCHEMA,
        "snapshot_id": snapshot_id,
        "created_at": created_at,
        "plugin_version": plugin_version,
        "status": "ok" if conclusive else "unavailable",
        "problems": problems,
        "context": dict(context),
        "account": dict(account),
        "configured": dict(configured),
        "managed_defaults": {
            key: managed[key] for key in ("status", "model", "effort")
        },
        "native": _native_resolution(
            conclusive, configured, managed["status"], mismatch, api_key_env,
            catalog,
        ),
        "routing": {
            "mode": routing_mode, "eligible": not reasons, "reasons": reasons,
        },
        "catalog": {
            "complete": complete, "models": list(catalog["models"].values()),
        },
    }


def _discover(routing_mode):
    """Run bounded, metadata-only model discovery; return the snapshot dict.

    Never raises (Ctrl+C aside): codex missing, spawn errors, timeouts,
    protocol violations, RPC errors, and server requests all yield status
    "unavailable" with machine-safe problems, which every caller treats as
    "inherit native configuration". Routing mode "off" still discovers
    (the summary stays informative) but records routing as ineligible.
    """
    deadline = time.monotonic() + DISCOVERY_TIMEOUT_SECS
    try:
        observed = _observe_codex(deadline)
    except Exception as e:  # a discovery bug must never cost a council
        observed = {
            "context": dict(_EMPTY_DISCOVERY_CONTEXT),
            "problems": [f"internal_error:{type(e).__name__}"],
            "conclusive": False, "account": None, "configured": None,
            "managed": None, "catalog": None,
        }
    return _build_snapshot(
        snapshot_id=secrets.token_hex(8),
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        plugin_version=_plugin_version(),
        routing_mode=routing_mode,
        **observed,
    )


# ----- the snapshot file and the --discover summary -----

def _write_snapshot(run_dir, snapshot):
    """Atomically write RUNDIR/model-snapshot.json (0600); return its path.

    On any failure the previous snapshot, if one exists, is removed (best
    effort) before the error propagates: evidence from an earlier discovery
    must never be read back as this run's.
    """
    path = os.path.join(run_dir, SNAPSHOT_FILENAME)
    try:
        # ASCII JSON: even a lone surrogate in catalog text stays writable.
        data = (json.dumps(snapshot, indent=2) + "\n").encode("ascii")
        _atomic_write_private(path, data)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(path)
        raise
    return path


def _reject_duplicate_keys(pairs):
    """json object_pairs_hook: build the object, refusing a repeated key."""
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate JSON key {key!r}")
        obj[key] = value
    return obj


def _reject_non_finite(name):
    """json parse_constant hook: refuse NaN, Infinity, and -Infinity."""
    raise ValueError(f"non-finite number {name}")


def _strict_json_loads(text):
    """json.loads that rejects duplicate keys and non-finite numbers."""
    return json.loads(
        text, object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_non_finite,
    )


_MISSING = object()
_ISO_UTC_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
_SNAPSHOT_ID_RE = re.compile(r"[0-9a-f]{16}")


def _dotted(data, path):
    """data["a"]["b"] for path "a.b", or _MISSING when a level is absent."""
    for key in path.split("."):
        if not isinstance(data, dict) or key not in data:
            return _MISSING
        data = data[key]
    return data


def _is_str_or_none(value):
    return value is None or isinstance(value, str)


def _is_bool_or_none(value):
    return value is None or isinstance(value, bool)


def _is_str_list(value):
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _one_of(*allowed):
    return lambda value: isinstance(value, str) and value in allowed


def _matches(pattern):
    return lambda value: isinstance(value, str) and bool(pattern.fullmatch(value))


# Every snapshot field a reader relies on, with its required shape.
_SNAPSHOT_CHECKS = (
    ("schema", _one_of(SNAPSHOT_SCHEMA)),
    ("snapshot_id", _matches(_SNAPSHOT_ID_RE)),
    ("created_at", _matches(_ISO_UTC_RE)),
    ("plugin_version", lambda value: isinstance(value, str)),
    ("status", _one_of("ok", "unavailable")),
    ("problems", _is_str_list),
    *((f"context.{key}", _is_str_or_none) for key in (
        "project_root", "launch_cwd", "codex_executable",
        "codex_cli_version", "codex_home", "profile",
    )),
    ("context.exec_api_key_env", lambda value: isinstance(value, bool)),
    ("account.type", _is_str_or_none),
    ("account.requires_openai_auth", _is_bool_or_none),
    *((f"configured.{key}", _is_str_or_none) for key in (
        "model", "effort", "provider", "model_origin", "effort_origin",
    )),
    ("managed_defaults.status", _one_of("present", "absent", "unknown")),
    ("managed_defaults.model", _is_str_or_none),
    ("managed_defaults.effort", _is_str_or_none),
    ("native.resolution", _one_of("proven", "unknown")),
    ("native.model", _is_str_or_none),
    ("native.reason", _is_str_or_none),
    ("routing.mode", _one_of("auto", "off")),
    ("routing.eligible", lambda value: isinstance(value, bool)),
    ("routing.reasons", _is_str_list),
    ("catalog.complete", lambda value: isinstance(value, bool)),
    ("catalog.models", lambda value: isinstance(value, list)),
)


def _is_snapshot_model(entry):
    """True when a snapshot catalog entry has the normalized shape."""
    if not isinstance(entry, dict):
        return False
    if not all(_nonempty_str(entry.get(k)) for k in ("model", "default_effort")):
        return False
    if not all(isinstance(entry.get(k), str)
               for k in ("catalog_id", "display_name", "description")):
        return False
    if not all(isinstance(entry.get(k), bool) for k in ("hidden", "recommended")):
        return False
    efforts = entry.get("efforts")
    if not isinstance(efforts, list) or not all(
        isinstance(option, dict) and _nonempty_str(option.get("effort"))
        and isinstance(option.get("description"), str)
        for option in efforts
    ):
        return False
    upgrade = entry.get("upgrade", _MISSING)
    if upgrade is None:
        return True
    if not isinstance(upgrade, dict) or not _nonempty_str(upgrade.get("model")):
        return False
    retirement_at = upgrade.get("retirement_at", _MISSING)
    return retirement_at is None or _matches(_ISO_UTC_RE)(retirement_at)


def _snapshot_shape_problem(snapshot):
    """The first field of a parsed snapshot that breaks the schema, or None."""
    for path, valid in _SNAPSHOT_CHECKS:
        if not valid(_dotted(snapshot, path)):
            return path
    models = snapshot["catalog"]["models"]
    if not all(_is_snapshot_model(entry) for entry in models):
        return "catalog.models"
    if len({entry["model"] for entry in models}) != len(models):
        return "catalog.models"
    native = snapshot["native"]
    if native["resolution"] == "proven" and not any(
        entry["model"] == native["model"] for entry in models
    ):
        return "native.model"
    return None


def _snapshot_file_problem(st):
    """Why a stat result is not the private file --discover writes, or None."""
    if stat.S_ISLNK(st.st_mode):
        return f"{SNAPSHOT_FILENAME} is a symlink"
    if not stat.S_ISREG(st.st_mode):
        return f"{SNAPSHOT_FILENAME} is not a regular file"
    if st.st_uid != os.geteuid():
        return f"{SNAPSHOT_FILENAME} is owned by uid {st.st_uid}"
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        return (f"{SNAPSHOT_FILENAME} is mode {mode:04o}, not the private "
                "0600 file --discover writes")
    return None


def _read_snapshot(run_dir):
    """Load RUNDIR/model-snapshot.json: (snapshot, None) or (None, problem).

    Accepts only the private regular file --discover writes: lstat refuses
    a symlink, special file, foreign owner, or group/other mode bits; the
    open adds O_NOFOLLOW|O_NONBLOCK (no FIFO hang, no swap after the lstat);
    the content must be strict JSON (no duplicate keys or non-finite
    numbers) in the SNAPSHOT_SCHEMA shape. `problem` is a short sentence
    fragment for the caller's diagnostic.
    """
    path = os.path.join(run_dir, SNAPSHOT_FILENAME)
    try:
        problem = _snapshot_file_problem(os.lstat(path))
    except FileNotFoundError:
        return None, f"{SNAPSHOT_FILENAME} does not exist"
    except OSError as e:
        return None, f"cannot inspect {SNAPSHOT_FILENAME} ({e.strerror or e})"
    if problem:
        return None, problem
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        with open(os.open(path, flags), "rb") as f:
            problem = _snapshot_file_problem(os.fstat(f.fileno()))
            raw = b"" if problem else f.read(SNAPSHOT_MAX_BYTES + 1)
    except OSError as e:
        return None, f"cannot read {SNAPSHOT_FILENAME} ({e.strerror or e})"
    if problem:
        return None, problem
    if len(raw) > SNAPSHOT_MAX_BYTES:
        return None, f"{SNAPSHOT_FILENAME} exceeds {SNAPSHOT_MAX_BYTES} bytes"
    try:
        snapshot = _strict_json_loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as e:
        return None, f"{SNAPSHOT_FILENAME} is not valid JSON ({e})"
    field = _snapshot_shape_problem(snapshot)
    if field:
        return None, (f"{SNAPSHOT_FILENAME} does not match {SNAPSHOT_SCHEMA} "
                      f"(field {field!r})")
    return snapshot, None


def _quoted(text):
    """Catalog text as one quoted literal (it is data, not instructions)."""
    return json.dumps(text, ensure_ascii=False)


def _summary_setting(value, origin):
    if value is None:
        return "unset"
    return f"{value} (origin {origin})" if origin else value


def _summary_efforts(entry):
    return ", ".join(
        f"{option['effort']} ({_quoted(option['description'])})"
        for option in entry["efforts"]
    ) or "none advertised"


def _summary_model_line(entry):
    line = (f"- {entry['model']} — {_quoted(entry['description'])}; "
            f"efforts: {_summary_efforts(entry)}")
    if entry["recommended"]:
        line += "; recommended"
    upgrade = entry["upgrade"]
    if upgrade is not None:
        if upgrade["retirement_at"] is not None:
            line += f"; retires {upgrade['retirement_at']}"
        line += f"; upgrade suggested: {upgrade['model']}"
    return line


def _discovery_summary(snapshot):
    """The --discover stdout summary lines (the snapshot path excluded).

    Compact on purpose: Claude reads it to choose per-role selections. Every
    line goes through _report_inline because catalog and config text is
    untrusted data; descriptions are additionally JSON-quoted.
    """
    snapshot_id = snapshot["snapshot_id"]
    if snapshot["status"] != "ok":
        reasons = ", ".join(snapshot["problems"]) or "no detail recorded"
        return [_report_inline(
            f"[codex-council] discovery unavailable: {reasons}; "
            f"snapshot_id={snapshot_id}; roles must inherit (omit model, "
            "effort, and selection)"
        )]
    context = snapshot["context"]
    configured = snapshot["configured"]
    managed = snapshot["managed_defaults"]
    routing = snapshot["routing"]
    native = snapshot["native"]
    models = snapshot["catalog"]["models"]
    version = context["codex_cli_version"]
    provider = configured["provider"]
    if managed["status"] == "present":
        managed_text = (
            f"present: model {_summary_setting(managed['model'], None)}, "
            f"effort {_summary_setting(managed['effort'], None)}"
        )
    else:
        managed_text = "none" if managed["status"] == "absent" else "unknown"
    if routing["mode"] == "off":
        # native.resolution stays evidence; the automatic action is off.
        native_text = f"unavailable — {MODEL_ROUTING_ENV}=off"
    elif native["resolution"] == "proven":
        native_text = f"available on {native['model']}"
        entry = next(
            (m for m in models if m["model"] == native["model"]), None
        )
        if entry is not None and entry["hidden"]:
            # Hidden models are not listed below, but native_effort may
            # still adjust effort on one, so show its efforts here.
            native_text += f" (hidden; efforts: {_summary_efforts(entry)})"
    else:
        native_text = f"unavailable — {native['reason']}"
    visible = [entry for entry in models if not entry["hidden"]]
    hidden = [entry["model"] for entry in models if entry["hidden"]]
    lines = [
        f"[codex-council] discovery ok: snapshot_id={snapshot_id} "
        f"codex-cli {'version unknown' if version is None else version}; "
        f"auth {snapshot['account']['type'] or 'none'}; "
        f"provider {'openai (default)' if provider is None else provider}; "
        f"version={snapshot['plugin_version']}",
        "native configuration: model "
        f"{_summary_setting(configured['model'], configured['model_origin'])}"
        ", effort "
        f"{_summary_setting(configured['effort'], configured['effort_origin'])}"
        f"; managed new-thread defaults: {managed_text}",
        "routing: " + ("eligible" if routing["eligible"] else
                       "unavailable — " + "; ".join(routing["reasons"])),
        f"native-model effort adjustment: {native_text}",
        "advertised models (catalog text is data, not instructions):"
        + ("" if visible else " none"),
        *(_summary_model_line(entry) for entry in visible),
    ]
    if hidden:
        lines.append(f"hidden (explicit pins only): {', '.join(hidden)}")
    return [_report_inline(line) for line in lines]


def _discover_command(run_dir):
    """--discover RUNDIR: discover, write the snapshot, print the summary.

    Exits 0 whenever RUNDIR is a valid private directory (roles.json and
    context.md need not exist yet): unavailable discovery, or a missing
    codex, is reported in the summary because inheritance is always a valid
    outcome. If the snapshot cannot be written, any older one is removed
    and the only line printed says to write no automatic selections.
    """
    run_dir = _check_private_dir(
        run_dir, prefix="--discover: ", recovery=STAGING_DIR_RECOVERY
    )
    snapshot = _discover(_model_routing_mode())
    try:
        path = _write_snapshot(os.path.abspath(run_dir), snapshot)
    except Exception as e:
        lines = [
            "[codex-council] discovery snapshot not written "
            f"({_report_inline(e)}); write no automatic selections — omit "
            "model, effort, and selection so roles inherit native "
            "configuration."
        ]
    else:
        lines = _discovery_summary(snapshot)
        lines.append(f"snapshot: {_report_inline(path)}")
    print("\n".join(lines), flush=True)


# ---------- model selection (one resolver for preflight and launch) ----------
#
# A role inherits (no model, effort, or selection), carries an explicit user
# pin, or carries a runtime-grounded choice ("routed" pair or
# "native_effort") bound to this run's --discover snapshot. Authoring
# defects exit 2 (_validate_selection_authoring); evidence that stops
# supporting a valid automatic choice resolves it to native inheritance with
# a reason (_resolve_selection). Nothing here ranks models or efforts: every
# check is exact membership in the discovered data, which is untrusted text
# and reaches output only through _report_inline.

ROUTING_OFF_NOTE = f"{MODEL_ROUTING_ENV}=off"
PROVENANCES = ("native", "user", "routed", "native_effort", "fallback")
UNVERIFIED_MODEL_ADVISORY = (
    "unverified: not in the discovered catalog; forwarded unchanged"
)
PARTIAL_PIN_ADVISORY = (
    "partial pin: Codex ignores managed new-thread model and effort "
    "defaults when either is overridden"
)
_INHERIT_DECISION = SelectionDecision("inherit", "native")


def _is_automatic(role):
    return role.selection is not None and role.selection.mode in AUTOMATIC_MODES


def _role_decision(role):
    """The decision attached at launch; without one (a Role built directly),
    the decision its request implies when no discovery evidence exists."""
    if role.decision is not None:
        return role.decision
    return _resolve_selection(role, None, None, "auto", None)


def _catalog_entry(snapshot, model):
    """The snapshot catalog entry whose dispatch id is exactly `model`."""
    for entry in snapshot["catalog"]["models"]:
        if entry["model"] == model:
            return entry
    return None


def _effort_advertised(entry, effort):
    """Exact, case-sensitive membership in the entry's advertised efforts."""
    return any(option["effort"] == effort for option in entry["efforts"])


def _execution_id_hint(snapshot, model):
    """Point at the dispatch id when `model` is an entry's picker id or
    display name (neither is what `-m` receives)."""
    for entry in snapshot["catalog"]["models"]:
        if model in (entry["catalog_id"], entry["display_name"]):
            return f"; use the execution id '{entry['model']}'"
    return ""


def _routed_evidence_gap(snapshot, model, effort, now):
    """Why `snapshot` does not support routing to (model, effort), or None.

    Exact checks only: the dispatch id is advertised and visible, its
    advertised retirement (if any) has not passed at `now`, and the effort
    is one that entry advertises. Catalog order and the recommended marker
    never matter.
    """
    entry = _catalog_entry(snapshot, model)
    if entry is None:
        return (
            f"model '{model}' is not an advertised execution id in snapshot "
            f"{snapshot['snapshot_id']}{_execution_id_hint(snapshot, model)}"
        )
    if entry["hidden"]:
        return (f"cannot route to hidden model '{model}' (hidden models are "
                "for explicit user pins)")
    upgrade = entry["upgrade"]
    retirement = upgrade["retirement_at"] if upgrade else None
    if retirement is not None and now is not None and retirement <= now:
        return f"model '{model}' advertised retirement passed ({retirement})"
    if not _effort_advertised(entry, effort):
        return f"effort '{effort}' is not advertised for model '{model}'"
    return None


def _native_model_gap(snapshot):
    """Why the snapshot proves no native model the runner could pin."""
    native = snapshot["native"]
    model = native["model"]
    if native["resolution"] != "proven":
        reason = native["reason"] or "native model not proven"
    elif not (isinstance(model, str) and SELECTION_VALUE_PATTERN.match(model)):
        reason = (f"native model id {model!r} is not a dispatchable "
                  "selection value")
    else:
        return None
    return f"cannot adjust effort on the native model: {reason}"


def _native_effort_gap(snapshot, effort):
    """Why `snapshot` does not support `effort` on its proven native model."""
    gap = _native_model_gap(snapshot)
    if gap:
        return gap
    model = snapshot["native"]["model"]
    entry = _catalog_entry(snapshot, model)
    if entry is None or not _effort_advertised(entry, effort):
        return f"effort '{effort}' is not advertised for model '{model}'"
    return None


def _user_pin_advisory(role, evidence):
    """Advisory text for an explicit pin, or None; never a rejection.

    A pin reaches Codex unchanged whatever the catalog says (a custom
    provider's models are not in it); the snapshot only annotates it.
    """
    if evidence is None:
        return None
    notes = []
    if evidence["status"] == "ok":
        entry = None
        if role.model is not None:
            entry = _catalog_entry(evidence, role.model)
            if entry is None:
                notes.append(UNVERIFIED_MODEL_ADVISORY)
        elif evidence["native"]["resolution"] == "proven":
            entry = _catalog_entry(evidence, evidence["native"]["model"])
        if (
            entry is not None and role.effort is not None
            and not _effort_advertised(entry, role.effort)
        ):
            notes.append(
                f"unverified effort: '{role.effort}' is not advertised for "
                f"model '{entry['model']}'; forwarded unchanged"
            )
    partial = (role.model is None) != (role.effort is None)
    if partial and evidence["managed_defaults"]["status"] != "absent":
        notes.append(PARTIAL_PIN_ADVISORY)
    return "; ".join(notes) or None


def _decision(role, mode, provenance, model=None, effort=None, note=None):
    """A SelectionDecision that keeps the role's request for reporting."""
    reason = role.selection.reason if role.selection else None
    return SelectionDecision(
        mode, provenance, role.model, role.effort, model, effort, reason, note
    )


def _resolve_selection(role, planning, launch, routing_mode, now):
    """Decide what one role sends: the single resolver for preflight and
    launch. Pure — the same inputs always give the same decision.

    `planning` is this run's --discover snapshot and `launch` the fresh
    launch-time one (either may be None; preflight passes no launch
    snapshot, so its decisions are the plan). Launch evidence wins when
    present. `now` ("YYYY-MM-DDTHH:MM:SSZ", or None to skip) is compared
    with advertised retirements. Authoring defects never reach here (see
    _validate_selection_authoring): an automatic choice that the evidence
    does not support resolves to native inheritance, with the reason, and
    an explicit pin is forwarded unchanged with advisories at most.
    """
    selection = role.selection
    if selection is None and role.model is None and role.effort is None:
        return _INHERIT_DECISION
    evidence = launch if launch is not None else planning
    if selection is None or selection.mode == "user":
        # An untagged pin gets here only from direct CLI use.
        return _decision(role, "user", "user", role.model, role.effort,
                         _user_pin_advisory(role, evidence))
    mode = selection.mode
    if routing_mode == "off":
        return _decision(role, mode, "fallback", note=ROUTING_OFF_NOTE)
    if evidence is None:
        return _decision(role, mode, "fallback",
                         note="no discovery snapshot for this run")
    stage = "launch discovery" if launch is not None else "discovery snapshot"
    if evidence["status"] != "ok":
        problems = ", ".join(evidence["problems"]) or "no detail recorded"
        return _decision(role, mode, "fallback",
                         note=f"{stage} unavailable: {problems}")
    if mode == "routed":
        if not evidence["routing"]["eligible"]:
            reasons = "; ".join(evidence["routing"]["reasons"])
            return _decision(
                role, mode, "fallback",
                note=f"{stage} reports routing unavailable: {reasons}",
            )
        gap = _routed_evidence_gap(evidence, role.model, role.effort, now)
        model = role.model
    else:
        # native_effort pins the native model THIS evidence proves.
        gap = _native_effort_gap(evidence, role.effort)
        model = evidence["native"]["model"]
    if gap:
        if launch is not None:
            gap = f"selection evidence changed since discovery: {gap}"
        return _decision(role, mode, "fallback", note=gap)
    return _decision(role, mode, mode, model, role.effort)


def _authoring_problem(role, planning, planning_problem, now):
    """Why an automatic selection is an authoring defect against this run's
    planning snapshot, or None."""
    selection = role.selection
    if planning is None:
        return (
            f"selection.mode '{selection.mode}' requires this run's "
            f"discovery snapshot ({planning_problem}); run --discover on "
            "this run directory first, or omit model, effort, and "
            "selection to inherit"
        )
    if selection.snapshot_id != planning["snapshot_id"]:
        return (
            f"snapshot_id '{selection.snapshot_id}' does not identify this "
            f"run's discovery snapshot '{planning['snapshot_id']}'"
        )
    if selection.mode == "native_effort":
        gap = _native_model_gap(planning)
        if gap:
            return f"{gap}; omit effort and selection to inherit"
        return _native_effort_gap(planning, role.effort)
    if not planning["routing"]["eligible"]:
        return (
            "discovery reported routing unavailable "
            f"({'; '.join(planning['routing']['reasons'])}); omit model, "
            "effort, and selection to inherit"
        )
    return _routed_evidence_gap(planning, role.model, role.effort, now)


def _validate_selection_authoring(roles, planning, planning_problem,
                                  routing_mode, now):
    """Exit 2 (uniform rewrite recovery) on an automatic selection this
    run's planning snapshot does not support.

    Runs in preflight and at launch, before any worker. Explicit pins are
    never checked against the catalog, and with routing off automatic roles
    are not errors: they resolve to native inheritance.
    """
    if routing_mode == "off":
        return
    for idx, role in enumerate(roles):
        if not _is_automatic(role):
            continue
        problem = _authoring_problem(role, planning, planning_problem, now)
        if problem:
            _roles_usage_exit(_report_inline(
                f"--roles-file entry {idx} (id '{role.id}'): {problem}."
            ))


def _launch_discovery_state(routing_mode, automatic, launch):
    """(state, reason) of launch discovery for err.log and the report.

    state is "ok", "unavailable", or "not-run"; reason is None or why.
    """
    if launch is None:
        if automatic and routing_mode == "off":
            return "not-run", ROUTING_OFF_NOTE
        return "not-run", NO_AUTOMATIC_SELECTIONS
    if launch["status"] != "ok":
        return "unavailable", (
            ", ".join(launch["problems"]) or "no detail recorded"
        )
    if not launch["routing"]["eligible"]:
        return "ok", launch["routing"]["reasons"][0]
    return "ok", None


def _discovery_sentence(state, reason, launch):
    """The discovery half of the report's Model selection paragraph."""
    if state == "not-run":
        return f"discovery not run ({reason})"
    if state == "unavailable":
        return f"launch discovery unavailable: {reason}"
    version = launch["context"]["codex_cli_version"] or "version unknown"
    return f"launch discovery ok (codex-cli {version})"


def _model_selection_lines(roles, routing_mode, state, reason):
    """The err.log lines that follow the dispatch line: one summary, then
    one line per role whose automatic choice fell back to inheritance."""
    decisions = [(role.id, _role_decision(role)) for role in roles]
    counts = collections.Counter(d.provenance for _, d in decisions)
    discovery = f"{state} ({reason})" if reason else state
    lines = [
        f"[codex-council] model selection: routing={routing_mode}; "
        f"discovery={discovery}; "
        + " ".join(f"{name}={counts[name]}" for name in PROVENANCES)
    ]
    lines += [
        f"[codex-council:{role_id}] routing fell back to native "
        f"inheritance: {decision.note}"
        for role_id, decision in decisions
        if decision.provenance == "fallback"
    ]
    return [_report_inline(line) for line in lines]


def _resolve_launch_selections(roles, run_dir, routing_mode):
    """Validate authoring, discover once if needed, and attach decisions.

    Returns (roles with `decision` set, the launch snapshot or None). The
    launch snapshot is taken only when a role carries an automatic
    selection and routing is on — councils of inherited and explicit roles
    pay no discovery latency — and is frozen for the whole council; it is
    never written over the planning snapshot.
    """
    now = _utc_iso(time.time())
    planning, planning_problem = _read_snapshot(run_dir)
    _validate_selection_authoring(
        roles, planning, planning_problem, routing_mode, now
    )
    launch = None
    if routing_mode == "auto" and any(_is_automatic(r) for r in roles):
        launch = _discover(routing_mode)
    resolved = [
        replace(role, decision=_resolve_selection(
            role, planning, launch, routing_mode, now
        ))
        for role in roles
    ]
    return resolved, launch


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
            "preflight, e.g. one created by `mktemp -d`. Without "
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
            "discovery runs. Use this after writing the per-run staging "
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
            "whenever RUNDIR is valid, even when discovery is unavailable. "
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
            "SKILL/script pair; re-run scripts/dev-link.sh (or reinstall "
            "the plugin) and restart the session."
        )
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


def _usage_exit_if_codex_missing(prefix):
    """Exit 2 with an install-method-neutral recovery if codex is absent.

    Shared by the --check-staging-dir preflight and the launch path: a
    missing binary must fail loudly BEFORE a background launch, where the
    error would otherwise surface only inside err.log. PATH is included
    because the failing Bash invocation's PATH is the diagnostic that
    matters — Claude Code Bash calls do not share environment mutations.
    """
    if shutil.which("codex"):
        return
    _usage_exit(
        f"{prefix}Codex CLI not found on PATH for this Bash invocation. "
        "Recovery: make `codex --version` work in the same environment "
        "that will run the council launch, then re-run --check-staging-dir "
        "and launch. Do not rely on PATH changes from a previous Claude "
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


def _roles_usage_exit(msg):
    """Exit 2 on a roles-file validation defect, with the uniform recovery."""
    _usage_exit(f"{msg} {ROLES_REWRITE_RECOVERY}")


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
# One grammar for model and effort values. Shape checks only: no value list
# is hardcoded — whether an automatic choice is advertised comes from this
# run's discovery snapshot, and an explicit pin reaches Codex unchanged. No
# leading "-" and no whitespace, control characters, quotes, backslashes,
# or angle brackets, so argv, the TOML string in
# -c model_reasoning_effort="<effort>", report lines, and reply-file headers
# are safe by construction. Case is preserved, never folded. \Z (not $) for
# the same trailing-newline reason as ROLE_ID_PATTERN.
SELECTION_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]*\Z")
# Model values a human might mean as "inherit", but which Codex would
# receive as a literal model id (compared case-insensitively). Inheritance
# is omission.
RESERVED_MODEL_VALUES = ("inherit", "default")
SELECTION_MODES = ("user", "routed", "native_effort")
# The runtime-grounded modes, bound to this run's --discover snapshot.
AUTOMATIC_MODES = ("routed", "native_effort")
INHERIT_HINT = "omit model, effort, and selection to inherit native configuration"


def _validate_optional_role_field(entry, field, ctx):
    """Return a validated model/effort value, or None when omitted."""
    if field not in entry:
        return None
    value = entry[field]
    if not isinstance(value, str) or not SELECTION_VALUE_PATTERN.match(value):
        _roles_usage_exit(
            f"--roles-file {ctx}: optional field {field!r} must be a "
            "non-empty string matching "
            f"{SELECTION_VALUE_PATTERN.pattern} (got {value!r}); "
            f"{INHERIT_HINT}."
        )
    if field == "model" and value.lower() in RESERVED_MODEL_VALUES:
        _roles_usage_exit(
            f"--roles-file {ctx}: model {value!r} is not an inheritance "
            f"value; {INHERIT_HINT}."
        )
    return value


def _validate_selection_reason(reason, ctx):
    """selection.reason: a non-empty, single-line string (no length cap)."""
    if not isinstance(reason, str) or not reason.strip():
        _roles_usage_exit(
            f"--roles-file {ctx}: selection.reason must be a non-empty "
            "single-line string."
        )
    if any(ch in reason for ch in LINEBREAK_CHARS):
        _roles_usage_exit(
            f"--roles-file {ctx}: selection.reason must not contain newlines."
        )


def _parse_selection(entry, model, effort, ctx, require_selection):
    """Validate a role's `selection` object; return a Selection, or None to
    inherit.

    {"mode": "user"} (optionally with "reason") is an explicit pin and
    needs model and/or effort. {"mode": "routed", "snapshot_id", "reason"}
    needs both; {"mode": "native_effort", "snapshot_id", "reason"} needs
    effort and forbids model (the runner pins the proven native model). A
    model or effort with no selection is refused on the skill path
    (require_selection, i.e. --skill-contract was passed) and read as an
    explicit user pin in direct CLI use.
    """
    if "selection" not in entry:
        if model is None and effort is None:
            return None
        if require_selection:
            _roles_usage_exit(
                f"--roles-file {ctx}: 'model'/'effort' without 'selection': "
                "declare selection.mode: 'user' for an explicit user "
                "request, 'routed' or 'native_effort' for a runtime-grounded "
                f"choice; or {INHERIT_HINT}."
            )
        return Selection("user")
    value = entry["selection"]
    if not isinstance(value, dict):
        _roles_usage_exit(
            f"--roles-file {ctx}: 'selection' must be an object with a "
            "'mode'."
        )
    mode = value.get("mode")
    if mode not in SELECTION_MODES:
        _roles_usage_exit(
            f"--roles-file {ctx}: selection.mode must be 'user', 'routed', "
            f"or 'native_effort' (got {mode!r}); to inherit, omit model, "
            "effort, and selection."
        )
    if mode == "user" and "snapshot_id" in value:
        _roles_usage_exit(
            f"--roles-file {ctx}: selection.snapshot_id is only for 'routed' "
            "and 'native_effort'; an explicit user pin is not bound to a "
            "snapshot."
        )
    if mode == "user":
        allowed, shape = {"mode", "reason"}, "'mode' and an optional 'reason'"
    else:
        allowed = {"mode", "snapshot_id", "reason"}
        shape = "'mode', 'snapshot_id', and 'reason'"
    unknown = sorted(set(value) - allowed)
    if unknown:
        _roles_usage_exit(
            f"--roles-file {ctx}: selection has unknown field(s) "
            f"{', '.join(repr(k) for k in unknown)}; selection.mode "
            f"'{mode}' takes only {shape}."
        )
    if mode == "user" and model is None and effort is None:
        _roles_usage_exit(
            f"--roles-file {ctx}: selection.mode 'user' needs 'model' and/or "
            "'effort'; to inherit, omit model, effort, and selection."
        )
    if mode == "routed" and (model is None or effort is None):
        _roles_usage_exit(
            f"--roles-file {ctx}: selection.mode 'routed' needs both 'model' "
            "and 'effort', copied from this run's discovery snapshot."
        )
    if mode == "native_effort" and (model is not None or effort is None):
        _roles_usage_exit(
            f"--roles-file {ctx}: selection.mode 'native_effort' takes "
            "'effort' and no 'model' (the runner pins the proven native "
            "model at launch)."
        )
    snapshot_id = value.get("snapshot_id")
    if mode in AUTOMATIC_MODES and not (
        isinstance(snapshot_id, str) and _SNAPSHOT_ID_RE.fullmatch(snapshot_id)
    ):
        _roles_usage_exit(
            f"--roles-file {ctx}: selection.mode '{mode}' needs "
            "'snapshot_id', the snapshot_id --discover printed for this run "
            f"(got {snapshot_id!r})."
        )
    reason = value.get("reason")
    if mode in AUTOMATIC_MODES or "reason" in value:
        _validate_selection_reason(reason, ctx)
    return Selection(mode, snapshot_id, reason)


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
    the selection object is well-formed for its mode (see _parse_selection;
    require_selection is the skill path), instructions follow the
    documented contract, and ids are unique within the JSON. A key repeated
    at any object level is rejected too: it would hide one value behind
    another. Unknown keys are rejected, not ignored: stray filler fields
    like '"_": ""' are the signature of a corrupted LLM write, so surfacing
    them forces a clean rewrite instead of silently launching from a file
    that already glitched once. Every validation defect carries the same
    full-rewrite recovery (ROLES_REWRITE_RECOVERY). Whether an automatic
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
        model = _validate_optional_role_field(entry, "model", ctx)
        effort = _validate_optional_role_field(entry, "effort", ctx)
        selection = _parse_selection(
            entry, model, effort, ctx, require_selection
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


def _read_context_file(path):
    """Read a staged context file; content defects are usage errors (exit 2).

    Empty and non-UTF-8 staged context files exit 2 like every other staging
    defect, so 'exit 2 = fix the staged inputs and re-run preflight' holds
    uniformly; exit 1 stays for runtime failures (stdin defects, every role
    failing). There is no plugin-imposed context size ceiling.
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
        _usage_exit(
            f"--context-file: Context file {path!r} is not valid UTF-8 "
            f"({detail}). Recovery: rewrite context.md as UTF-8 text, then "
            "re-run --check-staging-dir."
        )
    _usage_exit(
        f"--context-file: Context file {path!r} is empty or "
        "whitespace-only. Recovery: rewrite context.md with the "
        "decision-complete working context or a self-contained question, "
        "then re-run "
        "--check-staging-dir."
    )


def _check_private_dir(path, prefix="--check-staging-dir: ",
                       recovery=STAGING_DIR_RECOVERY):
    """Usage-error unless path is a user-owned, non-symlink, private dir.

    Returns the normalized path the checks were performed on; callers
    must use it for any subsequent joins so the validated path and the
    used path cannot diverge.

    lstat (not stat) so a symlink final component is rejected instead of
    silently followed, and ownership is checked so a foreign-owned dir
    that happens to be mode 0700 does not pass. Every rejection carries
    action-first recovery text: the caller is an LLM, and for a staging
    rejection the one correct move is always a NEW `mktemp -d` directory —
    never chmod, mkdir, or reuse of the rejected path (for --follow it is
    pointing at the real run directory). `prefix` names the actual
    entrypoint (a direct-launch rejection must not claim it came from
    --check-staging-dir) and `recovery` is mode-specific.
    """
    # normpath strips trailing slashes first: lstat("link/") follows the
    # final symlink (the slash demands a directory target), so an
    # un-normalized path would let a symlink pass the check below.
    path = os.path.normpath(path)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        _usage_exit(
            f"{prefix}{path!r} does not exist. "
            f"{recovery}"
        )
    except OSError as e:
        _usage_exit(
            f"{prefix}cannot inspect {path!r} "
            f"({e.strerror or e}). {recovery}"
        )
    if stat.S_ISLNK(st.st_mode):
        _usage_exit(
            f"{prefix}{path!r} is a symlink, not the directory "
            f"printed by `mktemp -d`. {recovery}"
        )
    if not stat.S_ISDIR(st.st_mode):
        _usage_exit(
            f"{prefix}{path!r} is not a directory. "
            f"{recovery}"
        )
    if st.st_uid != os.geteuid():
        _usage_exit(
            f"{prefix}{path!r} is owned by uid {st.st_uid}, not "
            f"the invoking user (uid {os.geteuid()}). {recovery}"
        )
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        _usage_exit(
            f"{prefix}{path!r} is mode {mode:04o}, not private "
            f"0700 — not the private mode `mktemp -d` produces. Files "
            f"already written here may have been readable by other local "
            f"users. {recovery}"
        )
    return path


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

    Prints the staging-OK line, then one selection-plan line per role. No
    discovery runs here: automatic selections are validated against this
    run's planning snapshot (DIR/model-snapshot.json) with the same resolver
    the launch uses, and are revalidated by a fresh discovery at launch.
    require_selection is the skill path (--skill-contract was passed).
    """
    if path == "":
        _usage_exit("--check-staging-dir must be non-empty.")
    path = _check_private_dir(path)
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
    routing_mode = _model_routing_mode()
    now = _utc_iso(time.time())
    planning, planning_problem = _read_snapshot(path)
    _validate_selection_authoring(
        roles, planning, planning_problem, routing_mode, now
    )
    print(
        f"[codex-council] staging OK: {os.path.abspath(path)} "
        f"({len(roles)} roles; max parallel {max_parallel}) "
        f"version={_plugin_version()}"
    )
    for role in roles:
        decision = _resolve_selection(role, planning, None, routing_mode, now)
        print(_report_inline(
            f"[codex-council] selection plan: {role.id}: "
            f"{_selection_plan_text(decision)}"
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
    try:
        print(line, flush=True)
    except (OSError, ValueError):
        with contextlib.suppress(Exception):
            sys.stdout.close()
        raise SystemExit(1)


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
    marker = line.rfind(" reply=")
    if marker < 0:
        return True
    path = line[marker + len(" reply="):]
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
        _discover_command(args.discover)
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

    # Parse and validate staged inputs before requiring Codex. This catches
    # temp-path mismatches without launching or depending on any Codex state.
    if args.roles_file is not None:
        custom_roles = _parse_roles_json(
            _read_roles_file(args.roles_file), require_selection
        )
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

    _usage_exit_if_codex_missing("")
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
        roles, launch = _resolve_launch_selections(roles, run_dir, routing_mode)
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
