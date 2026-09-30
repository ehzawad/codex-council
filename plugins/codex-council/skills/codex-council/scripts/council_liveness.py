"""Run liveness for the codex-council runner (see codex_council.py).

The runner publishes RUNDIR/status.json through RunStatus: its own pid and
OS start identity, its state, a tick that advances on every role transition
and at least every STATUS_TICK_SECS, and per role the scheduling state, the
attempt, the live codex process group, and the outcome. A detached run
(launched with --start) also has RUNDIR/supervisor.lock, which its runner
holds locked (flock) for its whole life, and RUNDIR/supervisor.json, which
that runner writes about itself before any other work. Two read-only
commands and two explicit commands use those files:

* --follow RUNDIR relays the actionable lines of RUNDIR/err.log (one stdout
  line per event, for a Claude Code Monitor), reports a runner that is
  gone or has stopped ticking, and after 600s with nothing relayed prints
  one `still running` keepalive line while the runner is alive;
* --status RUNDIR prints a short snapshot and one next action;
* --reap RUNDIR, only once the runner is gone, terminates the recorded codex
  process groups whose leader is still this run's codex, and the process
  groups and processes that live codex started;
* --cancel RUNDIR stops a detached run's verified runner (SIGTERM, and
  SIGKILL after a grace), which then tears down its own codex groups.

A process identity is its pid plus its start time as `ps -o lstart=` prints
it (C locale, UTC): the same pid with another start time is another
process. A detached runner is alive while its lock is held, and gone only
when the lock is free and its recorded identity is gone; any disagreement
reads unknown and nothing is signalled. Reports state facts (present, gone,
tick age, quiet seconds), never health. Nothing here writes anything but
status.json, and only the runner writes that (and supervisor.json).
"""

import contextlib
import dataclasses
import fcntl
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

from council_common import (
    _READ_CHUNK_BYTES,
    REPLIES_SUBDIR,
    REPLY_MARKER,
    SUPERVISOR_FILENAME,
    SUPERVISOR_LOCK_FILENAME,
    _atomic_write_private,
    _check_private_dir,
    _diag,
    _log_inline,
    _print_stdout,
    _private_stat_problem,
    _report_inline,
    _usage_exit,
)

STATUS_FILENAME = "status.json"
STATUS_SCHEMA = 1
# The runner republishes status.json at least this often while it runs.
STATUS_TICK_SECS = 15
ROLE_STATES = ("queued", "active", "retry-wait", "settled")
TERMINAL_STATES = ("done", "interrupted", "aborted")
# runner.mode in status.json (optional): "detached" for a --start runner.
RUNNER_MODES = ("tracked", "detached")
SUPERVISOR_SCHEMA = 1
# Every ps call is bounded; a timeout reads as "cannot tell", never "gone".
PS_TIMEOUT_SECS = 2
# ps prints lstart in local time and the locale's words; pin both so the
# runner and a later reader always print the same start identity.
_PS_ENV = {"LC_ALL": "C", "TZ": "UTC0"}

# --follow timing: read err.log every FOLLOW_POLL_SECS, look at the runner
# and the follower's own parent every FOLLOW_CHECK_SECS; no dispatch line
# within FOLLOW_START_SECS means no council activity. No usable status.json
# for STATUS_UNUSABLE_WARN_SECS after dispatch (or after its last usable
# read) is reported once. A tick older than TICK_WARN_SECS while the runner
# is present is reported once; at TICK_GIVE_UP_SECS the follower stops
# (exit 4).
FOLLOW_POLL_SECS = 0.5
FOLLOW_CHECK_SECS = 2
FOLLOW_START_SECS = 120
STATUS_UNUSABLE_WARN_SECS = 30
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
# After dispatch, while the runner is alive and ticking, a follower that has
# relayed nothing for FOLLOW_KEEPALIVE_SECS prints one `still running` line
# naming at most KEEPALIVE_ROLES active roles. Each line is a Monitor event
# that wakes Claude, so a long, quiet council still shows progress.
FOLLOW_KEEPALIVE_SECS = 600
KEEPALIVE_ROLES = 5
# The runner's role-id grammar (codex_council.ROLE_ID_PATTERN): only ids of
# this shape are named in a keepalive line.
_ROLE_ID_RE = re.compile(r"^[a-z0-9_-]+\Z")
# --reap: SIGTERM, up to this long for the group to empty, then SIGKILL.
REAP_GRACE_SECS = 2
# --start creates supervisor.lock and then locks it (retrying for up to a
# second past a reader's momentary shared lock). For this long after the
# file appears, a free lock with no record is "unknown", not "gone".
LOCK_CLAIM_GRACE_SECS = 3
# --cancel: SIGCONT and SIGTERM to the verified runner, then up to this long
# for it to release its lock; a runner still holding it with the same
# identity then gets SIGKILL and up to CANCEL_KILL_WAIT_SECS more. These
# bound the cancel command only, never a running council.
CANCEL_GRACE_SECS = 30
CANCEL_KILL_WAIT_SECS = 5
CANCEL_POLL_SECS = 0.1
# --cancel exit codes: 0 = the runner has ended, 1 = refused or not ended.
CANCEL_EXIT_REFUSED = 1
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
# Recovery for a rejected run directory. These commands only read (or
# signal verified processes), so the fix is always "point at the real run
# directory", never chmod or mkdir.
RUN_DIR_RECOVERY = (
    "Recovery: pass the exact absolute mktemp directory the council was "
    "launched from (the directory holding roles.json, out.md, and err.log). "
    "--follow, --status, --reap, and --cancel only read DIR/err.log, "
    "DIR/status.json, and the supervisor files; do not chmod or mkdir "
    "anything for them."
)


