"""Persistent run directories: --new-run allocation and --prune-runs.

Each council gets <state dir>/runs/<UTC YYYYMMDDTHHMMSSZ>-<16 hex>/, so a
reboot that clears $TMPDIR keeps its evidence. Nothing is deleted
automatically: --prune-runs lists, and with --delete removes, only finished
runs whose recorded runner and codex workers are verifiably gone (a process
the runner never recorded, such as a tool command codex left running in the
background, is not detected). Every step works
through pinned directory descriptors and never follows a link. Saved-thread
state and role locks beside runs/ are never read or touched here.
"""

import fcntl
import os
import re
import secrets
import stat
import time

from council_common import (
    SUPERVISOR_FILENAME,
    SUPERVISOR_LOCK_FILENAME,
    _print_stdout,
    _private_stat_problem,
    _report_inline,
)
from council_liveness import (
    STATUS_FILENAME,
    TERMINAL_STATES,
    _identity_state,
    _process_table,
    read_lock_token,
    read_status,
    read_supervisor,
)

RUNS_SUBDIR = "runs"
RUN_NAME = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}\Z")
DEFAULT_PRUNE_DAYS = 30
PLAN_FILENAME = "run.json"  # council_workflow.PLAN_FILE
# What a launched run must hold to be recognised; removed last, so a run
# whose removal stopped partway is still recognised on the next prune.
CONTROL_RECORDS = (PLAN_FILENAME, SUPERVISOR_LOCK_FILENAME,
                   SUPERVISOR_FILENAME, STATUS_FILENAME)
_DIR_FLAGS = (os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
              | os.O_CLOEXEC)
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
# Recovery for an unusable state or runs directory: never repaired here.
_MOVE_ASIDE = ("it is not changed. Move it aside or point XDG_STATE_HOME at "
               "an owned private directory")
# A state directory whose only problem is its mode (it holds saved threads).
_STATE_MODE = ("it is not changed. It also holds saved threads and role "
               "locks, which moving it aside would discard: chmod 700 it (as "
               "a council launch would) or point XDG_STATE_HOME at an owned "
               "private directory")


class RunsError(Exception):
    """A refused allocation or an unusable runs root (exit 2)."""


class PruneError(Exception):
    """One run's removal stopped; what remains is left in place."""


# Descriptor-relative open/stat/mkdir/unlink/rmdir and listing by fd;
# without them nothing is allocated or deleted (fail closed).
_FD_OPS = ({os.open, os.stat, os.unlink, os.rmdir, os.mkdir}
           <= os.supports_dir_fd and os.listdir in os.supports_fd
           and os.stat in os.supports_follow_symlinks)


def _private_dir_fd(name, dir_fd, label, shown, *, absent_ok=False):
    """Open `name` (relative to dir_fd, or as spelled when dir_fd is None)
    as an owned private real directory without following a final link.
    None when absent and `absent_ok`; RunsError (naming `shown`) otherwise."""
    try:
        seen = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        if absent_ok:
            return None
        raise RunsError(f"{label} {shown!r} does not exist") from None
    except OSError as e:
        raise RunsError(f"cannot inspect {label} {shown!r} "
                        f"({e.strerror or e})") from None
    problem = _private_stat_problem(seen, directory=True)
    if problem is not None:
        advice = (_STATE_MODE if (label, problem[0]) == ("state directory", "mode")
                  else _MOVE_ASIDE)
        raise RunsError(f"{label} {shown!r} {problem[1]}; {advice}")
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    except OSError as e:
        raise RunsError(f"cannot open {label} {shown!r} "
                        f"({e.strerror or e})") from None
    opened = os.fstat(fd)
    if ((opened.st_dev, opened.st_ino) != (seen.st_dev, seen.st_ino)
            or _private_stat_problem(opened, directory=True) is not None):
        os.close(fd)
        raise RunsError(f"{label} {shown!r} changed while being checked; "
                        "it is not used")
    return fd


def _make_missing(path):
    """Create the missing components of `path` 0700 (the caller's umask is
    0o077); existing ancestors are never touched."""
    missing = []
    current = path
    while not os.path.lexists(current):
        missing.append(current)
        current = os.path.dirname(current)
    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            continue  # a concurrent caller made it; checked when opened


