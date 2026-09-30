"""Shared primitives for the codex-council runner (see codex_council.py).

Nothing here imports a sibling module; every sibling imports from here:
single-line, control-free escaping for report and progress text
(_report_inline, and _log_inline for err.log diagnostics), the advisory
stderr sink (_diag), stdout output that ends quietly when nobody reads it
any more (_print_stdout), usage exits with the uniform recovery texts, the
one private-path policy (_private_stat_problem) and the private-directory
gate built on it, the one-launch-per-directory gate, atomic 0600 writes,
strict JSON loading, JSONL record iteration, the UTC timestamp format (_utc_iso), the project root (a
bounded, cached git lookup), and the plugin version. Its module-level state
(the diagnostics sink and the cached project root) exists only here.
"""

import contextlib
import contextvars
import errno
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

_READ_CHUNK_BYTES = 65536
# The most `git rev-parse --show-toplevel` may take (see _project_root); a
# timeout falls back to the working directory, as any git failure does.
PROJECT_ROOT_TIMEOUT_SECS = 5
# Every line-boundary character str.splitlines() recognizes (beyond the plain
# space): CR, LF, VT, FF, FS, GS, RS, NEL, LS, PS. Labels reject the full set
# and _report_inline escapes the same set, so the two contracts agree.
LINEBREAK_CHARS = (
    "\r", "\n", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e",
    "\x85", "\u2028", "\u2029",
)
# What a new run directory needs before its pre-flight: its own discovery
# snapshot, and a roles.json whose automatic selections name that snapshot
# (the old snapshot_id identifies the abandoned directory's snapshot, so the
# pre-flight would refuse it). Every new-directory recovery below ends with
# this sequence.
NEW_DIR_SNAPSHOT_CLAUSE = (
    "with the new snapshot_id in every routed or native_effort selection"
)
# Action-first recovery text for a rejected staging DIRECTORY. The orchestrator
# is an LLM; the cheapest literal reading of "create it with mktemp -d" is
# satisfiable by mkdir/chmod on the same predictable path, so the recovery must
# forbid exactly those moves and demand a NEW path, then give the complete
# sequence there: discovery, both files, the pre-flight.
STAGING_DIR_RECOVERY = (
    "Recovery: abandon this directory — do not chmod it, do not mkdir it, "
    "and do not reuse its name. Run `mktemp -d` again, copy the NEW printed "
    "absolute path, run --discover in that new directory, re-Write BOTH "
    f"roles.json and context.md there ({NEW_DIR_SNAPSHOT_CLAUSE}), and "
    "re-run --check-staging-dir on it."
)
# A run directory holds exactly one launch. A launch leaves these behind: its
# command's own stdout and stderr redirections and the runner's reply
# directory, so any of them means the directory has already launched.
REPLIES_SUBDIR = "replies"
LAUNCH_OUTPUTS = ("out.md", "err.log", REPLIES_SUBDIR)
# A detached launch (--start) claims its directory with these two files
# before anything else: the lifetime lock the supervisor holds, and the
# record the supervisor writes about itself. Either one also means the
# directory has launched, and neither is ever removed or replaced.
SUPERVISOR_LOCK_FILENAME = "supervisor.lock"
SUPERVISOR_FILENAME = "supervisor.json"
SUPERVISOR_FILES = (SUPERVISOR_LOCK_FILENAME, SUPERVISOR_FILENAME)
# Recovery for a directory that already launched. The action is always a NEW
# directory: relaunching here truncates a running council's out.md and
# err.log, or replaces a finished council's report and mixes two runs in
# replies/.
LAUNCHED_DIR_RECOVERY = (
    "Recovery: every launch needs its own directory, so leave this one and "
    "its files as they are. Run `mktemp -d` again, run --discover in the "
    "NEW directory, Write roles.json and context.md there "
    f"({NEW_DIR_SNAPSHOT_CLAUSE}), and run --check-staging-dir on it."
)
# Uniform recovery appended to EVERY roles-file validation failure. The only
# production writer of roles.json is an LLM; partial patches of a file that
# already glitched once are the corruption vector, so every defect demands one
# complete rewrite. Parse-time code cannot know its caller, so this default
# names both the pre-flight and a direct command; a staged launch scopes in
# STAGED_LAUNCH_ROLES_RECOVERY instead (see _roles_recovery).
ROLES_REWRITE_RECOVERY = (
    "Recovery: rewrite the entire file passed to --roles-file in one "
    "complete Write operation; do not patch, append, or replace a "
    "substring. Do not launch until the rewritten file validates, then "
    "re-run the pre-flight or the direct command you used."
)
# The next step for a staged launch (--roles-file with --context-file) that
# refuses before dispatch, in place of a pre-flight re-run. The launch
# command's own shell redirections created out.md and err.log before the
# runner started, so the directory already holds this launch and
# --discover and the pre-flight refuse it (_usage_exit_if_launched): the
# fix always goes into a NEW directory. A lowercase clause, so each
# launch-side recovery can lead with it or follow its own fix with it.
STAGED_LAUNCH_RESTART = (
    "start over in a new directory: this launch's own redirections already "
    "created out.md and err.log in this one, so the pre-flight refuses it "
    "now. Leave it and its files as they are, run `mktemp -d` again, run "
    "--discover in the NEW directory, Write roles.json and context.md "
    f"there ({NEW_DIR_SNAPSHOT_CLAUSE}), and run --check-staging-dir on it."
)
# ROLES_REWRITE_RECOVERY's staged-launch form: the same whole-file rule,
# applied to the roles.json written in the new directory.
STAGED_LAUNCH_ROLES_RECOVERY = (
    f"Recovery: {STAGED_LAUNCH_RESTART} Write the new roles.json whole, in "
    "one complete Write operation; do not patch, append, or replace a "
    "substring."
)
# The recovery _roles_usage_exit appends; _roles_recovery scopes an override.
_roles_recovery_text = contextvars.ContextVar(
    "roles_recovery_text", default=ROLES_REWRITE_RECOVERY
)


