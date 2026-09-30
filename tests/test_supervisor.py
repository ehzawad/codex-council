"""The detached launch: --start, its supervised child, and --cancel.

Real subprocesses against the fake codex (no Codex, no network). The
process-death, concurrency, cancel, and stale-lock scenarios (S8-S12) live
in liveness_scenarios.py; this module pins the command's contract: what
it refuses before creating anything, what it creates and with which mode,
what it prints, what the supervisor's command line carries, and how the
hidden supervisor entry refuses anything but --start's own handoff.

Run from repo root:
    python3 -m unittest discover -s tests -p 'test_*.py'
"""

import contextlib
import fcntl
import io
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import council_testlib  # noqa: E402
from council_testlib import EPOCH, SCRIPT  # noqa: E402
import codex_council  # noqa: E402
import council_liveness  # noqa: E402
from fake_codex import EXEC_SENTINELS  # noqa: E402

setUpModule = council_testlib.install_fake_codex
tearDownModule = council_testlib.remove_fake_codex

# Text that must never appear on a process command line.
CONTEXT_MARKER = "context-marker-6f1d0c"
STARTED_RE = re.compile(
    r"^\[codex-council\] started: pid=(\d+) dir=(\S+) version=\S+$")


def _role(role_id, *sentences):
    return {"id": role_id, "label": role_id.title(),
            "instruction": [*sentences,
                            "If nothing material falls in your lens, say so.",
                            "Thoroughness beats speed."]}


class StartCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = tmp.name
        self.pid_dir = os.path.join(self.base, "pids")
        os.mkdir(self.pid_dir)
        self.run_dir = self.stage("run")
        self.env = council_testlib.clean_env(
            PATH=council_testlib.fake_bin_dir() + os.pathsep
            + os.environ.get("PATH", ""),
            XDG_STATE_HOME=os.path.join(self.base, "state"),
            CODEX_HOME=os.path.join(self.base, "codex-home"),
            FAKE_CODEX_PID_DIR=self.pid_dir,
        )
        self.addCleanup(self.stop_everything)

    def stage(self, name, roles=None, context=None):
        run_dir = os.path.join(self.base, name)
        os.mkdir(run_dir, 0o700)
        with open(os.path.join(run_dir, "roles.json"), "w",
                  encoding="utf-8") as f:
            json.dump(roles or [_role("quick", "Review the quick lens.")], f)
        with open(os.path.join(run_dir, "context.md"), "w",
                  encoding="utf-8") as f:
            f.write(context or f"Please review. {CONTEXT_MARKER}\n")
        return run_dir

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, SCRIPT, *args], capture_output=True, text=True,
            env=self.env, cwd=self.base, stdin=subprocess.DEVNULL, timeout=60)

    def start(self, run_dir=None):
        return self.run_cli("--start", run_dir or self.run_dir,
                            "--skill-contract", EPOCH)

    def record(self, run_dir=None):
        with open(os.path.join(run_dir or self.run_dir, "supervisor.json"),
                  encoding="utf-8") as f:
            return json.load(f)

    def wait_end(self, run_dir=None, timeout=60):
        run_dir = run_dir or self.run_dir
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if council_liveness.lock_state(run_dir) == "free":
                return
            time.sleep(0.05)
        self.fail("the supervisor did not end")

    def stop_everything(self):
        for name in os.listdir(self.base):
            path = os.path.join(self.base, name, "supervisor.json")
            with contextlib.suppress(OSError, ValueError, KeyError,
                                     TypeError):
                with open(path, encoding="utf-8") as f:
                    pid = json.load(f)["pid"]
                command = subprocess.run(
                    ["ps", "-o", "command=", "-p", str(pid)],
                    capture_output=True, text=True).stdout
                if "--supervisor-lock-fd" in command:
                    os.kill(pid, signal.SIGKILL)
        for name in os.listdir(self.pid_dir):
            with contextlib.suppress(OSError, ValueError):
                with open(os.path.join(self.pid_dir, name)) as f:
                    council_testlib.kill_quietly(int(f.read()))