def new_run(state_dir):
    """Allocate a fresh private run directory and return its real path.

    `state_dir` is used as spelled (a "link/.." component resolves where
    the I/O does); missing components are created 0700 whatever the umask.
    The state directory, runs/ and the new run must each be a real
    directory owned by the effective uid with no group or other bits;
    anything else raises RunsError and nothing is chmodded.
    """
    if not os.path.isabs(state_dir):
        raise RunsError(
            f"no absolute state directory ({state_dir!r}): $HOME is unset, "
            "empty or relative and the account has no home directory. Set "
            "XDG_STATE_HOME to an absolute private directory, then run "
            "--new-run again")
    if not _FD_OPS:
        raise RunsError("descriptor-relative directory operations are "
                        "unavailable on this platform")
    previous = os.umask(0o077)
    try:
        _make_missing(state_dir)
        runs_path = os.path.join(state_dir, RUNS_SUBDIR)
        state_fd = _private_dir_fd(state_dir, None, "state directory",
                                   state_dir)
        try:
            try:
                os.mkdir(RUNS_SUBDIR, 0o700, dir_fd=state_fd)
            except FileExistsError:
                pass
            runs_fd = _private_dir_fd(RUNS_SUBDIR, state_fd, "runs directory",
                                      runs_path)
        finally:
            os.close(state_fd)
        try:
            return _allocate(runs_fd, runs_path)
        finally:
            os.close(runs_fd)
    finally:
        os.umask(previous)


def _allocate(runs_fd, runs_path):
    for _ in range(8):
        name = (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-"
                + secrets.token_hex(8))
        try:
            os.mkdir(name, 0o700, dir_fd=runs_fd)
        except FileExistsError:
            continue
        break
    else:
        raise RunsError("could not allocate a unique run directory name")
    run_fd = _private_dir_fd(name, runs_fd, "new run directory",
                             os.path.join(runs_path, name))
    try:
        opened = os.fstat(run_fd)
        # The printed path is the physical one, so later commands (which
        # normalize lexically) reach this same directory.
        path = os.path.realpath(os.path.join(runs_path, name))
        try:
            named = os.lstat(path)
        except OSError:
            named = None
        if (stat.S_IMODE(opened.st_mode) != 0o700 or named is None
                or (named.st_dev, named.st_ino)
                != (opened.st_dev, opened.st_ino)
                or not path.isprintable()):
            os.rmdir(name, dir_fd=runs_fd)
            raise RunsError(f"the new run directory {path!r} cannot be "
                            "named reliably; it was removed")
        return path
    finally:
        os.close(run_fd)


# ---------- --prune-runs ----------

def _lstat_at(dir_fd, name):
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _structure_problem(run_fd):
    """Why this directory is not a launched run's directory, or None."""
    present = {name: _lstat_at(run_fd, name)
               for name in ("prepare.lock", *CONTROL_RECORDS)}
    if present[PLAN_FILENAME] is None:
        if present["prepare.lock"] is not None:
            return "partially prepared (the capture did not finish)"
        if not os.listdir(run_fd):
            return "allocated but never used (empty)"
        return "not prepared (no run.json)"
    if (present[SUPERVISOR_LOCK_FILENAME] is None
            and present[SUPERVISOR_FILENAME] is None):
        return "prepared but never started"
    for name in CONTROL_RECORDS:
        st = present[name]
        if st is None:
            return f"never dispatched or incomplete (no {name})"
        if _private_stat_problem(st, directory=False) is not None:
            return f"{name} is not a private regular file"
    return None


def _take_lock(run_fd, supervisor):
    """(held descriptor, None) or (None, why not). The lock must be the
    recorded file (device, inode and token) and is taken exclusively
    without blocking; the caller holds it through removal."""
    try:
        fd = os.open(SUPERVISOR_LOCK_FILENAME, _FILE_FLAGS, dir_fd=run_fd)
    except OSError as e:
        return None, (f"{SUPERVISOR_LOCK_FILENAME} cannot be opened without "
                      f"following a link ({e.strerror or e})")
    try:
        st = os.fstat(fd)
        token = read_lock_token(fd)
        if _private_stat_problem(st, directory=False) is not None:
            why = f"{SUPERVISOR_LOCK_FILENAME} is not a private regular file"
        elif (supervisor.lock_dev, supervisor.lock_ino) != (st.st_dev,
                                                            st.st_ino):
            why = (f"{SUPERVISOR_LOCK_FILENAME} is not the file "
                   f"{SUPERVISOR_FILENAME} records (replaced)")
        elif supervisor.lock_token is None or token != supervisor.lock_token:
            why = (f"{SUPERVISOR_LOCK_FILENAME} token does not match "
                   f"{SUPERVISOR_FILENAME}")
        else:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd, None
            except BlockingIOError:
                why = ("the supervisor lock is held (the runner or a reader "
                       "is using this run)")
            except OSError as e:
                why = f"cannot lock {SUPERVISOR_LOCK_FILENAME} ({e.strerror or e})"
    except BaseException:
        os.close(fd)
        raise
    os.close(fd)
    return None, why


