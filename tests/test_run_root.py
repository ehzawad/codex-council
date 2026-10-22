"""The persistent run root: --new-run allocation and --prune-runs.

Everything lives under temporary directories (an isolated state home); the
real ~/.local/state is never read or touched. Unit tests drive
council_runs directly; the CLI tests run the real runner, and one end-to-end
test launches a council against the fake codex and prunes it.
"""

import contextlib
import io
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
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import council_testlib  # noqa: E402
from council_testlib import EPOCH, SCRIPT  # noqa: E402
import codex_council  # noqa: E402
import council_common  # noqa: E402
import council_liveness  # noqa: E402
import council_policy  # noqa: E402
import council_runs  # noqa: E402
import council_workspace  # noqa: E402

setUpModule = council_testlib.install_fake_codex
tearDownModule = council_testlib.remove_fake_codex

DAY = 86400


def _mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def _rmtree_any(path):
    """Remove a test tree whatever its directory modes (frozen baselines,
    a mode-000 directory), never following a link."""
    if not os.path.lexists(path):
        return
    for root, dirs, _ in os.walk(path):
        for name in dirs:
            full = os.path.join(root, name)
            if not os.path.islink(full):
                os.chmod(full, 0o700)
    if not os.path.islink(path):
        os.chmod(path, 0o700)
    shutil.rmtree(path, ignore_errors=True)


def _tree(path):
    """{relative path: (mode, inode, size, mtime_ns)} of everything under
    path, links not followed: what a dry run must leave unchanged."""
    found = {}
    for root, dirs, files in os.walk(path):
        for name in (*dirs, *files):
            full = os.path.join(root, name)
            st = os.lstat(full)
            found[os.path.relpath(full, path)] = (
                st.st_mode, st.st_ino, st.st_size, st.st_mtime_ns)
    return found


def _dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _other_device(st):
    """`st` as if it were on another filesystem."""
    fields = list(st[:10])
    fields[2] += 1  # st_dev
    return os.stat_result(fields)


def _hook(real, name, action, *, before=False):
    """Wrap council_runs helper `real` (dir_fd, name, ...) so `action(dir_fd)`
    runs after (or before) its calls for `name` until it returns True: a
    swap in the window between a check and the step that follows it."""
    done = []

    def wrapper(dir_fd, entry, *rest):
        hit = entry == name and not done
        if hit and before and action(dir_fd):
            done.append(True)
        result = real(dir_fd, entry, *rest)
        if hit and not before and action(dir_fd):
            done.append(True)
        return result

    return wrapper


# Holds the exclusive lock on argv[1] until killed.
_LOCK_HOLDER = (
    "import fcntl, os, sys, time\n"
    "fd = os.open(sys.argv[1], os.O_RDONLY)\n"
    "fcntl.flock(fd, fcntl.LOCK_EX)\n"
    "print('locked', flush=True)\n"
    "time.sleep(120)\n"
)