class StartCommandTests(StartCase):
    def test_start_prints_the_started_line_and_the_three_commands(self):
        proc = self.start()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        lines = proc.stdout.splitlines()
        # A quick council can end before --start looks: still a start.
        if len(lines) == 5 and lines[1].startswith(
                "note: the runner has already ended (exit 0)"):
            del lines[1]
        self.assertEqual(len(lines), 4, lines)
        match = STARTED_RE.match(lines[0])
        self.assertIsNotNone(match, lines[0])
        self.assertEqual(int(match.group(1)), self.record()["pid"])
        self.assertEqual(match.group(2), self.run_dir)
        for line, (label, command) in zip(lines[1:], (
                ("follow", "--follow"), ("status", "--status"),
                ("cancel", "--cancel"))):
            self.assertTrue(line.startswith(f"{label}: "), line)
            self.assertTrue(line.endswith(
                f"{os.path.realpath(SCRIPT)} {command} {self.run_dir} "
                f"--skill-contract {EPOCH}"), line)
        self.wait_end()
        with open(os.path.join(self.run_dir, "out.md"),
                  encoding="utf-8") as f:
            self.assertIn("1/1 roles responded", f.read())

    def test_created_files_are_private_and_the_record_is_complete(self):
        proc = self.start()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_end()
        for name in ("supervisor.lock", "supervisor.json", "err.log",
                     "out.md", "status.json"):
            with self.subTest(file=name):
                st = os.lstat(os.path.join(self.run_dir, name))
                self.assertTrue(stat.S_ISREG(st.st_mode))
                self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
        record = self.record()
        lock_path = os.path.join(self.run_dir, "supervisor.lock")
        lock = os.stat(lock_path)
        with open(lock_path, "rb") as f:
            token = f.read().decode("ascii")
        self.assertRegex(token, r"^[0-9a-f]{32}$")
        self.assertEqual(record["lock"], {"dev": lock.st_dev,
                                          "ino": lock.st_ino,
                                          "token": token})
        self.assertEqual(record["epoch"], codex_council.SKILL_CONTRACT_EPOCH)
        self.assertEqual(record["pgid"], record["pid"])
        self.assertEqual(record["sid"], record["pid"])
        self.assertTrue(record["start_identity"])
        with open(os.path.join(self.run_dir, "status.json"),
                  encoding="utf-8") as f:
            status = json.load(f)
        self.assertEqual(status["schema"], 1)
        self.assertEqual(status["runner"]["mode"], "detached")
        self.assertEqual(status["runner"]["pid"], record["pid"])

    def test_the_supervisor_command_line_carries_no_context(self):
        run_dir = self.stage("slow", roles=[_role(
            "slow", f"{EXEC_SENTINELS['sleep_secs']}30.")])
        proc = self.start(run_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        pid = self.record(run_dir)["pid"]
        command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                                 capture_output=True, text=True).stdout
        self.assertIn("--supervisor-lock-fd", command)
        self.assertIn(os.path.join(run_dir, "context.md"), command)
        self.assertNotIn(CONTEXT_MARKER, command)
        self.assertNotIn("Please review", command)
        cancel = self.run_cli("--cancel", run_dir, "--skill-contract", EPOCH)
        self.assertEqual(cancel.returncode, 0, cancel.stdout)

    def test_a_refused_start_leaves_the_directory_untouched(self):
        """Every refusal before the claim exits 2 and creates nothing: a
        public or symlinked directory, invalid roles, missing codex."""
        public = self.stage("public")
        os.chmod(public, 0o755)
        link = os.path.join(self.base, "link")
        os.symlink(self.run_dir, link)
        bad_roles = self.stage("badroles")
        with open(os.path.join(bad_roles, "roles.json"), "w",
                  encoding="utf-8") as f:
            f.write('[{"id": "x"}]')
        cases = (
            (public, {}, "--start: '"),
            (link, {}, "is a symlink"),
            (bad_roles, {}, "missing field 'label'"),
            (self.run_dir, {"PATH": "/usr/bin:/bin"},
             "--start: Codex CLI not found on PATH"),
        )
        for run_dir, env, expected in cases:
            with self.subTest(expected=expected):
                before = sorted(os.listdir(run_dir))
                proc = subprocess.run(
                    [sys.executable, SCRIPT, "--start", run_dir],
                    capture_output=True, text=True,
                    env={**self.env, **env}, cwd=self.base, timeout=60)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn(expected, proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assertEqual(sorted(os.listdir(run_dir)), before)
        self.assertEqual(os.listdir(self.pid_dir), [])

    def test_a_launched_or_claimed_directory_is_refused(self):
        """A tracked launch's files, a planted lock, or a symlinked lock:
        exit 2, and nothing is created, truncated, or followed."""
        tracked = self.stage("tracked")
        for name in ("out.md", "err.log"):
            with open(os.path.join(tracked, name), "w",
                      encoding="utf-8") as f:
                f.write("a running council's output\n")
        planted = self.stage("planted")
        os.close(os.open(os.path.join(planted, "supervisor.lock"),
                         os.O_CREAT | os.O_WRONLY, 0o600))
        linked = self.stage("linked")
        target = os.path.join(self.base, "target")
        os.symlink(target, os.path.join(linked, "supervisor.lock"))
        for run_dir, present in ((tracked, "out.md, err.log"),
                                 (planted, "supervisor.lock"),
                                 (linked, "supervisor.lock")):
            with self.subTest(run_dir=os.path.basename(run_dir)):
                before = {}
                for name in os.listdir(run_dir):
                    path = os.path.join(run_dir, name)
                    before[name] = (os.lstat(path).st_mtime_ns,
                                    os.lstat(path).st_size)
                proc = self.start(run_dir)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn(f"already holds a council launch ({present} "
                              "present)", proc.stderr)
                after = {}
                for name in os.listdir(run_dir):
                    path = os.path.join(run_dir, name)
                    after[name] = (os.lstat(path).st_mtime_ns,
                                   os.lstat(path).st_size)
                self.assertEqual(after, before)
        self.assertFalse(os.path.lexists(target))

    def test_the_claim_race_loser_creates_and_truncates_nothing(self):
        """The O_EXCL claim itself: a lock created after validation (a
        concurrent --start that won) makes this --start exit 2 without
        touching the winner's files."""
        real_validate = codex_council._validate_staging_dir

        def validate_then_lose(*args, **kwargs):
            result = real_validate(*args, **kwargs)
            for name in ("supervisor.lock", "err.log", "out.md"):
                with open(os.path.join(self.run_dir, name), "w",
                          encoding="utf-8") as f:
                    f.write(f"winner's {name}\n")
            return result

        err = io.StringIO()
        with patch.dict(os.environ, self.env, clear=True), \
                patch.object(codex_council, "_validate_staging_dir",
                             validate_then_lose), \
                contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(SystemExit) as ctx:
            codex_council._start_command(self.run_dir)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("supervisor.lock already exists", err.getvalue())
        for name in ("supervisor.lock", "err.log", "out.md"):
            with open(os.path.join(self.run_dir, name),
                      encoding="utf-8") as f:
                self.assertEqual(f.read(), f"winner's {name}\n")
        self.assertFalse(os.path.exists(
            os.path.join(self.run_dir, "supervisor.json")))

    def test_a_supervisor_that_wrote_its_record_and_ended_is_a_start(self):
        """A slow launcher can first look after the supervisor wrote
        supervisor.json and already finished: that is a start (exit 0,
        with a note), never 'start failed'."""
        real_popen = subprocess.Popen
        writer = (
            "import json, os, sys\n"
            "fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL"
            " | os.O_NOFOLLOW, 0o600)\n"
            "os.write(fd, json.dumps({'schema': 1, 'pid': os.getpid()})"
            ".encode())\n"
            "os.close(fd)\n")
        sup_path = os.path.join(self.run_dir, "supervisor.json")

        def finished_child(argv, **kwargs):
            proc = real_popen([sys.executable, "-c", writer, sup_path],
                              **kwargs)
            proc.wait()  # ended before --start's first look
            return proc

        err, out = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self.env, clear=True), \
                patch.object(codex_council.subprocess, "Popen",
                             finished_child), \
                patch.object(codex_council, "START_POLL_SECS", 3), \
                contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(out):
            codex_council._start_command(self.run_dir)
        self.assertEqual(err.getvalue(), "")
        lines = out.getvalue().splitlines()
        self.assertIsNotNone(STARTED_RE.match(lines[0]), lines)
        self.assertEqual(lines[1], "note: the runner has already ended "
                                   "(exit 0); run --status, then read out.md")
        self.assertEqual(len(lines), 5, lines)

    def test_a_start_with_a_standard_stream_closed_still_starts(self):
        """With fd 0 or 1 closed, the claimed files must not land on a
        standard-stream number the child's setup would overwrite."""
        for closed in (0, 1):
            with self.subTest(closed=closed):
                run_dir = self.stage(f"closed{closed}")
                proc = subprocess.run(
                    [sys.executable, SCRIPT, "--start", run_dir,
                     "--skill-contract", EPOCH],
                    stderr=subprocess.PIPE, text=True, env=self.env,
                    cwd=self.base, timeout=60,
                    stdin=None if closed == 0 else subprocess.DEVNULL,
                    stdout=None if closed == 1 else subprocess.DEVNULL,
                    preexec_fn=lambda fd=closed: os.close(fd))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.wait_end(run_dir)
                with open(os.path.join(run_dir, "out.md"),
                          encoding="utf-8") as f:
                    self.assertIn("1/1 roles responded", f.read())

    def test_a_supervisor_that_exits_at_once_is_exit_1(self):
        real_popen = subprocess.Popen

        def failing_child(argv, **kwargs):
            return real_popen([sys.executable, "-c",
                               "import sys; sys.exit(3)"], **kwargs)

        err, out = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self.env, clear=True), \
                patch.object(codex_council.subprocess, "Popen",
                             failing_child), \
                contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(out), \
                self.assertRaises(SystemExit) as ctx:
            codex_council._start_command(self.run_dir)
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("exited with status 3 before the council was running",
                      err.getvalue())
        self.assertIn("err.log", err.getvalue())
        self.assertIn("Do not retry --start in this directory",
                      err.getvalue())
        self.assertEqual(council_liveness.lock_state(self.run_dir), "free")
        again = self.start()
        self.assertEqual(again.returncode, 2)