# ---------- process identity ----------

def _process_table(pid=None):
    """{pid: (pgid, stat, start identity, ppid)} from one bounded ps call.

    All processes, or only `pid`. None when ps cannot tell (missing, timed
    out, or reporting an error); an empty table means no such process.
    """
    args = ["-o", "pid=", "-o", "ppid=", "-o", "pgid=", "-o", "stat=",
            "-o", "lstart="]
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
        if len(fields) < 5:
            continue
        try:
            table[int(fields[0])] = (int(fields[2]), fields[3],
                                     " ".join(fields[4:]), int(fields[1]))
        except ValueError:
            continue
    return table


def descendant_targets(root_pid, table=None):
    """What else to signal so root_pid's whole process tree ends.

    Returns (groups, pids) from one ps snapshot, walking parent links down
    from root_pid while it is alive. A descendant that leads its own
    process group (current codex starts each tool command in its own
    session) contributes that group; any other descendant outside root's
    group is named by pid. Root's own group, this process's group, and ids
    <= 1 are never included. A descendant already reparented away from the
    tree (its parent exited) cannot be traced.
    """
    table = _process_table() if table is None else table
    if not table or root_pid not in table:
        return [], []
    children = {}
    for pid, entry in table.items():
        children.setdefault(entry[3], []).append(pid)
    skip = {table[root_pid][0], os.getpgrp(), 0, 1}
    tree, stack = set(), [root_pid]
    while stack:
        for child in children.get(stack.pop(), ()):
            if child not in tree:
                tree.add(child)
                stack.append(child)
    groups = sorted({table[pid][0] for pid in tree
                     if table[pid][0] in tree and table[pid][0] not in skip})
    pids = sorted(pid for pid in tree
                  if table[pid][0] not in skip and table[pid][0] not in groups)
    return groups, pids