class RunRootCase(unittest.TestCase):
    def setUp(self):
        self.base = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(_rmtree_any, self.base)
        # A heavily loaded host can push `ps -A` past its 2 s bound, which
        # (correctly) reads as liveness unknown and skips the run.
        ps_bound = patch.object(council_liveness, "PS_TIMEOUT_SECS", 30)
        ps_bound.start()
        self.addCleanup(ps_bound.stop)
        self.state = os.path.join(self.base, "state", "codex-council")
        self.runs = os.path.join(self.state, "runs")

    def make_runs(self):
        """An existing private state directory and runs/ (makedirs alone
        would leave the intermediate state directory at the umask's mode)."""
        os.makedirs(self.state, 0o700)
        os.mkdir(self.runs, 0o700)

    def new_run(self, state=None):
        return council_runs.new_run(state or self.state)

    def prune(self, days=30, delete=False, state=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = council_runs.prune_runs(state or self.state, days, delete)
        return code, out.getvalue().splitlines()

    def line_for(self, lines, path):
        matches = [line for line in lines if f" {path}: " in line]
        self.assertEqual(len(matches), 1, lines)
        return matches[0]

    def sleeper(self, seconds=60):
        """A sleeping process leading its own group: (pid, identity)."""
        proc = subprocess.Popen(["sleep", str(seconds)], start_new_session=True)

        def cleanup():
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()

        self.addCleanup(cleanup)
        return proc.pid, council_liveness.process_start_identity(proc.pid)

    def write_private(self, path, data):
        council_common._atomic_write_private(path, data)

    def finished_run(self, *, state="done", age_days=40.0, pid=None,
                     identity="Thu Jan  1 00:00:00 2026", roles=None,
                     run_dir=None):
        """A launched run as the runner leaves it: inputs, capture records,
        a frozen baseline, a role workspace, replies, the supervisor files
        and a terminal status.json `age_days` old."""
        run_dir = run_dir or self.new_run()
        for name, mode in (("request.json", 0o644), ("roles.json", 0o644),
                           ("context.md", 0o644)):
            with open(os.path.join(run_dir, name), "w", encoding="utf-8") as f:
                f.write("{}\n")
            os.chmod(os.path.join(run_dir, name), mode)
        for name in ("prepare.lock", "execution.claim", "out.md", "err.log"):
            os.close(os.open(os.path.join(run_dir, name),
                             os.O_CREAT | os.O_WRONLY, 0o600))
        self.write_private(os.path.join(run_dir, "run.json"), b"{}\n")
        baseline = os.path.join(run_dir, "baseline")
        os.makedirs(os.path.join(baseline, "src", "deep"), 0o700)
        os.mkdir(os.path.join(baseline, "empty"), 0o700)
        for name, mode in (("src/a.txt", 0o644), ("src/deep/tool.sh", 0o755)):
            with open(os.path.join(baseline, name), "w", encoding="utf-8") as f:
                f.write("captured\n")
            os.chmod(os.path.join(baseline, name), mode)
        council_workspace.freeze(Path(baseline))
        for sub in ("workspaces/architect/src", "evidence/architect",
                    "catalogs/architect", "replies"):
            os.makedirs(os.path.join(run_dir, sub), 0o700)
        with open(os.path.join(run_dir, "replies", "architect.md"), "w",
                  encoding="utf-8") as f:
            f.write("reply\n")
        lock = os.path.join(run_dir, "supervisor.lock")
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        token = os.urandom(16).hex()
        os.write(fd, token.encode("ascii"))
        os.close(fd)
        pid = _dead_pid() if pid is None else pid
        self.write_supervisor(run_dir, pid, identity, token=token)
        self.write_status(run_dir, pid, identity, state=state,
                          age_days=age_days, roles=roles)
        return run_dir

    def write_supervisor(self, run_dir, pid, identity, token=None, lock=None):
        st = os.stat(os.path.join(run_dir, "supervisor.lock"))
        if token is None:
            with open(os.path.join(run_dir, "supervisor.lock"), "rb") as f:
                token = f.read().decode("ascii")
        record = {"schema": 1, "pid": pid, "start_identity": identity,
                  "pgid": pid, "sid": pid,
                  "lock": lock or {"dev": st.st_dev, "ino": st.st_ino,
                                   "token": token},
                  "version": "9.9.9", "epoch": codex_council.SKILL_CONTRACT_EPOCH,
                  "started_at": "2026-01-01T00:00:00Z"}
        self.write_private(os.path.join(run_dir, "supervisor.json"),
                           json.dumps(record).encode())

    def write_status(self, run_dir, pid, identity, *, state="done",
                     age_days=40.0, roles=None, at=None):
        data = {"schema": 1, "run_id": "0123456789abcdef",
                "runner": {"pid": pid, "start_identity": identity,
                           "state": state, "exit": 0, "mode": "detached"},
                "tick": {"seq": 9, "at": time.time() - age_days * DAY
                         if at is None else at},
                "roles": roles or {"architect": {"state": "settled",
                                                 "attempt": 1,
                                                 "outcome": "ok"}}}
        self.write_private(os.path.join(run_dir, "status.json"),
                           json.dumps(data).encode())


# ---------- --new-run ----------

class NewRunTests(RunRootCase):
    def test_new_run_is_private_whatever_the_umask(self):
        for mask in (0o022, 0o077, 0o200):
            with self.subTest(umask=f"{mask:03o}"):
                xdg = os.path.join(self.base, f"xdg-{mask:03o}", "state")
                previous = os.umask(mask)
                try:
                    path = self.new_run(os.path.join(xdg, "codex-council"))
                    self.assertEqual(os.umask(mask), mask)  # restored
                finally:
                    os.umask(previous)
                runs = os.path.join(xdg, "codex-council", "runs")
                self.assertEqual(os.path.dirname(path), runs)
                self.assertTrue(council_runs.RUN_NAME.fullmatch(
                    os.path.basename(path)))
                self.assertEqual(os.listdir(path), [])
                # Missing state-home components are created 0700 too.
                for created in (os.path.dirname(xdg), xdg,
                                os.path.dirname(runs), runs, path):
                    self.assertEqual(_mode(created), 0o700, created)

    def test_each_call_allocates_a_new_directory_and_leaves_ancestors(self):
        parent = os.path.join(self.base, "state")
        os.mkdir(parent)
        os.chmod(parent, 0o755)
        first, second = self.new_run(), self.new_run()
        self.assertNotEqual(first, second)
        self.assertEqual(sorted(os.listdir(self.runs)),
                         sorted(map(os.path.basename, (first, second))))
        self.assertEqual(_mode(parent), 0o755)
        stamp = os.path.basename(first).split("-")[0]
        self.assertLess(abs(time.mktime(time.strptime(stamp, "%Y%m%dT%H%M%SZ"))
                            - time.mktime(time.gmtime())), 120)

    def test_saved_threads_and_role_locks_are_untouched(self):
        os.makedirs(self.state, 0o700)
        saved = os.path.join(self.state, "proj-abc__architect.json")
        with open(saved, "w", encoding="utf-8") as f:
            f.write('{"session_id": "s"}')
        os.chmod(saved, 0o600)
        lock = saved + ".lock"
        os.close(os.open(lock, os.O_CREAT | os.O_WRONLY, 0o600))
        before = _tree(self.state)
        self.new_run()
        after = _tree(self.state)
        for name in (os.path.basename(saved), os.path.basename(lock)):
            self.assertEqual(after[name], before[name])

    def test_a_symlinked_runs_directory_is_refused(self):
        os.makedirs(self.state, 0o700)
        target = os.path.join(self.base, "elsewhere")
        os.mkdir(target, 0o700)
        os.symlink(target, self.runs)
        with self.assertRaisesRegex(council_runs.RunsError, "is a symlink"):
            self.new_run()
        self.assertEqual(os.listdir(target), [])
        self.assertTrue(os.path.islink(self.runs))

    def test_a_public_runs_directory_is_refused_not_repaired(self):
        self.make_runs()
        os.chmod(self.runs, 0o755)
        with self.assertRaises(council_runs.RunsError) as caught:
            self.new_run()
        self.assertEqual(str(caught.exception), (
            f"runs directory {self.runs!r} is mode 0755; it is not changed. "
            "Move it aside or point XDG_STATE_HOME at an owned private "
            "directory"))
        self.assertEqual(_mode(self.runs), 0o755)
        self.assertEqual(os.listdir(self.runs), [])

    def test_a_public_state_directory_is_refused_not_repaired(self):
        """Its saved threads would go with it, so the advice is never to
        move a state directory aside only because of its mode."""
        os.makedirs(self.state)
        os.chmod(self.state, 0o755)
        with self.assertRaises(council_runs.RunsError) as caught:
            self.new_run()
        self.assertEqual(str(caught.exception), (
            f"state directory {self.state!r} is mode 0755; it is not changed. "
            "It also holds saved threads and role locks, which moving it "
            "aside would discard: chmod 700 it (as a council launch would) "
            "or point XDG_STATE_HOME at an owned private directory"))
        self.assertEqual(_mode(self.state), 0o755)
        self.assertEqual(os.listdir(self.state), [])
        os.chmod(self.state, 0o700)
        os.rename(self.state, self.state + ".real")
        os.symlink(self.state + ".real", self.state)
        with self.assertRaisesRegex(council_runs.RunsError,
                                    "is a symlink; it is not changed. Move it aside"):
            self.new_run()

    def test_a_foreign_runs_directory_is_refused(self):
        self.make_runs()
        runs = os.stat(self.runs)
        real_stat, real_fstat = os.stat, os.fstat

        def foreign(st):
            if (st.st_dev, st.st_ino) != (runs.st_dev, runs.st_ino):
                return st
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev,
                                   st.st_nlink, st.st_uid + 1, st.st_gid,
                                   st.st_size, int(st.st_atime),
                                   int(st.st_mtime), int(st.st_ctime)))

        with patch.object(os, "stat", lambda *a, **k: foreign(real_stat(*a, **k))), \
                patch.object(os, "fstat", lambda fd: foreign(real_fstat(fd))), \
                self.assertRaisesRegex(council_runs.RunsError,
                                       f"is owned by uid {os.geteuid() + 1}"):
            self.new_run()
        self.assertEqual(os.listdir(self.runs), [])

    def test_a_link_dotdot_state_spelling_is_used_where_it_resolves(self):
        """The state directory is never lexically normalized: "link/.."
        resolves physically, and the printed path names that directory."""
        real = os.path.join(self.base, "real")
        os.makedirs(os.path.join(real, "inner"), 0o700)
        os.symlink(os.path.join(real, "inner"), os.path.join(self.base, "link"))
        spelled = os.path.join(self.base, "link", "..", "codex-council")
        path = self.new_run(spelled)
        self.assertEqual(os.path.dirname(path),
                         os.path.join(real, "codex-council", "runs"))
        self.assertTrue(os.path.isdir(path))
        self.assertFalse(os.path.lexists(os.path.join(self.base, "codex-council")))

    def test_a_relative_state_directory_is_refused(self):
        with self.assertRaisesRegex(council_runs.RunsError,
                                    "no absolute state directory"):
            self.new_run(os.path.join(".local", "state", "codex-council"))
        with self.assertRaisesRegex(council_runs.RunsError,
                                    "no absolute state directory"):
            council_runs.prune_runs("relative", 30, False)


