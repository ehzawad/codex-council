"""Run liveness for the codex-council runner (see codex_council.py).

The runner publishes RUNDIR/status.json through RunStatus: its own pid and
OS start identity, its state, a tick that advances on every role transition
and at least every STATUS_TICK_SECS, and per role the scheduling state, the
attempt, the live codex process group, and the outcome. Three read-only
commands use that file:

* --follow RUNDIR relays the actionable lines of RUNDIR/err.log (one stdout
  line per event, for a Claude Code Monitor) and reports a runner that is
  gone or has stopped ticking;
* --status RUNDIR prints a short snapshot and one next action;
* --reap RUNDIR, only once the runner is gone, terminates the recorded codex
  process groups whose leader is still this run's codex.

A process identity is its pid plus its start time as `ps -o lstart=` prints
it (C locale, UTC): the same pid with another start time is another
process. Reports state facts (present, gone, tick age, quiet seconds),
never health. Nothing here writes anything but status.json, and only the
runner writes that.
"""

import contextlib
import dataclasses
import json
import math
import os
import re
import select
import signal
import stat
import subprocess
import sys
import time
from typing import Optional

from council_common import (
    _READ_CHUNK_BYTES,
    REPLIES_SUBDIR,
    REPLY_MARKER,
    _atomic_write_private,
    _check_private_dir,
    _diag,
    _log_inline,
    _print_stdout,
    _report_inline,
    _usage_exit,
)

STATUS_FILENAME = "status.json"
STATUS_SCHEMA = 1
# The runner republishes status.json at least this often while it runs.
STATUS_TICK_SECS = 15
ROLE_STATES = ("queued", "active", "retry-wait", "settled")
TERMINAL_STATES = ("done", "interrupted", "aborted")
# Every ps call is bounded; a timeout reads as "cannot tell", never "gone".
PS_TIMEOUT_SECS = 2
# ps prints lstart in local time and the locale's words; pin both so the
# runner and a later reader always print the same start identity.
_PS_ENV = {"LC_ALL": "C", "TZ": "UTC0"}

# --follow timing: read err.log every FOLLOW_POLL_SECS, look at the runner
# and the follower's own parent every FOLLOW_CHECK_SECS; no dispatch line
# within FOLLOW_START_SECS means no council activity; a dispatched run
# without status.json after FOLLOW_STATUS_WAIT_SECS is followed on err.log
# alone. A tick older than TICK_WARN_SECS while the runner is present is
# reported once; at TICK_GIVE_UP_SECS the follower stops (exit 4).
FOLLOW_POLL_SECS = 0.5
FOLLOW_CHECK_SECS = 2
FOLLOW_START_SECS = 120
FOLLOW_STATUS_WAIT_SECS = 30
TICK_WARN_SECS = 120
TICK_GIVE_UP_SECS = 300
# A wall-clock jump this much larger than the monotonic advance between two
# follower polls is a system suspend: tick age then counts from the resume.
FOLLOW_SUSPEND_SLACK_SECS = 60
# Follower exit codes: 0 = a terminal line (or terminal status) was seen,
# 1 = stdout is gone, 2 = usage error, 3 = no council activity, 4 = the
# runner is gone or stopped ticking, 5 = the follower's own parent is gone.
FOLLOW_EXIT_STDOUT_GONE = 1
FOLLOW_EXIT_NO_ACTIVITY = 3
FOLLOW_EXIT_RUNNER_GONE = 4
FOLLOW_EXIT_PARENT_GONE = 5
# --reap: SIGTERM, up to this long for the group to empty, then SIGKILL.
REAP_GRACE_SECS = 2
# --status shows at most this many unfinished roles (about ten lines total).
STATUS_ROLE_LINES = 5