def signal_targets(groups, pids, sig):
    """Best-effort signal to each group and pid; vanished ones are fine."""
    for pgid in groups:
        with contextlib.suppress(OSError):
            os.killpg(pgid, sig)
    for pid in pids:
        with contextlib.suppress(OSError):
            os.kill(pid, sig)


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
        members = sorted(pid for pid, (group, state, *_) in table.items()
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


def _terminate_group(pgid, table=None):
    """End a codex process group and its tool sessions; return how many
    other groups and processes (its descendants) were signalled.

    SIGTERM to all, wait up to REAP_GRACE_SECS for the codex group, then
    SIGKILL whatever of them remains.
    """
    groups, pids = descendant_targets(pgid, table)
    signal_targets([pgid, *groups], pids, signal.SIGTERM)
    deadline = time.monotonic() + REAP_GRACE_SECS
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except OSError:
            break
        time.sleep(0.05)
    signal_targets([pgid, *groups], pids, signal.SIGKILL)
    return len(groups) + len(pids)


# ---------- status.json: the runner's writer ----------

class RunStatus:
    """The runner's live state, published as RUNDIR/status.json.

    Roles and their last-output stamps live in memory for the heartbeat;
    once attach() names a file, every transition and every tick of the
    event loop republishes it atomically (0600). Each publish is a tick:
    tick.seq grows and tick.at is its wall time, so a fresh tick shows the
    runner's event loop is turning. A failed write is reported once, removes
    the file an earlier write left (so no stale tick remains), and never
    affects the council.
    """

    def __init__(self):
        self.path = None
        self.run_id = None
        self.runner = {}
        self.roles = {}
        self.seq = 0
        self._write_failed = False

    def attach(self, path, mode=None):
        """Publish to `path` from now on (the launch path only).

        `mode` "detached" (a --start supervisor) is recorded as runner.mode,
        an optional schema-1 field; a tracked launch records none.
        """
        pid = os.getpid()
        self.path = path
        self.run_id = os.urandom(8).hex()
        self.runner = {"pid": pid, "start_identity": process_start_identity(pid),
                       "state": "running"}
        if mode is not None:
            self.runner["mode"] = mode

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
            # An earlier file would keep an ageing tick and make a live
            # runner look unresponsive: better no file than a stale one.
            with contextlib.suppress(OSError):
                os.remove(self.path)
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
    pid: int | None
    identity: str | None
    state: str
    exit: int | None
    tick_at: float | None
    roles: dict
    mode: str | None = None


def _int(value, minimum):
    if isinstance(value, int) and not isinstance(value, bool) \
            and value >= minimum:
        return value
    return None


def _number(value):
    """A finite float, or None (also for an int no float can hold)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _text(value):
    return value if isinstance(value, str) and value else None


def read_status(path):
    """status.json as a RunView, or None when there is no usable file.

    Unknown fields are ignored and a consumed field of the wrong type, or a
    number no float can hold, reads as unknown (None), so an unexpected
    file degrades to fewer facts, never to a wrong claim.
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
        mode=runner.get("mode") if runner.get("mode") in RUNNER_MODES
        else None,
    )


# ---------- supervisor.json and supervisor.lock: detached runs ----------

@dataclasses.dataclass
class SupervisorView:
    """The supervisor.json fields the commands below use."""
    pid: int | None
    identity: str | None
    pgid: int | None
    sid: int | None
    lock_dev: int | None
    lock_ino: int | None
    lock_token: str | None
    version: str | None
    epoch: int | None
    started_at: str | None


LOCK_TOKEN_MAX_BYTES = 64


def read_lock_token(fd):
    """The token --start wrote into supervisor.lock (read at offset 0 without
    moving the descriptor), or None when there is none or it is unreadable."""
    try:
        raw = os.pread(fd, LOCK_TOKEN_MAX_BYTES + 1, 0)
    except OSError:
        return None
    if not raw or len(raw) > LOCK_TOKEN_MAX_BYTES:
        return None
    try:
        token = raw.decode("ascii")
    except UnicodeDecodeError:
        return None
    return token if re.fullmatch(r"[0-9a-f]{16,64}", token) else None


def supervisor_record(pid, lock_stat, version, epoch, started_at,
                      lock_token=None):
    """The supervisor.json object a detached runner writes about itself."""
    lock = {"dev": lock_stat.st_dev, "ino": lock_stat.st_ino}
    if lock_token is not None:
        lock["token"] = lock_token
    return {
        "schema": SUPERVISOR_SCHEMA,
        "pid": pid,
        "start_identity": process_start_identity(pid),
        "pgid": os.getpgid(0),
        "sid": os.getsid(0),
        "lock": lock,
        "version": version,
        "epoch": epoch,
        "started_at": started_at,
    }