class SignalLatchCliTests(StartCase):
    """A repeated termination signal during teardown changes nothing: one
    interruption line, 128 + the FIRST signal, and every worker gone."""

    def test_repeated_signals_keep_the_first_signals_exit(self):
        roles = [_role("hanger", f"{EXEC_SENTINELS['tool_session']}.",
                       f"{EXEC_SENTINELS['sleep_secs']}300.")]
        run_dir = self.stage("latch", roles=roles)
        err_path = os.path.join(run_dir, "err.log")
        with open(os.path.join(run_dir, "out.md"), "wb") as out, \
                open(err_path, "wb") as err:
            proc = subprocess.Popen(
                [sys.executable, SCRIPT,
                 "--roles-file", os.path.join(run_dir, "roles.json"),
                 "--context-file", os.path.join(run_dir, "context.md"),
                 "--skill-contract", EPOCH],
                stdout=out, stderr=err, env=self.env, cwd=self.base,
                stdin=subprocess.DEVNULL, start_new_session=True,
                preexec_fn=council_testlib.default_signal_dispositions)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        deadline = time.monotonic() + 30
        while not any(name.startswith("tool-")
                      for name in os.listdir(self.pid_dir)):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        proc.send_signal(signal.SIGTERM)
        for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            time.sleep(0.01)
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(sig)
        self.assertEqual(proc.wait(timeout=60), 128 + signal.SIGTERM)
        with open(err_path, encoding="utf-8") as f:
            interruptions = [line for line in f.read().splitlines()
                             if "interrupted by" in line]
        self.assertEqual(interruptions,
                         ["[codex-council] interrupted by SIGTERM"])
        for name in os.listdir(self.pid_dir):
            with open(os.path.join(self.pid_dir, name)) as f:
                self.assertTrue(council_testlib.pid_gone(int(f.read())),
                                name)