FOLLOW_LINE_PREFIX = "[codex-council"
FOLLOW_NOTE = "[codex-council-follow]"
FOLLOW_DISPATCH_PREFIX = "[codex-council] dispatching "
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
# Routine lines err.log keeps for humans and the default follower does not
# relay: per-attempt starts and the periodic heartbeat (--verbose relays them).
FOLLOW_ROUTINE_PATTERN = re.compile(
    r"^\[codex-council\] (?:[a-z0-9_-]+: started \((?:fresh|resume)\) "
    r"attempt=\d+/\d+ watchdog=\S+|still running after \d+s: .*)\Z"
)
# Recovery for a rejected run directory. These commands only read, so the
# fix is always "point at the real run directory", never chmod or mkdir.
RUN_DIR_RECOVERY = (
    "Recovery: pass the exact absolute mktemp directory the council was "
    "launched from (the directory holding roles.json, out.md, and err.log). "
    "--follow, --status, and --reap only read DIR/err.log and "
    "DIR/status.json; do not chmod or mkdir anything for them."
)


# ---------- process identity ----------

def _process_table(pid=None):
    """{pid: (pgid, stat, start identity)} from one bounded ps call.

    All processes, or only `pid`. None when ps cannot tell (missing, timed
    out, or reporting an error); an empty table means no such process.
    """
    args = ["-o", "pid=", "-o", "pgid=", "-o", "stat=", "-o", "lstart="]
    args += ["-p", str(pid)] if pid is not None else ["-A"]
    try:
        done = subprocess.run(
            ["ps", *args], capture_output=True, text=True,
            timeout=PS_TIMEOUT_SECS, env={**os.environ, **_PS_ENV},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0 and (done.stdout.strip() or done.stderr.strip()):
        return None
    table = {}
    for line in done.stdout.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        try:
            table[int(fields[0])] = (int(fields[1]), fields[2],
                                     " ".join(fields[3:]))
        except ValueError:
            continue
    return table


def process_start_identity(pid):
    """pid's start identity (normalized `ps -o lstart=`), or None."""
    entry = (_process_table(pid) or {}).get(pid)
    return entry[2] if entry else None


def _identity_state(table, pid, identity):
    """"alive", "gone", or "unknown" for a recorded (pid, identity).

    Gone: no such process, a zombie, or a process that started at another
    time than the one recorded (the pid was reused).
    """
    if table is None or pid is None:
        return "unknown"
    entry = table.get(pid)
    if entry is None or entry[1].startswith("Z"):
        return "gone"
    if identity is not None and entry[2] != identity:
        return "gone"
    return "alive"


def runner_state(pid, identity):
    """The recorded runner's state from one bounded single-pid ps call."""
    return _identity_state(_process_table(pid), pid, identity)


def _codex_groups(view, table):
    """(verified, unverified) live process groups of unfinished roles.

    Each item is (role id, pgid, member pids). A group is verified when its
    leader (the codex process the runner spawned, pid == pgid) is alive with
    the start identity the runner recorded; anything else (leader gone or
    replaced, no recorded identity, or a group this process belongs to) is
    unverified and never signalled.
    """
    verified, unverified = [], []
    if table is None:
        return verified, unverified
    own_group = os.getpgrp()
    for role_id, role in view.roles.items():
        pgid = role["pgid"]
        if role["state"] == "settled" or pgid is None:
            continue
        members = sorted(pid for pid, (group, state, _) in table.items()
                         if group == pgid and not state.startswith("Z"))
        if not members:
            continue
        leader = table.get(pgid)
        ours = (
            pgid != own_group
            and role["pid"] == pgid
            and role["start_identity"] is not None
            and leader is not None
            and leader[2] == role["start_identity"]
        )
        (verified if ours else unverified).append((role_id, pgid, members))
    return verified, unverified


def _terminate_group(pgid):
    """SIGTERM a process group, wait up to REAP_GRACE_SECS, then SIGKILL."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + REAP_GRACE_SECS
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except OSError:
            return
        time.sleep(0.05)
    with contextlib.suppress(OSError):
        os.killpg(pgid, signal.SIGKILL)


# ---------- status.json: the runner's writer ----------

class RunStatus:
    """The runner's live state, published as RUNDIR/status.json.

    Roles and their last-output stamps live in memory for the heartbeat;
    once attach() names a file, every transition and every tick of the
    event loop republishes it atomically (0600). Each publish is a tick:
    tick.seq grows and tick.at is its wall time, so a fresh tick shows the
    runner's event loop is turning. A failed write is reported once and
    never affects the council.
    """

    def __init__(self):
        self.path = None
        self.run_id = None
        self.runner = {}
        self.roles = {}
        self.seq = 0
        self._write_failed = False

    def attach(self, path):
        """Publish to `path` from now on (the launch path only)."""
        pid = os.getpid()
        self.path = path
        self.run_id = os.urandom(8).hex()
        self.runner = {"pid": pid, "start_identity": process_start_identity(pid),
                       "state": "running"}

    def begin(self, role_ids):
        self.roles = {rid: {"state": "queued", "attempt": 0} for rid in role_ids}
        self.publish()

    def update(self, role_id, **fields):
        role = self.roles.get(role_id)
        if role is not None:
            role.update(fields)
            self.publish()

    def spawned(self, role_id, pid, pgid):
        """A codex process for this role started in its own group."""
        if role_id in self.roles:
            identity = process_start_identity(pid) if self.path else None
            self.update(role_id, pid=pid, pgid=pgid, start_identity=identity,
                        output=time.monotonic())

    def exited(self, role_id):
        """The role's codex process group is gone (exited and swept)."""
        self.update(role_id, pid=None, pgid=None, start_identity=None)

    def output(self, role_id, stamp):
        """Record a role's last output byte (published with the next tick)."""
        role = self.roles.get(role_id)
        if role is not None:
            role["output"] = stamp

    def describe(self, role_id, now):
        """One heartbeat fragment for an active role."""
        role = self.roles.get(role_id, {})
        if role.get("state") == "retry-wait":
            return f"{role_id} retry-wait"
        if role.get("output") is not None:
            return f"{role_id} quiet={max(0.0, now - role['output']):.0f}s"
        return role_id

    def finish(self, state, exit_code):
        """Publish the runner's terminal state (done, interrupted, aborted)."""
        if self.path is not None:
            self.runner.update(state=state, exit=exit_code)
            self.publish()

    def snapshot(self):
        now_wall, now_mono = time.time(), time.monotonic()
        roles = {}
        for role_id, role in self.roles.items():
            entry = {key: role[key]
                     for key in ("state", "attempt", "pid", "pgid",
                                 "start_identity", "outcome")
                     if role.get(key) is not None}
            if role.get("output") is not None:
                entry["output_at"] = round(
                    now_wall - (now_mono - role["output"]), 3)
            roles[role_id] = entry
        return {
            "schema": STATUS_SCHEMA,
            "run_id": self.run_id,
            "runner": {k: v for k, v in self.runner.items() if v is not None},
            "tick": {"seq": self.seq, "at": round(now_wall, 3)},
            "roles": roles,
        }

    def publish(self):
        if self.path is None:
            return
        self.seq += 1
        try:
            _atomic_write_private(
                self.path, json.dumps(self.snapshot()).encode("utf-8"))
        except OSError as e:
            if not self._write_failed:
                self._write_failed = True
                _diag(
                    f"[codex-council] {STATUS_FILENAME} not written "
                    f"({_log_inline(e)}); --follow and --status cannot see "
                    "runner liveness for this run"
                )


# ---------- status.json: the tolerant reader ----------

@dataclasses.dataclass
class RunView:
    """The status.json fields the commands below use."""
    pid: Optional[int]
    identity: Optional[str]
    state: str
    exit: Optional[int]
    tick_at: Optional[float]
    roles: dict


def _int(value, minimum):
    if isinstance(value, int) and not isinstance(value, bool) \
            and value >= minimum:
        return value
    return None


def _number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) \
            and math.isfinite(value):
        return float(value)
    return None