def read_supervisor(path):
    """supervisor.json as a SupervisorView, or None when unusable.

    Tolerant like read_status: a field of the wrong type reads as unknown
    (None). The file must also be a private regular file (not a symlink,
    this user's, mode 0600 or tighter), since --cancel signals the pid it
    names.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                     | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as f:
            if _private_stat_problem(os.fstat(f.fileno()),
                                     directory=False) is not None:
                return None
            data = json.loads(f.read())
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    lock = data.get("lock") if isinstance(data.get("lock"), dict) else {}
    return SupervisorView(
        pid=_int(data.get("pid"), 2),
        identity=_text(data.get("start_identity")),
        pgid=_int(data.get("pgid"), 2),
        sid=_int(data.get("sid"), 2),
        lock_dev=_int(lock.get("dev"), 0),
        lock_ino=_int(lock.get("ino"), 0),
        lock_token=_text(lock.get("token")),
        version=_text(data.get("version")),
        epoch=_int(data.get("epoch"), 0),
        started_at=_text(data.get("started_at")),
    )


def lock_state(run_dir, supervisor=None):
    """"absent", "held", "free", or "unknown" for RUNDIR/supervisor.lock.

    Opens read-only without following a symlink or creating anything,
    requires a private regular file (and, when `supervisor` records one,
    the same device and inode, plus the same token when one was recorded:
    a replaced lock file is unknown even if it reuses the old inode
    number), then tries
    a shared lock without blocking: refused means the runner holds it. The
    shared lock, if taken, is released at once by the close.
    """
    path = os.path.join(run_dir, SUPERVISOR_LOCK_FILENAME)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                     | os.O_CLOEXEC)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unknown"
    try:
        st = os.fstat(fd)
        if _private_stat_problem(st, directory=False) is not None:
            return "unknown"
        if supervisor is not None and (
                supervisor.lock_dev != st.st_dev
                or supervisor.lock_ino != st.st_ino
                or (supervisor.lock_token is not None
                    and read_lock_token(fd) != supervisor.lock_token)):
            return "unknown"
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return "held"
        except OSError:
            return "unknown"
        return "free"
    finally:
        os.close(fd)


def is_detached(run_dir, view=None):
    """True for a run launched with --start (either supervisor file, or a
    status.json that says so); every other run reads as before."""
    return (
        any(os.path.lexists(os.path.join(run_dir, name))
            for name in (SUPERVISOR_LOCK_FILENAME, SUPERVISOR_FILENAME))
        or (view is not None and view.mode == "detached")
    )


def _lock_just_created(run_dir):
    """True while supervisor.lock is younger than LOCK_CLAIM_GRACE_SECS
    (by its mtime: the file is written once, as it is created)."""
    try:
        st = os.lstat(os.path.join(run_dir, SUPERVISOR_LOCK_FILENAME))
    except OSError:
        return False
    return -1 < time.time() - st.st_mtime < LOCK_CLAIM_GRACE_SECS


@dataclasses.dataclass
class Detached:
    """A detached run's combined liveness (see supervisor_state)."""
    state: str
    supervisor: SupervisorView | None
    lock: str

    @property
    def pid(self):
        return self.supervisor.pid if self.supervisor is not None else None