class SupervisorEntryTests(StartCase):
    """The hidden --supervisor-lock-fd entry refuses anything that is not
    --start's own locked handoff, before any work."""

    def _child(self, fd):
        roles = os.path.join(self.run_dir, "roles.json")
        context = os.path.join(self.run_dir, "context.md")
        return subprocess.run(
            [sys.executable, SCRIPT, "--roles-file", roles, "--context-file",
             context, "--supervisor-lock-fd", str(fd)],
            capture_output=True, text=True, env=self.env, cwd=self.base,
            pass_fds=(fd,), timeout=60)

    def _assert_refused_before_work(self, proc, expected):
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn(expected, proc.stderr)
        self.assertFalse(os.path.exists(
            os.path.join(self.run_dir, "status.json")))
        self.assertEqual(os.listdir(self.pid_dir), [])

    def _open_lock(self):
        path = os.path.join(self.run_dir, "supervisor.lock")
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        return fd

    def test_a_descriptor_for_another_file_is_refused(self):
        self._open_lock()
        other = os.path.join(self.run_dir, "other")
        fd = os.open(other, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        self._assert_refused_before_work(
            self._child(fd), "is not this run's private supervisor.lock")

    def test_a_lock_held_by_another_process_is_refused(self):
        holder = self._open_lock()
        fcntl.flock(holder, fcntl.LOCK_EX)
        fd = os.open(os.path.join(self.run_dir, "supervisor.lock"), os.O_RDWR)
        self.addCleanup(os.close, fd)
        self._assert_refused_before_work(self._child(fd),
                                         "another process holds")

    def test_a_second_supervisor_for_one_directory_is_refused(self):
        fd = self._open_lock()
        with open(os.path.join(self.run_dir, "supervisor.json"), "w",
                  encoding="utf-8") as f:
            f.write("{}")
        self._assert_refused_before_work(self._child(fd), "already had a "
                                                          "supervisor")


if __name__ == "__main__":
    unittest.main()