def _text(value):
    return value if isinstance(value, str) and value else None


def read_status(path):
    """status.json as a RunView, or None when there is no usable file.

    Unknown fields are ignored and a consumed field of the wrong type reads
    as unknown (None), so an unexpected file degrades to fewer facts,
    never to a wrong claim.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                     | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                return None
            data = json.loads(f.read())
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    runner = data.get("runner") if isinstance(data.get("runner"), dict) else {}
    tick = data.get("tick") if isinstance(data.get("tick"), dict) else {}
    state = runner.get("state")
    roles = {}
    raw_roles = data.get("roles")
    for role_id, raw in (raw_roles.items() if isinstance(raw_roles, dict)
                         else ()):
        if not isinstance(raw, dict):
            continue
        roles[role_id] = {
            "state": raw.get("state") if raw.get("state") in ROLE_STATES
            else "unknown",
            "attempt": _int(raw.get("attempt"), 0),
            "pid": _int(raw.get("pid"), 2),
            "pgid": _int(raw.get("pgid"), 2),
            "start_identity": _text(raw.get("start_identity")),
            "output_at": _number(raw.get("output_at")),
        }
    return RunView(
        pid=_int(runner.get("pid"), 2),
        identity=_text(runner.get("start_identity")),
        state=state if state in ("running", *TERMINAL_STATES) else "unknown",
        exit=_int(runner.get("exit"), 0),
        tick_at=_number(tick.get("at")),
        roles=roles,
    )


def _unfinished(view):
    return [rid for rid, role in view.roles.items()
            if role["state"] != "settled"]


def _id_list(items, limit=8):
    shown = [_report_inline(item) for item in items[:limit]]
    if len(items) > limit:
        shown.append(f"+{len(items) - limit} more")
    return ", ".join(shown) or "none"


# ---------- --follow ----------

def _open_log(log_path):
    """Open err.log read-only if it exists; None while it does not.

    O_NONBLOCK so a FIFO planted at the path cannot hang the open, and
    O_NOFOLLOW so a symlink is refused; anything but a regular file is a
    usage error (the run directory is not a council run directory).
    """
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(log_path, flags)
    except FileNotFoundError:
        return None
    except OSError as e:
        _usage_exit(
            f"--follow: cannot open {log_path!r} ({e.strerror or e}). "
            f"{RUN_DIR_RECOVERY}"
        )
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        _usage_exit(
            f"--follow: {log_path!r} is not a regular file. {RUN_DIR_RECOVERY}"
        )
    return fd


def _reply_path_ok(line, replies_dir):
    """True unless the line names a reply= path outside replies_dir.

    Lexical only: the runner always prints abspath(RUNDIR)/replies/<key>.md,
    so any other shape was not written by the runner. This keeps a forged
    line from pointing Claude at an arbitrary file; it cannot authenticate
    a same-uid writer (see follow).
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