def _finished_problem(view, supervisor, days, now):
    """(why not, None) or (None, age in seconds) from status.json."""
    if view is None:
        return f"no usable {STATUS_FILENAME}", None
    if view.state not in TERMINAL_STATES:
        return (f"{STATUS_FILENAME} says {view.state}: no terminal state was "
                "recorded (still running or interrupted)"), None
    if view.tick_at is None:
        return f"{STATUS_FILENAME} records no terminal time", None
    if view.pid is None or view.pid != supervisor.pid or (
            view.identity is not None and view.identity != supervisor.identity):
        return (f"{STATUS_FILENAME} and {SUPERVISOR_FILENAME} name different "
                "runners"), None
    age = now - view.tick_at
    if age < 0:
        return "its terminal time is in the future", None
    if age <= days * 86400:
        return (f"{view.state} {age / 86400:.1f} days ago, not older than "
                f"{days} days"), None
    return None, age


def _live_members(table, pgid):
    return [pid for pid, (group, state, *_) in table.items()
            if group == pgid and not state.startswith("Z")]


def _process_problem(view, supervisor):
    """Why the recorded runner or a recorded codex process may be alive, or
    None. Inconclusive process information is a reason too."""
    table = _process_table()
    if not table:
        return "the process table cannot be read (liveness unknown)"
    if None in (supervisor.pid, supervisor.identity, supervisor.pgid):
        return f"{SUPERVISOR_FILENAME} names no verifiable runner"
    if not view.roles or not view.roles_intact:  # the runner records >= 1
        return f"{STATUS_FILENAME} has missing or unreadable role records"
    if _identity_state(table, supervisor.pid, supervisor.identity) != "gone":
        return f"the recorded supervisor (pid {supervisor.pid}) is still present"
    if _live_members(table, supervisor.pgid):
        return (f"the supervisor's process group {supervisor.pgid} still has "
                "live processes")
    for role_id, role in view.roles.items():
        label = _report_inline(role_id)
        if role["state"] == "unknown":
            return f"role {label} has an unreadable {STATUS_FILENAME} record"
        if role["pgid"] is not None and _live_members(table, role["pgid"]):
            return (f"codex process group {role['pgid']} ({label}) still has "
                    "live processes")
        entry = table.get(role["pid"]) if role["pid"] is not None else None
        if entry is not None and not entry[1].startswith("Z") and (
                role["start_identity"] is None
                or entry[2] == role["start_identity"]):
            return f"codex pid {role['pid']} ({label}) is still present"
    return None


def _open_subdir(parent_fd, name, seen):
    """A verified descriptor on subdirectory `name`, owner rwx restored
    through it (never by name, never on a file)."""
    fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    try:
        opened = os.fstat(fd)
        if ((opened.st_dev, opened.st_ino) != (seen.st_dev, seen.st_ino)
                or opened.st_uid != os.geteuid()):
            raise PruneError(f"{name!r} changed or is not owned by this user")
        if stat.S_IMODE(opened.st_mode) & 0o700 != 0o700:
            os.fchmod(fd, 0o700)  # frozen baselines are 0500
        return fd
    except BaseException:
        os.close(fd)
        raise


def _listing(fd):
    try:
        return os.listdir(fd)
    except BaseException:
        os.close(fd)
        raise


def _rmdir_same(parent_fd, name, seen):
    now = _lstat_at(parent_fd, name)
    if now is None or (now.st_dev, now.st_ino) != (seen.st_dev, seen.st_ino):
        raise PruneError(f"{name!r} changed while being removed")
    os.rmdir(name, dir_fd=parent_fd)


def _remove_run(runs_fd, name, run_fd, run_st):
    """Remove a verified run directory through descriptors. Symlinks and
    special files are unlinked unopened, regular files are never chmodded,
    and nothing on another filesystem is entered; the first problem raises
    and leaves the rest in place."""
    dev = run_st.st_dev
    if stat.S_IMODE(run_st.st_mode) != 0o700:
        os.fchmod(run_fd, 0o700)
    names = os.listdir(run_fd)
    entries = [n for n in CONTROL_RECORDS if n in names]
    entries += [n for n in names if n not in CONTROL_RECORDS]
    frames = [(run_fd, entries, name, run_st)]
    try:
        while frames:
            fd, pending, dname, dst = frames[-1]
            if pending:
                entry = pending.pop()
                seen = _lstat_at(fd, entry)
                if seen is None:
                    continue
                if seen.st_dev != dev:
                    raise PruneError(f"{entry!r} is on another filesystem; "
                                     "not entered")
                if stat.S_ISDIR(seen.st_mode):
                    child = _open_subdir(fd, entry, seen)
                    frames.append((child, _listing(child), entry, seen))
                else:
                    os.unlink(entry, dir_fd=fd)
                continue
            frames.pop()
            if frames:
                os.close(fd)
            _rmdir_same(frames[-1][0] if frames else runs_fd, dname, dst)
    finally:
        for frame in frames[1:]:
            os.close(frame[0])