# ---------- advisory diagnostics ----------

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


def _print_stdout(text):
    """Print text to stdout and flush; exit 1 quietly if stdout is dead.

    A dead stdout means nobody is reading any more (a follower's Monitor,
    the caller of --discover): stop with exit 1 instead of a traceback, and
    close stdout so an interpreter-shutdown flush of the broken stream
    cannot rewrite the exit code (never exit 120).
    """
    try:
        print(text, flush=True)
    except (OSError, ValueError):
        with contextlib.suppress(Exception):
            sys.stdout.close()
        raise SystemExit(1)


# ---------- plugin version and project root ----------

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


# The project root once _project_root has established it (key "root").
_project_root_cache = {}


def _project_root(deadline=None):
    """Return the git repo root for the current dir, falling back to cwd.

    `git rev-parse --show-toplevel` gets at most PROJECT_ROOT_TIMEOUT_SECS;
    a timeout falls back to the cwd exactly as a git failure does. The
    answer is cached (_project_key asks once per role), so git runs at most
    once per invocation. `deadline` (a time.monotonic() value; discovery
    passes its own) also caps git at the time left before it: when that
    budget, not git's own cap, runs out first, nothing is cached and None
    is returned, so discovery reports inconclusive evidence instead of a
    root it never established.
    """
    if "root" in _project_root_cache:
        return _project_root_cache["root"]
    timeout, budget_bound = PROJECT_ROOT_TIMEOUT_SECS, False
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        if remaining < timeout:
            timeout, budget_bound = remaining, True
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=timeout,
        ).stdout.strip()
    except subprocess.TimeoutExpired:
        if budget_bound:
            return None
        root = ""
    except OSError:
        root = ""
    _project_root_cache["root"] = root or os.getcwd()
    return _project_root_cache["root"]


