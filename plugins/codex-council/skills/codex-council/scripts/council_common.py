"""Shared primitives for the codex-council runner (see codex_council.py).

Nothing here imports a sibling module; every sibling imports from here:
single-line escaping for report and progress text (_report_inline over
LINEBREAK_CHARS), the advisory stderr sink (_diag), usage exits with the
uniform recovery texts, the private-directory gate, atomic 0600 writes,
strict JSON loading, JSONL record iteration, the project root, and the
plugin version. Its module-level state (the diagnostics sink and the cached
project root) exists only here.
"""

import contextlib
import errno
import json
import os
import stat
import subprocess
import sys
from functools import cache
from pathlib import Path

_READ_CHUNK_BYTES = 65536
# Every line-boundary character str.splitlines() recognizes (beyond the plain
# space): CR, LF, VT, FF, FS, GS, RS, NEL, LS, PS. Labels reject the full set
# and _report_inline escapes the same set, so the two contracts agree.
LINEBREAK_CHARS = (
    "\r", "\n", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e",
    "\x85", "\u2028", "\u2029",
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


# ---------- single-line report text ----------

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


# ---------- usage exits and the private-directory gate ----------

def _usage_exit(msg):
    """Exit 2 with msg on stderr (argparse-compatible usage-error code)."""
    print(msg, file=sys.stderr)
    raise SystemExit(2)


def _roles_usage_exit(msg):
    """Exit 2 on a roles-file validation defect, with the uniform recovery."""
    _usage_exit(f"{msg} {ROLES_REWRITE_RECOVERY}")


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


# ---------- atomic private writes, strict JSON, and JSONL records ----------

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


def _dedupe_preserve_order(items):
    seen = set()
    out = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out