def _consider(runs_fd, name, root_dev, days, delete, now):
    """(kind, detail) for one entry of the runs root: kind is ignored,
    skip, eligible, removed or failed."""
    if not RUN_NAME.fullmatch(name):
        return "ignored", "not a --new-run directory name"
    seen = _lstat_at(runs_fd, name)
    if seen is None:
        return "skip", "vanished"
    problem = _private_stat_problem(seen, directory=True)
    if problem is not None:
        tail = " (not followed)" if problem[0] == "symlink" else ""
        return "skip", f"{problem[1]}{tail}"
    if seen.st_dev != root_dev:
        return "skip", "is on another filesystem"
    try:
        run_fd = os.open(name, _DIR_FLAGS, dir_fd=runs_fd)
    except OSError as e:
        return "skip", f"cannot be opened ({e.strerror or e})"
    lock_fd = None
    try:
        opened = os.fstat(run_fd)
        if (opened.st_dev, opened.st_ino) != (seen.st_dev, seen.st_ino):
            return "skip", "changed while being checked"
        why = _structure_problem(run_fd)
        if why is not None:
            return "skip", why
        supervisor = read_supervisor(SUPERVISOR_FILENAME, dir_fd=run_fd)
        if supervisor is None:
            return "skip", f"no usable {SUPERVISOR_FILENAME}"
        lock_fd, why = _take_lock(run_fd, supervisor)
        if why is not None:
            return "skip", why
        # Validated with the lock held, and held through removal.
        view = read_status(STATUS_FILENAME, dir_fd=run_fd)
        why, age = _finished_problem(view, supervisor, days, now)
        if why is None:
            why = _process_problem(view, supervisor)
        if why is not None:
            return "skip", why
        detail = f"{view.state} {age / 86400:.1f} days ago"
        if not delete:
            return "eligible", detail
        try:
            _remove_run(runs_fd, name, run_fd, opened)
        except (OSError, PruneError) as e:
            return "failed", (f"{_report_inline(e)}; removal stopped and "
                              "what remains is left in place")
        return "removed", detail
    except OSError as e:
        return "skip", f"cannot be checked ({e.strerror or e})"
    finally:
        os.close(run_fd)
        if lock_fd is not None:
            os.close(lock_fd)


def prune_runs(state_dir, days, delete):
    """--prune-runs: list (or with `delete` remove) finished runs older than
    `days`; 0 when done, 1 when a removal failed partway. An unusable state
    or runs directory raises RunsError (exit 2); an absent one has nothing
    to prune. Never creates anything, cancels, reaps or follows a link."""
    if not os.path.isabs(state_dir):
        raise RunsError(f"no absolute state directory ({state_dir!r})")
    if not _FD_OPS:
        raise RunsError("descriptor-relative directory operations are "
                        "unavailable on this platform; nothing was deleted")
    root = os.path.join(state_dir, RUNS_SUBDIR)
    state_fd = _private_dir_fd(state_dir, None, "state directory", state_dir,
                               absent_ok=True)
    runs_fd = None
    if state_fd is not None:
        try:
            runs_fd = _private_dir_fd(RUNS_SUBDIR, state_fd, "runs directory",
                                      root, absent_ok=True)
        finally:
            os.close(state_fd)
    if runs_fd is None:
        _print_stdout(f"[codex-council] prune: no runs directory at "
                      f"{_report_inline(root)}; nothing to prune")
        return 0
    counts = {}
    try:
        shown = os.path.realpath(root)
        root_dev = os.fstat(runs_fd).st_dev
        now = time.time()
        for name in sorted(os.listdir(runs_fd)):
            try:
                kind, detail = _consider(runs_fd, name, root_dev, days,
                                         delete, now)
            except OSError as e:  # never "removed" after an error
                kind = "failed" if delete else "skip"
                detail = f"cannot be checked or removed ({_report_inline(e)})"
            counts[kind] = counts.get(kind, 0) + 1
            _print_stdout(f"{kind} {_report_inline(os.path.join(shown, name))}"
                          f": {detail}")
    finally:
        os.close(runs_fd)
    summary = ", ".join(f"{counts.get(kind, 0)} {kind}" for kind in (
        ("removed", "failed") if delete else ("eligible",)))
    summary += f", {counts.get('skip', 0)} skipped"
    if counts.get("ignored"):
        summary += f", {counts['ignored']} ignored"
    tail = "" if delete else "; dry run: pass --delete to remove the eligible runs"
    _print_stdout(f"[codex-council] prune: older than {days} days: "
                  f"{summary}{tail}")
    return 1 if counts.get("failed") else 0