def supervisor_state(run_dir, view, table=None):
    """Combined liveness of a detached run: "alive", "gone", or "unknown".

    Alive: the lock is held and, once supervisor.json exists, its recorded
    runner is alive and agrees with status.json. Gone: the lock is free and
    the recorded identity is gone (or, with no supervisor.json yet, the
    runner ended before it could write one; within LOCK_CLAIM_GRACE_SECS
    of the lock file's creation that reads as unknown instead, since
    --start may not have locked it yet). Anything else (a held lock
    with a dead or unverifiable pid, a free lock with the recorded process
    still present, a replaced or unreadable lock, records naming different
    processes) is unknown: nothing may be signalled or reaped then.
    `table` is a process table (None looks the recorded pid up once).
    """
    sup_path = os.path.join(run_dir, SUPERVISOR_FILENAME)
    supervisor = read_supervisor(sup_path)
    lock = lock_state(run_dir, supervisor)
    if supervisor is None:
        if os.path.lexists(sup_path):
            return Detached("unknown", None, lock)
        # No record yet: the runner has not reached its first step.
        state = {"held": "alive", "free": "gone"}.get(lock, "unknown")
        if view is not None and state != "unknown":
            state = "unknown"  # status.json without supervisor.json
        elif state == "gone" and _lock_just_created(run_dir):
            state = "unknown"  # --start may not have locked it yet
        return Detached(state, None, lock)
    if supervisor.pid is None or lock not in ("held", "free"):
        return Detached("unknown", supervisor, lock)
    if view is not None and view.pid is not None and (
            view.pid != supervisor.pid
            or (view.identity is not None and supervisor.identity is not None
                and view.identity != supervisor.identity)):
        return Detached("unknown", supervisor, lock)
    if table is None:
        table = _process_table(supervisor.pid)
    identity = _identity_state(table, supervisor.pid, supervisor.identity)
    if lock == "held":
        return Detached("alive" if identity == "alive" else "unknown",
                        supervisor, lock)
    return Detached("gone" if identity == "gone" else "unknown",
                    supervisor, lock)


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
        self.run_dir = run_dir
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
        self.status_seen_at = 0.0
        self.unusable_noted = False
        self.stale = False
        self.suspend_floor = 0.0
        self.last_wall, self.last_mono = time.time(), time.monotonic()
        # The last line this follower printed, and the status.json view of
        # a runner last seen alive and ticking (None otherwise): together
        # they decide the keepalive.
        self.last_emit = time.monotonic()
        self.live_view = None

    def emit(self, text):
        _print_stdout(text)
        self.last_emit = time.monotonic()

    def note(self, text):
        self.emit(f"{FOLLOW_NOTE} {text}")

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
        self.emit(line)
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
        self.live_view = None
        if os.getppid() != self.parent:
            return FOLLOW_EXIT_PARENT_GONE
        if _stdout_hung_up():
            with contextlib.suppress(Exception):
                sys.stdout.close()
            return FOLLOW_EXIT_STDOUT_GONE
        if self.dispatched_at is None:
            if is_detached(self.run_dir) and supervisor_state(
                    self.run_dir, read_status(self.status_path)
            ).state == "gone":
                # The lock is free and nothing dispatched: the runner has
                # ended (a refused launch, or a stop during discovery).
                if self.relay() == 0:
                    return 0
                self.note(f"runner ended before dispatch: read "
                          f"{self.log_path}")
                return FOLLOW_EXIT_NO_ACTIVITY
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
        now = time.monotonic()
        detached = (supervisor_state(self.run_dir, view)
                    if is_detached(self.run_dir, view) else None)
        if view is None or view.pid is None:
            if detached is not None and detached.state == "gone":
                if self.relay() == 0:
                    return 0
                self.note(f"runner gone: pid={detached.pid}; no usable "
                          f"{STATUS_FILENAME}; run --status")
                return FOLLOW_EXIT_RUNNER_GONE
            # Nothing to check yet, or the runner could not write the file
            # (it says so in err.log, which is relayed): err.log alone, and
            # one line once that has lasted STATUS_UNUSABLE_WARN_SECS.
            unusable = now - max(self.dispatched_at, self.status_seen_at)
            if not self.unusable_noted and (
                    unusable >= STATUS_UNUSABLE_WARN_SECS):
                self.unusable_noted = True
                self.note(f"runner liveness unavailable: no usable "
                          f"{STATUS_FILENAME}; following err.log only; run "
                          "--status")
            return None
        self.status_seen_at = now
        if detached is not None:
            # The lock decides: alive while held, gone only when it is free
            # and the recorded identity is gone, unknown otherwise.
            runner = detached.state
        else:
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
        else:
            self.live_view = view
        return None

    def keepalive(self):
        """One `still running` line after FOLLOW_KEEPALIVE_SECS of silence.

        Only after dispatch and while the last check saw the runner alive
        and ticking. Built only from counts, validated role ids, and
        numbers, so no text from a role or a file can reach it.
        """
        view = self.live_view
        if view is None or self.dispatched_at is None:
            return
        if time.monotonic() - self.last_emit < FOLLOW_KEEPALIVE_SECS:
            return
        now = time.time()
        unfinished = _unfinished(view)
        active = [(rid, role) for rid, role in view.roles.items()
                  if role["state"] == "active"]
        shown = []
        for role_id, role in active:
            if len(shown) == KEEPALIVE_ROLES:
                break
            if not isinstance(role_id, str) or not _ROLE_ID_RE.match(role_id):
                continue
            if role["output_at"] is None:
                shown.append(role_id)
            else:
                quiet = max(0.0, now - role["output_at"])
                shown.append(f"{role_id} quiet={quiet:.0f}s")
        if len(active) > len(shown):
            shown.append(f"+{len(active) - len(shown)} more")
        tick_age = max(0.0, now - max(view.tick_at, self.suspend_floor))
        self.note(
            f"still running: {len(view.roles) - len(unfinished)}/"
            f"{len(view.roles)} settled; active: {', '.join(shown) or 'none'}"
            f"; status tick {tick_age:.0f}s ago"
        )

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
                self.keepalive()
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
    replies/ directory, and SKILL.md bases the final verdict on a verified
    runner exit (the tracked task's completion, or for a detached run a
    released lock and a vanished runner identity). A new follower reads err.log from the
    start.

    Every FOLLOW_CHECK_SECS it also checks its own parent (exit 5 once
    reparented), its stdout reader (exit 1 once gone), and, after dispatch,
    the runner recorded in status.json: gone without a terminal line is one
    `runner gone` line and exit 4; present with a tick older than
    TICK_WARN_SECS is one `runner not responding` line (then `runner
    responding again` on recovery) and exit 4 at TICK_GIVE_UP_SECS; no
    usable status.json for STATUS_UNUSABLE_WARN_SECS is one `runner
    liveness unavailable` line, and err.log is still relayed. Exit 0
    after the CODEX_COUNCIL_DONE sentinel, an interruption line, or a
    `runner aborted` line; exit 3 when no dispatch line appears within
    FOLLOW_START_SECS. A Python traceback in err.log is one advisory line.
    For a detached run the supervisor lock decides the runner's liveness
    (supervisor_state), and a free lock with no dispatch line is one
    `runner ended before dispatch` line and exit 3 at once. After dispatch,
    FOLLOW_KEEPALIVE_SECS with nothing relayed while the runner is alive and
    ticking prints one `still running` line (see keepalive).
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