# ---------- the parser ----------

class ParserTests(unittest.TestCase):
    def usage_exit(self, argv, expect):
        council_testlib.assert_usage_exit(
            self, lambda: codex_council._parse_args(argv),
            expect_in_stderr=expect)

    def test_new_modes_parse_with_the_skill_contract(self):
        args = codex_council._parse_args(["--new-run", "--skill-contract", EPOCH])
        self.assertTrue(args.new_run)
        args = codex_council._parse_args(
            ["--prune-runs", "--older-than", "0", "--delete",
             "--skill-contract", EPOCH])
        self.assertEqual((args.prune_runs, args.older_than, args.delete),
                         (True, 0, True))
        args = codex_council._parse_args(["--prune-runs"])
        self.assertIsNone(args.older_than)
        self.assertIsNone(args.delete)

    def test_new_modes_exclude_every_other_mode(self):
        others = (["--prepare", "/x"], ["--start", "/x"], ["--status", "/x"],
                  ["--check-staging-dir", "/x"], ["--roles-file", "r"],
                  ["--context-file", "c"], ["--freshness", "/x"])
        for mode in ("--new-run", "--prune-runs"):
            for other in (*others, ["--prune-runs" if mode == "--new-run"
                                    else "--new-run"]):
                with self.subTest(mode=mode, other=other[0]):
                    self.usage_exit([mode, *other], "cannot be combined with")

    def test_prune_options_require_prune_runs(self):
        for argv in (["--delete"], ["--older-than", "3"],
                     ["--new-run", "--delete"], ["--start", "/x", "--older-than", "3"],
                     ["--roles-file", "/d/roles.json", "--context-file",
                      "/d/context.md", "--supervisor-lock-fd", "5", "--delete"]):
            with self.subTest(argv=argv):
                self.usage_exit(argv, "requires --prune-runs")
        self.usage_exit(["--prune-runs", "--supervisor-lock-fd", "5"],
                        "--supervisor-lock-fd")

    def test_older_than_takes_whole_days_only(self):
        for value in ("-1", "1.5", "abc", "", "1e3", "+3", " 3", "3 ",
                      "1000000000", "1234567890"):
            with self.subTest(value=value):
                self.usage_exit(["--prune-runs", "--older-than", value],
                                "--older-than must be a whole number of days "
                                f"from 0 to 999999999; got {value!r}")
        for value in ("45", "0", "999999999"):
            self.assertEqual(codex_council._parse_args(
                ["--prune-runs", f"--older-than={value}"]).older_than, int(value))

    def test_prune_help_states_its_bound_and_what_it_cannot_see(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            codex_council._parse_args(["--help"])
        text = " ".join(out.getvalue().split())
        self.assertIn("older than DAYS whole days, 0 to 999999999 (default 30)",
                      text)
        self.assertIn("A process the runner never recorded (such as a tool "
                      "command codex left running in the background) is not "
                      "detected.", text)


# ---------- --prune-runs ----------

class PruneTests(RunRootCase):
    def test_a_dry_run_lists_and_changes_nothing(self):
        eligible = self.finished_run()
        recent = self.finished_run(age_days=10)
        empty = self.new_run()
        partial = self.new_run()
        os.close(os.open(os.path.join(partial, "prepare.lock"),
                         os.O_CREAT | os.O_WRONLY, 0o600))
        before = _tree(self.base)
        code, lines = self.prune()
        self.assertEqual(code, 0, lines)
        self.assertEqual(_tree(self.base), before)
        self.assertEqual(self.line_for(lines, eligible),
                         f"eligible {eligible}: done 40.0 days ago")
        self.assertIn("done 10.0 days ago, not older than 30 days",
                      self.line_for(lines, recent))
        self.assertIn("allocated but never used", self.line_for(lines, empty))
        self.assertIn("partially prepared", self.line_for(lines, partial))
        self.assertEqual(lines[-1], "[codex-council] prune: older than 30 days: "
                         "1 eligible, 3 skipped; dry run: pass --delete to "
                         "remove the eligible runs")

    def test_delete_removes_a_finished_run_with_its_frozen_baseline(self):
        run = self.finished_run()
        self.assertEqual(_mode(os.path.join(run, "baseline", "src")), 0o500)
        self.assertEqual(_mode(os.path.join(run, "baseline", "src", "a.txt")), 0o400)
        code, lines = self.prune(delete=True)
        self.assertEqual(code, 0, lines)
        self.assertFalse(os.path.lexists(run))
        self.assertEqual(lines, [
            f"removed {run}: done 40.0 days ago",
            "[codex-council] prune: older than 30 days: 1 removed, 0 failed, "
            "0 skipped"])
        self.assertEqual(os.listdir(self.runs), [])

    def test_aborted_and_interrupted_terminal_states_are_eligible(self):
        runs = [self.finished_run(state=state) for state in
                ("aborted", "interrupted")]
        code, lines = self.prune(delete=True)
        self.assertEqual(code, 0, lines)
        for run in runs:
            self.assertFalse(os.path.lexists(run))

    def test_age_comes_from_the_terminal_status_not_mtimes(self):
        fresh_files = self.finished_run(age_days=40)
        old_files = self.finished_run(age_days=5)
        ancient = time.time() - 400 * DAY
        for root, dirs, files in os.walk(old_files):
            for name in (*dirs, *files):
                os.utime(os.path.join(root, name), (ancient, ancient),
                         follow_symlinks=False)
        os.utime(old_files, (ancient, ancient))
        _, lines = self.prune()
        self.assertTrue(self.line_for(lines, fresh_files).startswith("eligible "))
        self.assertIn("done 5.0 days ago, not older than 30 days",
                      self.line_for(lines, old_files))
        _, lines = self.prune(days=0)
        self.assertTrue(self.line_for(lines, old_files).startswith("eligible "))

    def test_a_held_supervisor_lock_is_refused(self):
        run = self.finished_run()
        holder = subprocess.Popen(
            [sys.executable, "-c", _LOCK_HOLDER,
             os.path.join(run, "supervisor.lock")], stdout=subprocess.PIPE)

        def cleanup():
            holder.kill()
            holder.wait()
            holder.stdout.close()

        self.addCleanup(cleanup)
        self.assertEqual(holder.stdout.readline(), b"locked\n")
        code, lines = self.prune(delete=True)
        self.assertEqual(code, 0, lines)
        self.assertIn("the supervisor lock is held", self.line_for(lines, run))
        self.assertTrue(os.path.isdir(os.path.join(run, "baseline")))

    def test_a_live_recorded_supervisor_is_refused(self):
        pid, identity = self.sleeper()
        run = self.finished_run(pid=pid, identity=identity)
        code, lines = self.prune(delete=True)
        self.assertEqual(code, 0, lines)
        self.assertIn(f"the recorded supervisor (pid {pid}) is still present",
                      self.line_for(lines, run))
        self.assertTrue(os.path.isfile(os.path.join(run, "status.json")))

    def test_a_live_worker_group_is_refused_even_with_the_runner_gone(self):
        pid, identity = self.sleeper()
        run = self.finished_run(state="interrupted", roles={
            "architect": {"state": "settled", "attempt": 1},
            "critic": {"state": "active", "attempt": 1, "pid": pid,
                       "pgid": pid, "start_identity": identity}})
        code, lines = self.prune(delete=True)
        self.assertEqual(code, 0, lines)
        self.assertIn(f"codex process group {pid} (critic) still has live "
                      "processes", self.line_for(lines, run))
        self.assertTrue(os.path.isdir(run))

    def test_an_unverifiable_worker_group_is_refused(self):
        """A group whose recorded leader is gone but a member lives is not
        verifiably this run's codex; it still blocks pruning."""
        leader = subprocess.Popen(["sh", "-c", "sleep 60 & echo $!"],
                                  stdout=subprocess.PIPE, start_new_session=True)
        member = int(leader.stdout.readline())
        leader.wait()
        leader.stdout.close()

        def cleanup():
            with contextlib.suppress(OSError):
                os.killpg(leader.pid, signal.SIGKILL)

        self.addCleanup(cleanup)
        self.assertTrue(council_testlib.pid_running(member))
        run = self.finished_run(state="interrupted", roles={
            "critic": {"state": "active", "attempt": 1, "pid": leader.pid,
                       "pgid": leader.pid, "start_identity": "gone"}})
        _, lines = self.prune(delete=True)
        self.assertIn(f"codex process group {leader.pid} (critic) still has "
                      "live processes", self.line_for(lines, run))
        self.assertTrue(os.path.isdir(run))

    def test_unknown_liveness_is_skipped(self):
        run = self.finished_run()
        for table in (None, {}):
            with self.subTest(table=table), \
                    patch.object(council_runs, "_process_table",
                                 return_value=table):
                code, lines = self.prune(delete=True)
            self.assertEqual(code, 0, lines)
            self.assertIn("the process table cannot be read",
                          self.line_for(lines, run))
            self.assertTrue(os.path.isdir(run))

    def test_a_replaced_lock_or_a_token_mismatch_is_refused(self):
        token_run = self.finished_run()
        st = os.stat(os.path.join(token_run, "supervisor.lock"))
        self.write_supervisor(token_run, _dead_pid(), "x", lock={
            "dev": st.st_dev, "ino": st.st_ino, "token": "ab" * 16})
        replaced = self.finished_run()
        lock = os.path.join(replaced, "supervisor.lock")
        with open(lock, "rb") as f:
            token = f.read()
        fresh = lock + ".new"  # created while the old file exists: new inode
        with open(fresh, "wb") as f:
            f.write(token)
        os.chmod(fresh, 0o600)
        os.replace(fresh, lock)
        no_token = self.finished_run()
        st = os.stat(os.path.join(no_token, "supervisor.lock"))
        self.write_supervisor(no_token, _dead_pid(), "x", lock={
            "dev": st.st_dev, "ino": st.st_ino})
        linked = self.finished_run()
        lock = os.path.join(linked, "supervisor.lock")
        os.rename(lock, lock + ".real")
        os.symlink(lock + ".real", lock)
        code, lines = self.prune(delete=True)
        self.assertEqual(code, 0, lines)
        self.assertIn("token does not match", self.line_for(lines, token_run))
        self.assertIn("is not the file supervisor.json records (replaced)",
                      self.line_for(lines, replaced))
        self.assertIn("token does not match", self.line_for(lines, no_token))
        self.assertIn("supervisor.lock is not a private regular file",
                      self.line_for(lines, linked))
        for run in (token_run, replaced, no_token, linked):
            self.assertTrue(os.path.isfile(os.path.join(run, "status.json")))

    def test_links_and_special_files_are_unlinked_and_targets_untouched(self):
        outside = os.path.join(self.base, "outside")
        os.mkdir(outside, 0o755)
        target_file = os.path.join(outside, "keep.txt")
        with open(target_file, "w", encoding="utf-8") as f:
            f.write("keep\n")
        shared = os.path.join(outside, "shared.txt")
        with open(shared, "w", encoding="utf-8") as f:
            f.write("shared\n")
        os.chmod(shared, 0o400)
        run = self.finished_run()
        workspace = os.path.join(run, "workspaces", "architect")
        os.symlink(outside, os.path.join(workspace, "dir-link"))
        os.symlink(target_file, os.path.join(workspace, "file-link"))
        os.symlink(os.path.join(self.base, "missing"),
                   os.path.join(workspace, "dangling"))
        os.mkfifo(os.path.join(workspace, "fifo"), 0o600)
        os.link(shared, os.path.join(workspace, "src", "hardlink.txt"))
        before = _tree(outside)
        code, lines = self.prune(delete=True)
        self.assertEqual(code, 0, lines)
        self.assertFalse(os.path.lexists(run))
        after = _tree(outside)
        self.assertEqual(set(after), set(before))
        for name in after:
            self.assertEqual(after[name][:3], before[name][:3], name)
        self.assertEqual(_mode(shared), 0o400)  # a hardlinked file never chmodded
        self.assertEqual(_mode(outside), 0o755)
        with open(target_file, encoding="utf-8") as f:
            self.assertEqual(f.read(), "keep\n")

    def test_saved_thread_state_and_role_locks_are_preserved(self):
        run = self.finished_run()
        saved = os.path.join(self.state, "proj-abc__architect.json")
        with open(saved, "w", encoding="utf-8") as f:
            f.write('{"session_id": "s"}')
        os.chmod(saved, 0o600)
        os.close(os.open(saved + ".lock", os.O_CREAT | os.O_WRONLY, 0o600))
        before = {name: value for name, value in _tree(self.state).items()
                  if not name.startswith("runs")}
        code, lines = self.prune(days=0, delete=True)
        self.assertEqual(code, 0, lines)
        self.assertFalse(os.path.lexists(run))
        after = {name: value for name, value in _tree(self.state).items()
                 if not name.startswith("runs")}
        self.assertEqual(after, before)
        with open(saved, encoding="utf-8") as f:
            self.assertEqual(f.read(), '{"session_id": "s"}')

    @unittest.skipIf(os.geteuid() == 0, "root opens a mode-000 directory")
    def test_a_partial_failure_exits_1_and_never_claims_removed(self):
        run = self.finished_run()
        locked = os.path.join(run, "workspaces", "architect", "locked")
        os.mkdir(locked, 0o700)
        with open(os.path.join(locked, "f"), "w", encoding="utf-8") as f:
            f.write("x")
        os.chmod(locked, 0o000)
        other = self.finished_run()
        code, lines = self.prune(delete=True)
        self.assertEqual(code, 1, lines)
        line = self.line_for(lines, run)
        self.assertTrue(line.startswith(f"failed {run}: "), line)
        self.assertIn("removal stopped and what remains is left in place", line)
        self.assertFalse(any(item.startswith(f"removed {run}") for item in lines))
        self.assertEqual(self.line_for(lines, other),
                         f"removed {other}: done 40.0 days ago")
        self.assertIn("1 removed, 1 failed", lines[-1])
        # The control records go last, so the run is still recognised and a
        # later prune finishes once the obstacle is gone.
        for name in council_runs.CONTROL_RECORDS:
            self.assertTrue(os.path.isfile(os.path.join(run, name)), name)
        os.chmod(locked, 0o700)
        code, lines = self.prune(delete=True)
        self.assertEqual(code, 0, lines)
        self.assertFalse(os.path.lexists(run))

    def test_non_run_entries_are_ignored_and_links_never_followed(self):
        self.make_runs()
        notes = os.path.join(self.runs, "notes")
        os.mkdir(notes, 0o700)
        with open(os.path.join(notes, "keep.md"), "w", encoding="utf-8") as f:
            f.write("keep")
        readme = os.path.join(self.runs, "README")
        with open(readme, "w", encoding="utf-8") as f:
            f.write("keep")
        # A complete eligible run outside the root, linked under a run name.
        outside = os.path.join(self.base, "outside-run")
        os.mkdir(outside, 0o700)
        self.finished_run(run_dir=outside)
        link = os.path.join(self.runs, "20260101T000000Z-0123456789abcdef")
        os.symlink(outside, link)
        public = os.path.join(self.runs, "20260101T000000Z-fedcba9876543210")
        self.finished_run(run_dir=self.new_run())
        os.rename(self.finished_run(), public)
        os.chmod(public, 0o755)
        code, lines = self.prune(days=0, delete=True)
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.line_for(lines, notes),
                         f"ignored {notes}: not a --new-run directory name")
        self.assertIn("ignored", self.line_for(lines, readme))
        self.assertEqual(self.line_for(lines, link),
                         f"skip {link}: is a symlink (not followed)")
        self.assertEqual(self.line_for(lines, public),
                         f"skip {public}: is mode 0755")
        self.assertTrue(os.path.isfile(os.path.join(notes, "keep.md")))
        self.assertTrue(os.path.isfile(readme))
        self.assertTrue(os.path.islink(link))
        self.assertTrue(os.path.isfile(os.path.join(outside, "status.json")))
        self.assertTrue(os.path.isdir(os.path.join(public, "baseline")))
        self.assertIn("1 removed, 0 failed, 2 skipped, 2 ignored", lines[-1])

    def test_unfinished_or_never_started_runs_are_skipped(self):
        never = self.new_run()
        self.write_private(os.path.join(never, "run.json"), b"{}\n")
        undispatched = self.finished_run()
        os.remove(os.path.join(undispatched, "status.json"))
        running = self.finished_run(state="running")
        foreign = self.finished_run()
        self.write_status(foreign, _dead_pid(), "other runner")
        code, lines = self.prune(days=0, delete=True)
        self.assertEqual(code, 0, lines)
        self.assertIn("prepared but never started", self.line_for(lines, never))
        self.assertIn("no status.json", self.line_for(lines, undispatched))
        self.assertIn("says running: no terminal state was recorded",
                      self.line_for(lines, running))
        self.assertIn("name different runners", self.line_for(lines, foreign))
        for run in (never, undispatched, running, foreign):
            self.assertTrue(os.path.isdir(run))

    def test_an_absent_runs_root_yields_nothing_and_creates_nothing(self):
        code, lines = self.prune(delete=True)
        self.assertEqual(code, 0)
        self.assertEqual(len(lines), 1)
        self.assertIn("nothing to prune", lines[0])
        self.assertFalse(os.path.lexists(os.path.dirname(self.state)))
        os.makedirs(self.state, 0o700)
        self.assertEqual(self.prune()[0], 0)
        self.assertEqual(os.listdir(self.state), [])

    def test_an_unsafe_runs_root_is_refused(self):
        os.makedirs(self.state, 0o700)
        target = os.path.join(self.base, "elsewhere")
        os.mkdir(target, 0o700)
        os.symlink(target, self.runs)
        with self.assertRaisesRegex(council_runs.RunsError, "is a symlink"):
            self.prune(delete=True)

    # ----- liveness the records describe -----

    def test_a_live_worker_pid_without_a_group_is_refused(self):
        pid, identity = self.sleeper()
        run = self.finished_run(state="interrupted", roles={
            "critic": {"state": "active", "attempt": 1, "pid": pid,
                       "start_identity": identity}})
        code, lines = self.prune(delete=True)
        self.assertEqual(code, 0, lines)
        self.assertIn(f"codex pid {pid} (critic) is still present",
                      self.line_for(lines, run))
        self.assertTrue(os.path.isdir(run))

    def test_a_role_in_an_unknown_state_is_refused(self):
        run = self.finished_run(roles={"critic": {"state": "paused",
                                                  "attempt": 1}})
        code, lines = self.prune(delete=True)
        self.assertEqual(code, 0, lines)
        self.assertIn("role critic has an unreadable status.json record",
                      self.line_for(lines, run))
        self.assertTrue(os.path.isdir(run))

    def test_a_live_supervisor_group_is_refused_with_the_supervisor_gone(self):
        leader = subprocess.Popen(["sh", "-c", "sleep 60 & echo $!"],
                                  stdout=subprocess.PIPE, start_new_session=True)
        member = int(leader.stdout.readline())
        leader.wait()
        leader.stdout.close()

        def cleanup():
            with contextlib.suppress(OSError):
                os.killpg(leader.pid, signal.SIGKILL)

        self.addCleanup(cleanup)
        self.assertTrue(council_testlib.pid_running(member))
        run = self.finished_run(pid=leader.pid)  # supervisor pgid = its pid
        code, lines = self.prune(delete=True)
        self.assertEqual(code, 0, lines)
        self.assertIn(f"the supervisor's process group {leader.pid} still has "
                      "live processes", self.line_for(lines, run))
        self.assertTrue(os.path.isdir(run))

    def test_incomplete_control_records_are_skipped(self):
        """Records that parse but cannot name every process to check (a
        damaged or tampered file) make liveness unverifiable."""
        def edit(run, name, change):
            path = os.path.join(run, name)
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            change(data)
            self.write_private(path, json.dumps(data).encode())

        no_pgid = self.finished_run()
        edit(no_pgid, "supervisor.json", lambda d: d.pop("pgid"))
        damaged = {
            "roles is a list": [{"state": "active", "pid": 4242, "pgid": 4242}],
            "a role is not an object": {"architect": {"state": "settled"},
                                        "critic": "active pid 4242"},
            "a pid is unreadable": {"critic": {"state": "active", "pid": "4242",
                                               "pgid": 4242}},
            "a pgid is unreadable": {"critic": {"state": "active", "pid": 4242,
                                                "pgid": 1.5}},
            "no roles": {},
        }
        runs = {}
        for label, roles in damaged.items():
            runs[label] = self.finished_run()
            edit(runs[label], "status.json",
                 lambda d, roles=roles: d.__setitem__("roles", roles))
        code, lines = self.prune(days=0, delete=True)
        self.assertEqual(code, 0, lines)
        self.assertIn("supervisor.json names no verifiable runner",
                      self.line_for(lines, no_pgid))
        for label, run in runs.items():
            with self.subTest(label):
                self.assertIn("status.json has missing or unreadable role records",
                              self.line_for(lines, run))
        for run in (no_pgid, *runs.values()):
            self.assertTrue(os.path.isfile(os.path.join(run, "status.json")))

    # ----- age -----

    def test_age_is_strictly_older_than_days_and_never_in_the_future(self):
        now = float(int(time.time()))
        exact, older, future = (self.finished_run() for _ in range(3))
        for run, at in ((exact, now - DAY), (older, now - DAY - 1),
                        (future, now + DAY)):
            with open(os.path.join(run, "supervisor.json"), encoding="utf-8") as f:
                record = json.load(f)
            self.write_status(run, record["pid"], record["start_identity"], at=at)
        with patch.object(council_runs, "time", types.SimpleNamespace(time=lambda: now)):
            code, lines = self.prune(days=1)
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.line_for(lines, exact),
                         f"skip {exact}: done 1.0 days ago, not older than 1 days")
        self.assertEqual(self.line_for(lines, older),
                         f"eligible {older}: done 1.0 days ago")
        self.assertEqual(self.line_for(lines, future),
                         f"skip {future}: its terminal time is in the future")

    # ----- swaps and other filesystems -----

    def test_a_run_swapped_after_its_check_is_never_entered(self):
        """Between the lstat and the open a run is replaced by a link (the
        open does not follow it) or by another complete run (its inode
        differs). Neither replacement is touched."""
        linked, swapped = self.finished_run(), self.finished_run()
        target = os.path.join(self.base, "target-run")
        os.mkdir(target, 0o700)
        self.finished_run(run_dir=target)
        intruder = os.path.join(self.base, "intruder-run")
        os.mkdir(intruder, 0o700)
        self.finished_run(run_dir=intruder)

        def to_link(_):
            os.rename(linked, os.path.join(self.base, "linked-away"))
            os.symlink(target, linked)
            return True

        def to_intruder(_):
            os.rename(swapped, os.path.join(self.base, "swapped-away"))
            os.rename(intruder, swapped)
            return True

        lstat = council_runs._lstat_at
        lstat = _hook(lstat, os.path.basename(linked), to_link)
        lstat = _hook(lstat, os.path.basename(swapped), to_intruder)
        with patch.object(council_runs, "_lstat_at", lstat):
            code, lines = self.prune(delete=True)
        self.assertEqual(code, 0, lines)
        self.assertTrue(self.line_for(lines, linked).startswith(
            f"skip {linked}: cannot be opened ("), lines)
        self.assertEqual(self.line_for(lines, swapped),
                         f"skip {swapped}: changed while being checked")
        for run in (target, swapped, os.path.join(self.base, "linked-away"),
                    os.path.join(self.base, "swapped-away")):
            self.assertTrue(os.path.isfile(os.path.join(run, "status.json")), run)
            self.assertTrue(os.path.isdir(os.path.join(run, "baseline")), run)

    def test_a_directory_swapped_during_removal_stops_it(self):
        """A subdirectory replaced before it is opened, or once emptied and
        before its rmdir, is someone else's: the removal stops there (exit
        1) and the replacement is left whole."""
        outside = os.path.join(self.base, "outside")
        os.makedirs(os.path.join(outside, "frozen"), 0o700)
        with open(os.path.join(outside, "frozen", "keep.txt"), "w",
                  encoding="utf-8") as f:
            f.write("keep\n")
        os.chmod(os.path.join(outside, "frozen", "keep.txt"), 0o400)
        os.chmod(os.path.join(outside, "frozen"), 0o500)
        before = _tree(outside)
        opened, emptied = self.finished_run(), self.finished_run()
        empty = os.path.join(self.base, "empty-outside")
        os.mkdir(empty, 0o700)
        empty_ino = os.stat(empty).st_ino

        def swap(parent_fd, run, name, replacement):
            if os.fstat(parent_fd).st_ino != os.stat(run).st_ino:
                return False  # the same name in the other run
            os.rename(name, name + ".away", src_dir_fd=parent_fd,
                      dst_dir_fd=parent_fd)
            os.rename(replacement, os.path.join(run, name))
            return True

        def at_open(parent_fd):
            return swap(parent_fd, opened, "workspaces", outside)

        def at_rmdir(parent_fd):
            return swap(parent_fd, emptied, "evidence", empty)

        with patch.object(council_runs, "_open_subdir", _hook(
                    council_runs._open_subdir, "workspaces", at_open, before=True)), \
                patch.object(council_runs, "_rmdir_same", _hook(
                    council_runs._rmdir_same, "evidence", at_rmdir, before=True)):
            code, lines = self.prune(delete=True)
        self.assertEqual(code, 1, lines)
        self.assertTrue(self.line_for(lines, opened).startswith(
            f"failed {opened}: 'workspaces' changed or is not owned"), lines)
        self.assertTrue(self.line_for(lines, emptied).startswith(
            f"failed {emptied}: 'evidence' changed while being removed"), lines)
        self.assertEqual(_tree(os.path.join(opened, "workspaces")), before)
        self.assertEqual(os.stat(os.path.join(emptied, "evidence")).st_ino,
                         empty_ino)
        for run in (opened, emptied):
            self.assertTrue(os.path.isfile(os.path.join(run, "status.json")))

    def test_another_filesystem_is_never_entered(self):
        """A mount inside a run stops its removal; a run on another
        filesystem than the runs root is skipped."""
        mounted, foreign = self.finished_run(), self.finished_run()
        mnt = os.path.join(mounted, "workspaces", "architect", "mnt")
        os.mkdir(mnt, 0o700)
        with open(os.path.join(mnt, "on-volume.txt"), "w", encoding="utf-8") as f:
            f.write("keep\n")
        real = council_runs._lstat_at

        def lstat(dir_fd, name):
            st = real(dir_fd, name)
            if st is not None and name in ("mnt", os.path.basename(foreign)):
                return _other_device(st)
            return st

        with patch.object(council_runs, "_lstat_at", lstat):
            code, lines = self.prune(delete=True)
        self.assertEqual(code, 1, lines)
        self.assertTrue(self.line_for(lines, mounted).startswith(
            f"failed {mounted}: 'mnt' is on another filesystem; not entered"),
            lines)
        self.assertEqual(self.line_for(lines, foreign),
                         f"skip {foreign}: is on another filesystem")
        self.assertTrue(os.path.isfile(os.path.join(mnt, "on-volume.txt")))
        self.assertTrue(os.path.isdir(os.path.join(foreign, "baseline")))