def _stdout_hung_up():
    """True when stdout is a pipe whose reader has gone away."""
    try:
        poller = select.poll()
        poller.register(sys.stdout.fileno(), select.POLLHUP)
        events = poller.poll(0)
    except (OSError, ValueError, AttributeError):
        return False
    return any(mask & (select.POLLHUP | select.POLLERR) for _, mask in events)


class _Follower:
    """State for one --follow run; see follow()."""

    def __init__(self, run_dir, verbose):
        self.log_path = os.path.join(run_dir, "err.log")
        self.status_path = os.path.join(run_dir, STATUS_FILENAME)
        # Same construction as the runner's replies directory.
        self.replies_dir = os.path.join(
            os.path.normpath(os.path.abspath(run_dir)), REPLIES_SUBDIR)
        self.verbose = verbose
        self.parent = os.getppid()
        self.started = time.monotonic()
        self.fd = None
        self.pending = b""
        self.dispatched_at = None
        self.traceback_noted = False
        self.status_noted = False
        self.stale = False
        self.suspend_floor = 0.0
        self.last_wall, self.last_mono = time.time(), time.monotonic()

    def note(self, text):
        _print_stdout(f"{FOLLOW_NOTE} {text}")

    def relay(self):
        """Relay new complete err.log lines; 0 after a terminal line."""
        if self.fd is None:
            self.fd = _open_log(self.log_path)
            if self.fd is None:
                return None
        while True:
            data = os.read(self.fd, _READ_CHUNK_BYTES)
            if not data:
                return None
            # Only complete lines: a read can land mid-write.
            lines = (self.pending + data).split(b"\n")
            self.pending = lines.pop()
            for raw in lines:
                line = raw.decode("utf-8", errors="replace").rstrip("\r")
                if self.relay_line(line):
                    return 0

    def relay_line(self, line):
        """Relay one line; True when it ends the run."""
        if line.startswith("Traceback (most recent call last):"):
            if not self.traceback_noted:
                self.traceback_noted = True
                self.note("err.log shows a Python traceback; the runner may "
                          f"have crashed — read {self.log_path}")
            return False
        if not line.startswith(FOLLOW_LINE_PREFIX):
            return False
        if not _reply_path_ok(line, self.replies_dir):
            return False
        if self.dispatched_at is None and line.startswith(
                FOLLOW_DISPATCH_PREFIX):
            self.dispatched_at = time.monotonic()
        if not self.verbose and FOLLOW_ROUTINE_PATTERN.match(line):
            return False
        _print_stdout(line)
        return bool(
            FOLLOW_DONE_PATTERN.match(line)
            or FOLLOW_INTERRUPTED_PATTERN.match(line)
            or FOLLOW_ABORTED_PATTERN.match(line)
        )

    def watch_clock(self):
        """Notice a system suspend (see FOLLOW_SUSPEND_SLACK_SECS)."""
        now_wall, now_mono = time.time(), time.monotonic()
        if (now_wall - self.last_wall) - (now_mono - self.last_mono) > (
            FOLLOW_SUSPEND_SLACK_SECS
        ):
            self.suspend_floor = now_wall
        self.last_wall, self.last_mono = now_wall, now_mono

    def check(self):
        """Parent, consumer, and runner checks; an exit code or None."""
        if os.getppid() != self.parent:
            return FOLLOW_EXIT_PARENT_GONE
        if _stdout_hung_up():
            with contextlib.suppress(Exception):
                sys.stdout.close()
            return FOLLOW_EXIT_STDOUT_GONE
        if self.dispatched_at is None:
            if time.monotonic() - self.started < FOLLOW_START_SECS:
                return None
            if self.fd is None:
                detail = (f"{self.log_path} did not appear within "
                          f"{FOLLOW_START_SECS}s; check the run directory path")
            else:
                detail = (f"{self.log_path} has no dispatch line after "
                          f"{FOLLOW_START_SECS}s; the launch may have failed "
                          "— read err.log")
            self.note(f"no council activity: {detail}")
            return FOLLOW_EXIT_NO_ACTIVITY
        view = read_status(self.status_path)
        if view is None or view.pid is None:
            if not self.status_noted and (
                time.monotonic() - self.dispatched_at
                >= FOLLOW_STATUS_WAIT_SECS
            ):
                self.status_noted = True
                self.note(f"no usable {STATUS_FILENAME} "
                          f"{FOLLOW_STATUS_WAIT_SECS}s after dispatch; "
                          "following err.log only, runner liveness unknown")
            return None
        runner = runner_state(view.pid, view.identity)
        if runner == "gone":
            # A terminal line written just before the exit wins.
            if self.relay() == 0:
                return 0
            if view.state in TERMINAL_STATES:
                self.note(f"runner finished: state={view.state} "
                          f"exit={view.exit}; err.log has no terminal line")
                return 0
            verified, _ = _codex_groups(view, _process_table())
            self.note(
                f"runner gone: pid={view.pid}; "
                f"unfinished={_id_list(_unfinished(view))}; "
                f"live codex groups={_id_list([str(g) for _, g, _ in verified])}"
                "; run --status"
            )
            return FOLLOW_EXIT_RUNNER_GONE
        if runner != "alive" or view.state != "running" or view.tick_at is None:
            return None
        age = time.time() - max(view.tick_at, self.suspend_floor)
        if age >= TICK_WARN_SECS:
            if not self.stale or age >= TICK_GIVE_UP_SECS:
                self.note(f"runner not responding: no status tick for "
                          f"{age:.0f}s (pid {view.pid} still present); "
                          "run --status")
            self.stale = True
            if age >= TICK_GIVE_UP_SECS:
                return FOLLOW_EXIT_RUNNER_GONE
        elif self.stale:
            self.stale = False
            self.note("runner responding again")
        return None

    def run(self):
        next_check = 0.0
        try:
            while True:
                if self.relay() == 0:
                    return 0
                self.watch_clock()
                if time.monotonic() >= next_check:
                    next_check = time.monotonic() + FOLLOW_CHECK_SECS
                    code = self.check()
                    if code is not None:
                        return code
                time.sleep(FOLLOW_POLL_SECS)
        finally:
            if self.fd is not None:
                with contextlib.suppress(OSError):
                    os.close(self.fd)