def _detached_label(view, now, detached, unfinished, table):
    """(label, action, groups) for a detached run's --status report."""
    groups = ([], [])
    pid = detached.pid if detached.pid is not None else (
        view.pid if view is not None else None)
    # A runner that has not written supervisor.json yet has no pid on record.
    who = "no pid recorded" if pid is None else f"pid {pid}"
    tick_age = (None if view is None or view.tick_at is None
                else max(0.0, now - view.tick_at))
    if detached.state == "unknown":
        label = (f"unknown ({who}; supervisor lock {detached.lock}; the "
                 "lock and the process records disagree or cannot be read)")
        action = ("re-check --status shortly; never --reap, --cancel, or "
                  "relaunch while the runner reads unknown")
    elif detached.state == "alive":
        if view is None:
            label = (f"starting ({who}; supervisor lock held; no "
                     "dispatch yet)")
            action = ("keep following; --cancel stops it; do not relaunch")
        elif view.state in TERMINAL_STATES:
            label = (f"{view.state} (exit {view.exit}); the runner is still "
                     f"exiting (pid {pid}; supervisor lock held)")
            action = ("re-check --status in a few seconds; out.md is final "
                      "only once --status reports that the runner ended")
        elif tick_age is None or tick_age >= TICK_WARN_SECS:
            age = "never" if tick_age is None else f"{tick_age:.0f}s ago"
            label = (f"not responding (pid {pid} present; supervisor lock "
                     f"held; last status tick {age})")
            action = (f"unless err.log says {STATUS_FILENAME} not written, "
                      "run --cancel on this directory, then --reap if it "
                      "says to")
        else:
            label = (f"running (pid {pid}; detached; status tick "
                     f"{tick_age:.0f}s ago)")
            action = "keep following; do not relaunch; --cancel stops it"
    elif view is None:
        label = (f"ended before dispatch ({who}"
                 f"{'' if pid is None else ' gone'}; supervisor lock free)")
        action = ("read err.log; start over in a new directory, never in "
                  "this one")
    elif view.state in TERMINAL_STATES:
        label = f"{view.state} (exit {view.exit})"
        if view.state == "done":
            action = "read out.md; the run has ended"
        else:
            action = ("read err.log and replies/; re-run unfinished roles in "
                      "a new directory")
    else:
        label = (f"gone (pid {pid} is no longer this run's runner; "
                 "supervisor lock free)")
        groups = _codex_groups(view, table)
        if groups[0]:
            action = ("run --reap on this directory, then re-run unfinished "
                      "roles in a new directory")
        elif unfinished:
            action = ("re-run unfinished roles in a new directory; replies/ "
                      "keeps the settled ones")
        else:
            action = ("every role settled before the runner ended; read "
                      "replies/ (out.md may be incomplete)")
    return label, action, groups