class StatusReaderTests(RunRootCase):
    def test_roles_intact_says_whether_every_role_record_was_read(self):
        path = os.path.join(self.base, "status.json")
        settled = {"state": "settled", "attempt": 1}
        for roles, intact in (
                ({"a": settled, "b": {"state": "active", "pid": 7, "pgid": 7}}, True),
                ({}, True),
                ([settled], False),
                ({"a": settled, "b": "active"}, False),
                ({"a": {"state": "active", "pid": True, "pgid": 7}}, False),
                ({"a": {"state": "active", "pid": 7, "pgid": "7"}}, False)):
            with self.subTest(roles=roles):
                self.write_private(path, json.dumps({"roles": roles}).encode())
                view = council_liveness.read_status(path)
                self.assertIs(view.roles_intact, intact)


class WorkspaceRootTests(unittest.TestCase):
    def test_workers_take_their_workspace_as_the_project_root(self):
        """A run under ~/.local/state can sit inside a Git work tree (a
        dotfiles home): no AGENTS.md or .codex layer above the workspace may
        reach a worker, whatever the policy."""
        for spec in (None, {"sandbox": "workspace-write", "network": True,
                            "authorization": "user asked"},
                     {"external_tools": "native", "authorization": "user asked"},
                     {"sandbox": "bypass", "network": True,
                      "authorization": "user asked"}):
            with self.subTest(spec=spec):
                policy = council_policy.parse_policy(spec)
                self.assertIn("project_root_markers=[]",
                              council_policy.config_overrides(policy))
                options = council_policy.command_options(policy)
                index = options.index("project_root_markers=[]")
                self.assertEqual(options[index - 1], "-c")