# ---------- single-line report text ----------

# Spellings for the FULL str.splitlines() boundary set beyond the plain
# space (\r \n \x0b \x0c \x1c \x1d \x1e U+0085 U+2028 U+2029, matching
# LINEBREAK_CHARS) and for tab. Every other non-printable character gets
# the \xNN, \uNNNN, or \UNNNNNNNN form (see _escape_char).
_REPORT_INLINE_ESCAPES = {
    "\t": "\\t",
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
}
# A diagnostic line that is not a completion line must not carry this
# marker: the follower drops a line whose last " reply=" names a path
# outside the run's replies directory (council_liveness._reply_path_ok),
# so foreign text holding it would hide the whole line from the Monitor.
REPLY_MARKER = " reply="
_REPLY_MARKER_ESCAPED = " reply\\x3d"


def _escape_char(ch):
    escaped = _REPORT_INLINE_ESCAPES.get(ch)
    if escaped is not None:
        return escaped
    code = ord(ch)
    if code < 0x100:
        return f"\\x{code:02x}"
    if code < 0x10000:
        return f"\\u{code:04x}"
    return f"\\U{code:08x}"


def _report_inline(value):
    """Keep report metadata on one line and free of terminal controls.

    Escapes every character str.isprintable() rejects: the full
    str.splitlines() boundary set (\\r \\n \\x0b \\x0c \\x1c \\x1d \\x1e
    U+0085 U+2028 U+2029), tab, the other C0 and C1 controls, DEL, format
    characters such as bidirectional overrides, separators other than the
    plain space, and lone surrogates. Catalog, configuration, and Codex
    text is untrusted, and an ESC or OSC sequence in it must not reach a
    terminal tailing err.log or --discover output.
    """
    text = str(value)
    if text.isprintable():
        return text
    return "".join(ch if ch.isprintable() else _escape_char(ch) for ch in text)


def _log_inline(value):
    """_report_inline for an err.log line that is not a completion line.

    Also escapes REPLY_MARKER, so foreign text inside a diagnostic (a
    fallback reason, a Codex warning) cannot make the follower drop it.
    """
    return _report_inline(value).replace(REPLY_MARKER, _REPLY_MARKER_ESCAPED)


# ---------- usage exits and the private-directory gate ----------

def _usage_exit(msg):
    """Exit 2 with msg on stderr (argparse-compatible usage-error code)."""
    print(msg, file=sys.stderr)
    raise SystemExit(2)


def _roles_usage_exit(msg):
    """Exit 2 on a roles-file validation defect, with the uniform recovery.

    The recovery is ROLES_REWRITE_RECOVERY unless a caller scoped another
    with _roles_recovery.
    """
    _usage_exit(f"{msg} {_roles_recovery_text.get()}")


@contextlib.contextmanager
def _roles_recovery(text):
    """Make `text` the recovery every roles-file defect carries in this block.

    The defects are raised deep in parsing and authoring validation, which
    cannot know their caller; the staged launch wraps its roles checks in
    this with STAGED_LAUNCH_ROLES_RECOVERY, because its directory already
    holds this launch and a pre-flight re-run there is refused.
    """
    token = _roles_recovery_text.set(text)
    try:
        yield
    finally:
        _roles_recovery_text.reset(token)