def follow(run_dir, verbose=False):
    """Relay a council's actionable err.log lines; return the exit code.

    Read-only by construction: it opens nothing for writing and relays only
    complete lines that begin with "[codex-council". Per-attempt start lines
    and the heartbeat stay in err.log unless `verbose`. Reply text never
    reaches err.log and runner diagnostics escape codex-controlled text, so
    role output cannot forge a line through the runner; but roles run
    unsandboxed as the same user and can append to err.log directly, which
    no same-uid check can authenticate. The follower therefore drops any
    completion line whose reply= path is not directly inside this run's
    replies/ directory, and SKILL.md bases the final verdict on the tracked
    background-task completion. A new follower reads err.log from the
    start.

    Every FOLLOW_CHECK_SECS it also checks its own parent (exit 5 once
    reparented), its stdout reader (exit 1 once gone), and, after dispatch,
    the runner recorded in status.json: gone without a terminal line is one
    `runner gone` line and exit 4; present with a tick older than
    TICK_WARN_SECS is one `runner not responding` line (then `runner
    responding again` on recovery) and exit 4 at TICK_GIVE_UP_SECS. Exit 0
    after the CODEX_COUNCIL_DONE sentinel, an interruption line, or a
    `runner aborted` line; exit 3 when no dispatch line appears within
    FOLLOW_START_SECS. A Python traceback in err.log is one advisory line.
    """
    run_dir = _check_private_dir(
        run_dir, prefix="--follow: ", recovery=RUN_DIR_RECOVERY
    )
    return _Follower(run_dir, verbose).run()