def _status_lines(view, now, table, detached=None):
    """The --status report (at most about ten lines).

    `detached` is a detached run's combined liveness (supervisor_state);
    None reports a tracked run from status.json alone.
    """
    if detached is not None:
        unfinished = _unfinished(view) if view is not None else []
        label, action, groups = _detached_label(
            view, now, detached, unfinished, table)
        lines = [f"runner: {label}"]
        if view is not None:
            lines += _role_lines(view, now, table, unfinished)
        if (detached.state == "gone" and view is not None
                and view.state not in TERMINAL_STATES):
            lines += _group_lines(groups)
        lines.append(f"next: {action}")
        return lines
    if view is None:
        return [
            f"runner: unknown (no usable {STATUS_FILENAME}: the council has "
            "not dispatched, its runner could not write the file, or this "
            "is not its run directory)",
            "next: read err.log; a launch that dispatched writes "
            f"{STATUS_FILENAME} at once, and err.log says when it cannot",
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
        action = (f"unless err.log says {STATUS_FILENAME} not written, stop "
                  "the council's background task, confirm with --status that "
                  "the runner is gone, then --reap if live codex groups remain")
    else:
        label = f"running (pid {view.pid}; status tick {tick_age:.0f}s ago)"
        action = "keep following; do not relaunch"
    lines = [f"runner: {label}", *_role_lines(view, now, table, unfinished)]
    if runner == "gone" and view.state not in TERMINAL_STATES:
        lines += _group_lines(groups)
    lines.append(f"next: {action}")
    return lines


def _role_lines(view, now, table, unfinished):
    lines = [f"roles: {len(view.roles) - len(unfinished)} of "
             f"{len(view.roles)} settled"]
    for role_id in unfinished[:STATUS_ROLE_LINES]:
        lines.append(_role_line(role_id, view.roles[role_id], now, table))
    if len(unfinished) > STATUS_ROLE_LINES:
        lines.append(f"  ... and {len(unfinished) - STATUS_ROLE_LINES} more "
                     "unfinished")
    return lines


def _group_lines(groups):
    verified, unverified = groups
    lines = ["live codex groups: " + _id_list(
        [f"{pgid} ({role_id})" for role_id, pgid, _ in verified])]
    if unverified:
        lines.append("unverified groups (left alone): " + _id_list(
            [f"{pgid} ({role_id})" for role_id, pgid, _ in unverified]))
    return lines


def status_command(run_dir):
    """--status RUNDIR: print the run's snapshot; always exit 0.

    A detached run (see is_detached) is reported from its combined
    liveness: starting, running, not responding, a terminal state once the
    lock is free and the runner gone, ended before dispatch, gone, or
    unknown. Read-only: it never writes, repairs, or signals anything.
    """
    run_dir = _check_private_dir(
        run_dir, prefix="--status: ", recovery=RUN_DIR_RECOVERY
    )
    view = read_status(os.path.join(run_dir, STATUS_FILENAME))
    detached = None
    if is_detached(run_dir, view):
        table = _process_table()
        detached = supervisor_state(run_dir, view, table)
    else:
        table = _process_table() if view is not None else None
    for line in _status_lines(view, time.time(), table, detached):
        _print_stdout(line)
    return 0


def reap_command(run_dir):
    """--reap RUNDIR: end a gone runner's live codex groups; 0 or 1.

    Refused (exit 1) unless status.json shows a runner that is gone and,
    for a detached run, its supervisor lock is free (a held lock refuses
    even when ps says the pid is gone; see supervisor_state). Only
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
    if is_detached(run_dir, view):
        # The lock decides first: held means a live runner owns its groups
        # whatever ps says, and any disagreement is unknown.
        detached = supervisor_state(run_dir, view, table)
        if detached.state != "gone":
            why = ("its supervisor lock is still held" if detached.lock
                   == "held" else "the supervisor lock and the process "
                   "records disagree or cannot be read")
            _print_stdout(f"--reap refused: the runner (pid {view.pid}): "
                          f"{why}; run --status")
            return 1
    runner = _identity_state(table, view.pid, view.identity)
    if runner != "gone":
        why = ("ps cannot tell whether it is running" if runner == "unknown"
               else "it is still present and owns its codex process groups")
        _print_stdout(f"--reap refused: the runner (pid {view.pid}): {why}; "
                      "run --status")
        return 1
    verified, unverified = _codex_groups(view, table)
    for role_id, pgid, members in verified:
        tools = _terminate_group(pgid, table)
        count = f"{len(members)} process{'es' if len(members) != 1 else ''}"
        extra = (f" and {tools} more process group"
                 f"{'s' if tools != 1 else ''} it started" if tools else "")
        _print_stdout(f"reaped {_report_inline(role_id)}: process group "
                      f"{pgid} ({count}){extra} terminated")
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


# ---------- --cancel ----------

def _verified_supervisor(run_dir, supervisor, view):
    """Why the recorded supervisor may not be signalled, or None.

    Requires the lock held (same inode as recorded), the recorded pid alive
    with its recorded start identity and process group, and status.json
    (when present) naming the same runner.
    """
    if supervisor is None or supervisor.pid is None \
            or supervisor.identity is None:
        return "supervisor.json names no verifiable runner"
    if view is not None and view.pid is not None and (
            view.pid != supervisor.pid
            or (view.identity is not None
                and view.identity != supervisor.identity)):
        return (f"{STATUS_FILENAME} and {SUPERVISOR_FILENAME} name "
                "different runners")
    lock = lock_state(run_dir, supervisor)
    if lock != "held":
        return f"the supervisor lock is {lock}"
    table = _process_table(supervisor.pid)
    if _identity_state(table, supervisor.pid, supervisor.identity) != "alive":
        return (f"pid {supervisor.pid} is not the recorded runner (gone, "
                "reused, or ps cannot tell)")
    entry = table.get(supervisor.pid)
    if supervisor.pgid is not None and entry[0] != supervisor.pgid:
        return f"pid {supervisor.pid} is in another process group"
    return None


def _wait_lock_released(run_dir, supervisor, seconds):
    """True once the lock is no longer held (polls up to `seconds`)."""
    deadline = time.monotonic() + seconds
    while True:
        if lock_state(run_dir, supervisor) != "held":
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(CANCEL_POLL_SECS)


def cancel_command(run_dir):
    """--cancel RUNDIR: stop a detached run's runner; 0 once it has ended.

    Refused (exit 1, nothing signalled) unless the run was launched with
    --start, its supervisor lock is held, and the recorded runner is
    verified (see _verified_supervisor). Then SIGCONT and SIGTERM to that
    one pid: the runner tears down its own codex process groups and writes
    its interruption line. If it still holds the lock with the same
    identity after CANCEL_GRACE_SECS, it is verified again and gets
    SIGKILL, and --reap ends what it left behind.
    """
    run_dir = _check_private_dir(
        run_dir, prefix="--cancel: ", recovery=RUN_DIR_RECOVERY
    )
    view = read_status(os.path.join(run_dir, STATUS_FILENAME))
    if not is_detached(run_dir, view):
        _print_stdout(
            "--cancel refused: this run was not launched with --start (no "
            f"{SUPERVISOR_LOCK_FILENAME}); a tracked run ends when its "
            "background task is stopped")
        return CANCEL_EXIT_REFUSED
    detached = supervisor_state(run_dir, view)
    supervisor = detached.supervisor
    if detached.state == "gone":
        _print_stdout(
            f"--cancel refused: the runner "
            f"{'' if detached.pid is None else f'(pid {detached.pid}) '}"
            "has already ended; run --status, then --reap if it lists live "
            "codex groups")
        return CANCEL_EXIT_REFUSED
    if detached.state == "alive" and supervisor is None:
        _print_stdout(
            "--cancel refused: the runner is starting and has not written "
            f"{SUPERVISOR_FILENAME} yet; run --cancel again in a few seconds")
        return CANCEL_EXIT_REFUSED
    problem = _verified_supervisor(run_dir, supervisor, view)
    if detached.state != "alive" or problem is not None:
        _print_stdout(
            f"--cancel refused: {problem or 'the runner reads unknown'}; "
            "nothing was signalled; run --status")
        return CANCEL_EXIT_REFUSED
    pid = supervisor.pid
    for sig in (signal.SIGCONT, signal.SIGTERM):
        with contextlib.suppress(OSError):
            os.kill(pid, sig)
    if _wait_lock_released(run_dir, supervisor, CANCEL_GRACE_SECS):
        _print_stdout(
            f"cancelled: the runner (pid {pid}) ended after SIGTERM and "
            "tore down its codex process groups; read err.log and "
            "replies/, and run --status")
        return 0
    problem = _verified_supervisor(run_dir, supervisor, view)
    if problem is not None:
        _print_stdout(
            f"--cancel: the runner (pid {pid}) did not end within "
            f"{CANCEL_GRACE_SECS}s of SIGTERM, and SIGKILL was not sent: "
            f"{problem}; run --status")
        return CANCEL_EXIT_REFUSED
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)
    if _wait_lock_released(run_dir, supervisor, CANCEL_KILL_WAIT_SECS):
        _print_stdout(
            f"cancelled: the runner (pid {pid}) did not end within "
            f"{CANCEL_GRACE_SECS}s of SIGTERM and was sent SIGKILL; its "
            "codex process groups may still run: run --reap on this "
            "directory")
        return 0
    _print_stdout(
        f"--cancel: the runner (pid {pid}) still holds its lock after "
        "SIGKILL; run --status")
    return CANCEL_EXIT_REFUSED