def _private_stat_problem(st, *, directory):
    """Why an lstat/fstat result is not private to this user, or None.

    The one private-path policy, shared by the staging and follow
    directory gate, the replies directory, and the model snapshot: not a
    symlink, a directory (or a regular file when `directory` is false),
    owned by the effective uid, and no group or other permission bits.
    Returns (kind, fragment): kind is "symlink", "type", "owner", or
    "mode", and fragment is the shared sentence fragment ("is a symlink",
    "is not a directory", "is owned by uid N", "is mode 0NNN"); each caller
    adds its own ending and failure handling.
    """
    if stat.S_ISLNK(st.st_mode):
        return "symlink", "is a symlink"
    if directory and not stat.S_ISDIR(st.st_mode):
        return "type", "is not a directory"
    if not directory and not stat.S_ISREG(st.st_mode):
        return "type", "is not a regular file"
    if st.st_uid != os.geteuid():
        return "owner", f"is owned by uid {st.st_uid}"
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        return "mode", f"is mode {mode:04o}"
    return None


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
    problem = _private_stat_problem(st, directory=True)
    if problem is not None:
        kind, fragment = problem
        ending = {
            "symlink": ", not the directory printed by `mktemp -d`.",
            "type": ".",
            "owner": f", not the invoking user (uid {os.geteuid()}).",
            "mode": (", not private 0700 — not the private mode `mktemp -d` "
                     "produces. Files already written here may have been "
                     "readable by other local users."),
        }[kind]
        _usage_exit(f"{prefix}{path!r} {fragment}{ending} {recovery}")
    return path


def _usage_exit_if_launched(run_dir, prefix):
    """Usage-error when run_dir already holds a council launch.

    Called by --discover, the pre-flight, and --start after the
    private-directory gate. Any of LAUNCH_OUTPUTS or SUPERVISOR_FILES
    counts (lexists, so a dangling symlink does too). The tracked launch
    command's shell redirections truncate out.md and err.log before the
    runner starts, so only a step that runs before that command can stop a
    relaunch into a directory whose council may still be running; the
    tracked launch itself never checks. For the same reason, a staged
    launch that refuses before dispatch never asks for a pre-flight re-run
    in its own directory (STAGED_LAUNCH_RESTART). --start also claims the
    directory atomically after this check (O_EXCL), so two concurrent
    starts cannot both pass.
    """
    present = [name for name in (*LAUNCH_OUTPUTS, *SUPERVISOR_FILES)
               if os.path.lexists(os.path.join(run_dir, name))]
    if present:
        _usage_exit(
            f"{prefix}{run_dir!r} already holds a council launch "
            f"({', '.join(present)} present): its council may still be "
            "running, and relaunching here would truncate its out.md and "
            f"err.log. {LAUNCHED_DIR_RECOVERY}"
        )


# ---------- atomic private writes, strict JSON, and JSONL records ----------

def _atomic_write_private(path, data):
    """Atomically replace `path` with the bytes `data` as a 0600 file.

    Temp file in the same directory (O_CREAT|O_EXCL|O_NOFOLLOW, mode 0600),
    full write, fsync, then os.replace — a reader never sees a partial file.
    Any failure removes the temp file and propagates; each caller owns its
    failure policy (reply files are advisory, a snapshot must not go stale).
    """
    directory = os.path.dirname(path)
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
             | os.O_CLOEXEC)
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


def _iter_json_objects(jsonl_output):
    """Yield JSON object lines from a JSONL stream, skipping malformed lines.

    Split strictly on "\\n" (JSONL's record separator), never str.splitlines():
    splitlines also breaks on U+2028, U+2029, and U+0085, which are legal
    *unescaped* inside a JSON string. codex/serde_json can emit an agent_message
    containing one of those literally, and splitting there would tear the record
    into two invalid fragments — silently dropping a completed reply and turning
    a successful role into a failure. A line nested too deeply or holding an
    out-of-range number is skipped like any other malformed line.
    """
    for line in jsonl_output.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(event, dict):
            yield event


def _dedupe_preserve_order(items):
    seen = set()
    out = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _utc_iso(seconds):
    """Unix seconds as "YYYY-MM-DDTHH:MM:SSZ", or None when out of range.

    The one UTC timestamp format: snapshot creation, advertised
    retirements, the selection clock, and continuity state.
    """
    try:
        parts = time.gmtime(seconds)
    except (OverflowError, OSError, ValueError):
        return None
    if not 1 <= parts.tm_year <= 9999:
        return None
    return "%04d-%02d-%02dT%02d:%02d:%02dZ" % tuple(parts[:6])