# ---------- --status and --reap ----------

def _role_line(role_id, role, now, table):
    parts = [role["state"]]
    if role["attempt"]:
        parts.append(f"attempt {role['attempt']}")
    if role["state"] == "active" and role["output_at"] is not None:
        parts.append(f"quiet {max(0.0, now - role['output_at']):.0f}s")
    if role["pid"] is not None:
        alive = _identity_state(table, role["pid"], role["start_identity"])
        parts.append(f"codex pid {role['pid']} {alive}")
    return f"  {_report_inline(role_id)}: {', '.join(parts)}"


def _status_lines(view, now, table):
    """The --status report (at most about ten lines)."""
    if view is None:
        return [
            f"runner: unknown (no usable {STATUS_FILENAME}: the council has "
            "not dispatched, or this is not its run directory)",
            "next: read err.log; a launch that dispatched writes "
            f"{STATUS_FILENAME} at once",
        ]
    runner = _identity_state(table, view.pid, view.identity)
    tick_age = None if view.tick_at is None else max(0.0, now - view.tick_at)
    unfinished = _unfinished(view)
    groups = ([], [])
    if view.state in TERMINAL_STATES:
        label = f"{view.state} (exit {view.exit})"
        if view.state == "done":
            action = "read out.md; the run has ended"
        else:
            action = ("read err.log and replies/; re-run unfinished roles in "
                      "a new directory")
    elif runner == "gone":
        label = f"gone (pid {view.pid} is no longer this run's runner)"
        groups = _codex_groups(view, table)
        if groups[0]:
            action = ("confirm the council's background task has ended, run "
                      "--reap on this directory, then re-run unfinished roles "
                      "in a new directory")
        elif unfinished:
            action = ("re-run unfinished roles in a new directory; replies/ "
                      "keeps the settled ones")
        else:
            action = ("every role settled before the runner ended; read "
                      "replies/ (out.md may be incomplete)")
    elif runner == "unknown":
        label = f"unknown (pid {view.pid}; ps cannot tell)"
        action = "re-check shortly"
    elif tick_age is None or tick_age >= TICK_WARN_SECS:
        age = "never" if tick_age is None else f"{tick_age:.0f}s ago"
        label = f"not responding (pid {view.pid} present; last status tick {age})"
        action = ("check the council's background task; the runner process "
                  "is present, so never reap it")
    else:
        label = f"running (pid {view.pid}; status tick {tick_age:.0f}s ago)"
        action = "keep following; do not relaunch"
    lines = [f"runner: {label}",
             f"roles: {len(view.roles) - len(unfinished)} of "
             f"{len(view.roles)} settled"]
    for role_id in unfinished[:STATUS_ROLE_LINES]:
        lines.append(_role_line(role_id, view.roles[role_id], now, table))
    if len(unfinished) > STATUS_ROLE_LINES:
        lines.append(f"  ... and {len(unfinished) - STATUS_ROLE_LINES} more "
                     "unfinished")
    verified, unverified = groups
    if runner == "gone" and view.state not in TERMINAL_STATES:
        lines.append("live codex groups: " + _id_list(
            [f"{pgid} ({role_id})" for role_id, pgid, _ in verified]))
        if unverified:
            lines.append("unverified groups (left alone): " + _id_list(
                [f"{pgid} ({role_id})" for role_id, pgid, _ in unverified]))
    lines.append(f"next: {action}")
    return lines