# ---------- the CLI ----------

class RunRootCLITests(RunRootCase):
    def setUp(self):
        super().setUp()
        self.env = council_testlib.clean_env(
            PATH=council_testlib.fake_bin_dir() + os.pathsep
            + os.environ.get("PATH", ""),
            XDG_STATE_HOME=os.path.join(self.base, "state"),
            CODEX_HOME=os.path.join(self.base, "codex-home"),
            PYTHONDONTWRITEBYTECODE="1")
        self.source = os.path.join(self.base, "source")
        os.mkdir(self.source, 0o700)

    def cli(self, *args):
        return subprocess.run([sys.executable, SCRIPT, *args],
                              capture_output=True, text=True, env=self.env,
                              cwd=self.base, stdin=subprocess.DEVNULL,
                              timeout=60)

    def test_new_run_prints_one_path_that_prepare_accepts(self):
        proc = self.cli("--new-run", "--skill-contract", EPOCH)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        path = proc.stdout.rstrip("\n")
        self.assertEqual(proc.stdout, path + "\n")
        self.assertTrue(os.path.isabs(path))
        self.assertEqual(os.path.dirname(path), self.runs)
        self.assertEqual(_mode(path), 0o700)
        with open(os.path.join(path, "roles.json"), "w", encoding="utf-8") as f:
            json.dump([{"id": "quick", "label": "Quick",
                        "instruction": ["Review the quick lens."]}], f)
        with open(os.path.join(path, "context.md"), "w", encoding="utf-8") as f:
            f.write("Please review.\n")
        council_testlib.prepare_run(self, path, self.env, self.base,
                                    source_root=self.source)
        self.assertTrue(os.path.isfile(os.path.join(path, "run.json")))

    def test_new_run_refuses_an_unsafe_state_directory_with_exit_2(self):
        os.makedirs(self.state)
        os.chmod(self.state, 0o755)
        proc = self.cli("--new-run", "--skill-contract", EPOCH)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(proc.stderr, (
            f"--new-run: state directory {self.state!r} is mode 0755; it is "
            "not changed. It also holds saved threads and role locks, which "
            "moving it aside would discard: chmod 700 it (as a council launch "
            "would) or point XDG_STATE_HOME at an owned private directory; "
            "nothing was used\n"))
        self.assertEqual(os.listdir(self.state), [])
        self.assertEqual(_mode(self.state), 0o755)

    def test_a_source_root_holding_the_state_home_is_explained(self):
        """source_root $HOME (say) contains ~/.local/state, so every
        --new-run directory is inside it: the refusal says why and how out."""
        self.env["XDG_STATE_HOME"] = os.path.join(self.source, ".local", "state")
        proc = self.cli("--new-run", "--skill-contract", EPOCH)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        run = proc.stdout.strip()
        with open(os.path.join(run, "roles.json"), "w", encoding="utf-8") as f:
            json.dump([{"id": "quick", "label": "Quick",
                        "instruction": ["Review the quick lens."]}], f)
        with open(os.path.join(run, "context.md"), "w", encoding="utf-8") as f:
            f.write("Please review.\n")
        council_testlib.write_request(run, self.source, (), "test-task")
        proc = self.cli("--prepare", run, "--skill-contract", EPOCH)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(proc.stderr, (
            "--prepare: source and run directory must be separate, "
            "non-overlapping trees, but the run directory is inside "
            "source_root (a --new-run directory lives under $XDG_STATE_HOME, "
            "by default ~/.local/state): choose a source_root that does not "
            "contain it, or set XDG_STATE_HOME outside it; start over in a "
            "new directory from `--new-run` (if the inputs changed during "
            "capture, pause overlapping writers first).\n"))
        self.assertFalse(os.path.lexists(os.path.join(run, "prepare.lock")))

    def test_prune_cli_lists_deletes_and_refuses(self):
        run = self.finished_run()
        listing = self.cli("--prune-runs", "--skill-contract", EPOCH)
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertIn(f"eligible {run}: done 40.0 days ago", listing.stdout)
        self.assertIn("older than 30 days", listing.stdout)
        self.assertTrue(os.path.isdir(run))
        bad = self.cli("--prune-runs", "--older-than", "-3")
        self.assertEqual(bad.returncode, 2)
        self.assertTrue(os.path.isdir(run))
        delete = self.cli("--prune-runs", "--delete", "--older-than", "31")
        self.assertEqual(delete.returncode, 0, delete.stderr)
        self.assertIn(f"removed {run}: done 40.0 days ago", delete.stdout)
        self.assertFalse(os.path.lexists(run))
        os.symlink(self.base, self.runs + ".tmp")
        os.rmdir(self.runs)
        os.rename(self.runs + ".tmp", self.runs)
        unsafe = self.cli("--prune-runs", "--delete")
        self.assertEqual(unsafe.returncode, 2)
        self.assertIn(f"--prune-runs: runs directory {self.runs!r} is a "
                      "symlink", unsafe.stderr)
        self.assertIn("nothing was deleted", unsafe.stderr)

    def test_a_launched_council_is_pruned_once_it_ended(self):
        """End to end: --new-run, --prepare, --start against the fake codex,
        then --prune-runs. The live run is refused; once its runner is gone
        the whole directory goes and the role's saved thread stays."""
        proc = self.cli("--new-run", "--skill-contract", EPOCH)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        run = proc.stdout.strip()
        with open(os.path.join(run, "roles.json"), "w", encoding="utf-8") as f:
            json.dump([{"id": "quick", "label": "Quick",
                        "instruction": ["Review the quick lens.",
                                        "Thoroughness beats speed."]}], f)
        with open(os.path.join(run, "context.md"), "w", encoding="utf-8") as f:
            f.write("Please review.\n")
        council_testlib.prepare_run(self, run, self.env, self.base,
                                    source_root=self.source)
        start = self.cli("--start", run, "--skill-contract", EPOCH)
        self.assertEqual(start.returncode, 0, start.stderr)
        deadline = time.monotonic() + 60
        while council_liveness.lock_state(run) != "free":
            self.assertLess(time.monotonic(), deadline, "runner did not end")
            time.sleep(0.05)
        with open(os.path.join(run, "supervisor.json"), encoding="utf-8") as f:
            pid = json.load(f)["pid"]
        self.assertTrue(council_testlib.pid_gone(pid))
        saved = sorted(name for name in os.listdir(self.state)
                       if name != "runs")
        self.assertTrue(any(name.endswith(".json") for name in saved), saved)
        listing = self.cli("--prune-runs", "--older-than", "0")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertRegex(listing.stdout, rf"eligible {re.escape(run)}: done ")
        delete = self.cli("--prune-runs", "--older-than", "0", "--delete")
        self.assertEqual(delete.returncode, 0, delete.stdout + delete.stderr)
        self.assertRegex(delete.stdout, rf"removed {re.escape(run)}: done ")
        self.assertFalse(os.path.lexists(run))
        self.assertEqual(sorted(name for name in os.listdir(self.state)
                                if name != "runs"), saved)


if __name__ == "__main__":
    unittest.main()