def status_command(run_dir):
    """--status RUNDIR: print the run's snapshot; always exit 0."""
    run_dir = _check_private_dir(
        run_dir, prefix="--status: ", recovery=RUN_DIR_RECOVERY
    )
    view = read_status(os.path.join(run_dir, STATUS_FILENAME))
    table = _process_table() if view is not None else None
    for line in _status_lines(view, time.time(), table):
        _print_stdout(line)
    return 0


def reap_command(run_dir):
    """--reap RUNDIR: end a gone runner's live codex groups; 0 or 1.

    Refused (exit 1) unless status.json shows a runner that is gone. Only
    verified groups are signalled (see _codex_groups); saved threads,
    replies, and every file are left as they are.
    """
    run_dir = _check_private_dir(
        run_dir, prefix="--reap: ", recovery=RUN_DIR_RECOVERY
    )
    view = read_status(os.path.join(run_dir, STATUS_FILENAME))
    if view is None or view.pid is None:
        _print_stdout(f"--reap refused: no usable {STATUS_FILENAME}, so "
                      "nothing is known about this run's processes")
        return 1
    table = _process_table()
    runner = _identity_state(table, view.pid, view.identity)
    if runner != "gone":
        why = ("ps cannot tell whether it is running" if runner == "unknown"
               else "it is still present and owns its codex process groups")
        _print_stdout(f"--reap refused: the runner (pid {view.pid}): {why}; "
                      "run --status")
        return 1
    verified, unverified = _codex_groups(view, table)
    for role_id, pgid, members in verified:
        _terminate_group(pgid)
        count = f"{len(members)} process{'es' if len(members) != 1 else ''}"
        _print_stdout(f"reaped {_report_inline(role_id)}: process group "
                      f"{pgid} ({count}) terminated")
    for role_id, pgid, _ in unverified:
        _print_stdout(f"left alone {_report_inline(role_id)}: process group "
                      f"{pgid} is not verifiably this run's codex")
    if not verified and not unverified:
        _print_stdout("nothing to reap: no recorded codex process group of "
                      "this run is alive")
    unfinished = _unfinished(view)
    _print_stdout("saved threads and files are untouched" + (
        f"; re-run unfinished roles ({_id_list(unfinished)}) in a new "
        "directory" if unfinished else ""))
    return 0
