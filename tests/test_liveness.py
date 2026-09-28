"""Run liveness: status.json, the bounded codex lifecycle, --follow,
--status, and --reap.

Unit tests drive council_liveness and the runner's subprocess lifecycle
directly; LivenessScenarioTests runs every scenario of
tests/liveness_scenarios.py (the real runner CLI against the fake codex).

Run from repo root:
    python3 -m unittest discover -s tests -p 'test_*.py'
"""

import asyncio
import contextlib
import io
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# council_testlib first: it puts the scripts directory on sys.path.
from council_testlib import (  # noqa: E402
    assert_usage_exit,
    clean_env,
    pid_gone,
    pid_running,
)
import codex_council  # noqa: E402
import council_liveness  # noqa: E402
import liveness_scenarios  # noqa: E402

SCRIPT = liveness_scenarios.RUNNER
DISPATCH_LINE = ("[codex-council] dispatching 1 roles with max parallel 6 "
                 "(architect); version=9.9.9.")
DONE_LINE = ("[codex-council] CODEX_COUNCIL_DONE ok=1 total=1 elapsed=1.0s "
             "exit=0 version=9.9.9")
START_LINE = ("[codex-council] architect: started (fresh) attempt=1/2 "
              "watchdog=1800s")
HEARTBEAT_LINE = ("[codex-council] still running after 600s: completed=0/1; "
                  "active=1 (architect quiet=3s); queued=0; watchdog=1800s; "
                  "version=9.9.9.")


def _private_dir(test):
    path = tempfile.mkdtemp()
    test.addCleanup(lambda: subprocess.run(["rm", "-rf", path]))
    return path


def _dead_pid():
    """The pid of a process that has exited and been reaped."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _status(run_dir, *, pid, identity=None, state="running", tick_age=0.0,
            roles=None, exit_code=None):
    runner = {"pid": pid, "state": state}
    if identity is not None:
        runner["start_identity"] = identity
    if exit_code is not None:
        runner["exit"] = exit_code
    data = {"schema": 1, "run_id": "0123456789abcdef", "runner": runner,
            "tick": {"seq": 3, "at": time.time() - tick_age},
            "roles": roles or {"architect": {"state": "active",
                                             "attempt": 1}}}
    path = os.path.join(run_dir, council_liveness.STATUS_FILENAME)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(path + ".tmp", path)


def _own_runner():
    """This process as a live runner: (pid, start identity)."""
    pid = os.getpid()
    return pid, council_liveness.process_start_identity(pid)


def _sleeper_group(test, seconds=60):
    """A sleeping process leading its own group: (pid, start identity)."""
    proc = subprocess.Popen(["sleep", str(seconds)], start_new_session=True)

    def cleanup():
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()

    test.addCleanup(cleanup)
    return proc, council_liveness.process_start_identity(proc.pid)


# ---------- CLI arguments ----------

class LivenessArgTests(unittest.TestCase):
    def test_each_command_excludes_every_other_mode(self):
        commands = ("--follow", "--status", "--reap", "--discover")
        others = (["--roles-file", "r.json"], ["--context-file", "c.md"],
                  ["--check-staging-dir", "d"])
        for command in commands:
            for other in (*others, *([c, "/y"] for c in commands
                                     if c != command)):
                with self.subTest(command=command, other=other[0]):
                    err = assert_usage_exit(
                        self,
                        lambda c=command, o=other: codex_council._parse_args(
                            [c, "/x", *o]),
                        expect_in_stderr="cannot be combined with")
                    self.assertTrue(
                        f"{command} cannot be combined with {other[0]}" in err
                        or f"{other[0]} cannot be combined with {command}"
                        in err, err)

    def test_empty_values_are_rejected(self):
        for command in ("--follow", "--status", "--reap"):
            with self.subTest(command=command):
                assert_usage_exit(
                    self, lambda c=command: codex_council._parse_args([c, ""]),
                    expect_in_stderr=f"{command} must be non-empty")

    def test_verbose_requires_follow(self):
        assert_usage_exit(
            self, lambda: codex_council._parse_args(["--verbose"]),
            expect_in_stderr="--verbose requires --follow")
        args = codex_council._parse_args(["--follow", "/x", "--verbose"])
        self.assertTrue(args.verbose)

    def test_commands_accept_the_skill_contract(self):
        epoch = str(codex_council.SKILL_CONTRACT_EPOCH)
        for command in ("--follow", "--status", "--reap"):
            with self.subTest(command=command):
                args = codex_council._parse_args(
                    [command, "/x", "--skill-contract", epoch])
                self.assertEqual(getattr(args, command[2:]), "/x")


# ---------- process identity ----------

class ProcessIdentityTests(unittest.TestCase):
    def test_own_identity_is_stable(self):
        pid, identity = _own_runner()
        self.assertTrue(identity)
        self.assertEqual(council_liveness.process_start_identity(pid),
                         identity)
        self.assertEqual(council_liveness.runner_state(pid, identity),
                         "alive")
        self.assertEqual(council_liveness.runner_state(pid, None), "alive")

    def test_another_start_time_is_another_process(self):
        pid, _ = _own_runner()
        self.assertEqual(
            council_liveness.runner_state(pid, "Thu Jan  1 00:00:00 1970"),
            "gone")

    def test_missing_and_zombie_processes_are_gone(self):
        self.assertEqual(council_liveness.runner_state(_dead_pid(), None),
                         "gone")
        zombie = subprocess.Popen([sys.executable, "-c", "pass"])
        self.addCleanup(zombie.wait)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = subprocess.run(["ps", "-o", "stat=", "-p", str(zombie.pid)],
                                   capture_output=True, text=True).stdout
            if state.strip().startswith("Z"):
                break
            time.sleep(0.02)
        self.assertEqual(council_liveness.runner_state(zombie.pid, None),
                         "gone")

    def test_ps_failure_is_unknown_never_gone(self):
        with patch.object(council_liveness.subprocess, "run",
                          side_effect=OSError("no ps")):
            self.assertIsNone(council_liveness._process_table())
            self.assertEqual(council_liveness.runner_state(_dead_pid(), None),
                             "unknown")
        failed = subprocess.CompletedProcess([], 1, "", "ps: bad option\n")
        with patch.object(council_liveness.subprocess, "run",
                          return_value=failed):
            self.assertEqual(council_liveness.runner_state(1234, None),
                             "unknown")


# ---------- status.json ----------

class RunStatusTests(unittest.TestCase):
    def setUp(self):
        self.run_dir = _private_dir(self)
        self.path = os.path.join(self.run_dir, "status.json")
        self.run = council_liveness.RunStatus()

    def _read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_unattached_status_writes_nothing(self):
        self.run.begin(["a"])
        self.run.update("a", state="active", attempt=1)
        self.run.finish("done", 0)
        self.assertEqual(os.listdir(self.run_dir), [])

    def test_publishes_runner_roles_and_ticks_atomically(self):
        self.run.attach(self.path)
        self.run.begin(["a", "b"])
        first = self._read()
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(first["schema"], council_liveness.STATUS_SCHEMA)
        self.assertEqual(first["runner"]["pid"], os.getpid())
        self.assertEqual(first["runner"]["start_identity"],
                         council_liveness.process_start_identity(os.getpid()))
        self.assertEqual(first["runner"]["state"], "running")
        self.assertNotIn("exit", first["runner"])
        self.assertEqual(first["roles"],
                         {"a": {"state": "queued", "attempt": 0},
                          "b": {"state": "queued", "attempt": 0}})
        self.run.update("a", state="active", attempt=1)
        self.run.spawned("a", os.getpid(), os.getpgrp())
        second = self._read()
        self.assertGreater(second["tick"]["seq"], first["tick"]["seq"])
        role = second["roles"]["a"]
        self.assertEqual((role["state"], role["attempt"], role["pid"]),
                         ("active", 1, os.getpid()))
        self.assertEqual(role["start_identity"],
                         second["runner"]["start_identity"])
        self.assertAlmostEqual(role["output_at"], time.time(), delta=5)
        self.run.exited("a")
        self.run.update("a", state="settled", outcome="ok")
        self.run.finish("done", 0)
        final = self._read()
        self.assertEqual(final["roles"]["a"]["outcome"], "ok")
        self.assertNotIn("pid", final["roles"]["a"])
        self.assertEqual((final["runner"]["state"], final["runner"]["exit"]),
                         ("done", 0))
        self.assertEqual(sorted(os.listdir(self.run_dir)), ["status.json"])

    def test_a_failed_write_is_reported_once_and_never_raises(self):
        self.run.attach(os.path.join(self.run_dir, "missing", "status.json"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.run.begin(["a"])
            self.run.update("a", state="active")
            self.run.finish("done", 0)
        self.assertEqual(err.getvalue().count("status.json not written"), 1)

    def test_a_failed_write_removes_the_earlier_file(self):
        """A file left from an earlier write would age into a false
        `runner not responding`; readers see no usable file instead."""
        self.run.attach(self.path)
        self.run.begin(["a"])
        self.assertIsNotNone(council_liveness.read_status(self.path))
        err = io.StringIO()
        with patch.object(council_liveness, "_atomic_write_private",
                          side_effect=OSError(28, "No space left on device")), \
                contextlib.redirect_stderr(err):
            self.run.update("a", state="active", attempt=1)
            self.run.update("a", state="settled", outcome="ok")
        self.assertEqual(os.listdir(self.run_dir), [])
        self.assertEqual(err.getvalue().count("status.json not written"), 1)
        self.run.finish("done", 0)
        self.assertEqual(council_liveness.read_status(self.path).state, "done")

    def test_numbers_no_float_can_hold_are_unknown(self):
        pid, identity = _own_runner()
        _status(self.run_dir, pid=pid, identity=identity, roles={
            "a": {"state": "active", "attempt": 1, "output_at": 10 ** 400}})
        with open(self.path, encoding="utf-8") as f:
            data = json.load(f)
        data["tick"]["at"] = 10 ** 400
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        view = council_liveness.read_status(self.path)
        self.assertEqual((view.pid, view.tick_at), (pid, None))
        self.assertIsNone(view.roles["a"]["output_at"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(council_liveness.status_command(self.run_dir), 0)
        self.assertIn(f"runner: not responding (pid {pid} present; last "
                      "status tick never)", out.getvalue())

    def test_reader_ignores_unknown_fields_and_distrusts_bad_types(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"schema": 99, "future": {"x": 1},
                       "runner": {"pid": "12", "state": "levitating",
                                  "start_identity": 5, "exit": -1},
                       "tick": {"at": "now"},
                       "roles": {"a": {"state": "weird", "pid": 1,
                                       "pgid": True, "attempt": 2,
                                       "output_at": float("nan")},
                                 "b": "not an object"}}, f)
        view = council_liveness.read_status(self.path)
        self.assertEqual((view.pid, view.identity, view.state, view.exit,
                          view.tick_at), (None, None, "unknown", None, None))
        self.assertEqual(view.roles, {"a": {
            "state": "unknown", "attempt": 2, "pid": None, "pgid": None,
            "start_identity": None, "output_at": None}})

    def test_reader_refuses_missing_invalid_and_non_regular_files(self):
        self.assertIsNone(council_liveness.read_status(self.path))
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertIsNone(council_liveness.read_status(self.path))
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("[1, 2]")
        self.assertIsNone(council_liveness.read_status(self.path))
        os.remove(self.path)
        target = os.path.join(self.run_dir, "elsewhere.json")
        with open(target, "w", encoding="utf-8") as f:
            json.dump({"runner": {"pid": 4242}}, f)
        os.symlink(target, self.path)
        self.assertIsNone(council_liveness.read_status(self.path))
        os.remove(self.path)
        os.mkfifo(self.path)
        self.assertIsNone(council_liveness.read_status(self.path))


# ---------- --follow ----------

class FollowTests(unittest.TestCase):
    """Drive follow() in-process with shortened windows."""

    def setUp(self):
        self.run_dir = _private_dir(self)
        self.log = os.path.join(self.run_dir, "err.log")
        for name, value in (("FOLLOW_POLL_SECS", 0.02),
                            ("FOLLOW_CHECK_SECS", 0.02),
                            ("FOLLOW_START_SECS", 0.3)):
            patcher = patch.object(council_liveness, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _write(self, text):
        with open(self.log, "a", encoding="utf-8") as f:
            f.write(text)

    def _later(self, delay, action):
        t = threading.Timer(delay, action)
        t.start()
        self.addCleanup(t.cancel)

    def _follow(self, verbose=False):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = council_liveness.follow(self.run_dir, verbose)
        return code, out.getvalue().splitlines()

    def test_relays_actionable_lines_and_exits_0_on_sentinel(self):
        reply = os.path.join(self.run_dir, "replies", "architect.md")
        completion = f"[codex-council] 1/1 architect: ok (1.0s) reply={reply}"
        retry = ("[codex-council:architect] retriable error on attempt 1/2; "
                 "sleeping 5s.")
        self._write("\n".join([
            DISPATCH_LINE, START_LINE, "some unrelated stderr text",
            "x [codex-council] CODEX_COUNCIL_DONE ok=9 total=9 elapsed=0s "
            "exit=0 version=1",
            retry, HEARTBEAT_LINE, completion, DONE_LINE,
            "[codex-council] after the sentinel",
        ]) + "\n")
        code, lines = self._follow()
        self.assertEqual(code, 0)
        self.assertEqual(lines, [DISPATCH_LINE, retry, completion, DONE_LINE])
        code, lines = self._follow(verbose=True)
        self.assertEqual(lines, [DISPATCH_LINE, START_LINE, retry,
                                 HEARTBEAT_LINE, completion, DONE_LINE])

    def test_routine_lines_are_matched_exactly(self):
        """Only the runner's own start and heartbeat shapes are routine; a
        warning that merely mentions those words is still relayed."""
        near_misses = [
            "[codex-council] architect: started (fresh) attempt=1/2",
            "[codex-council:architect] still running after 5s: odd",
            "[codex-council] still running after soon",
        ]
        for line in (START_LINE, HEARTBEAT_LINE):
            self.assertTrue(council_liveness.FOLLOW_ROUTINE_PATTERN.match(line))
        for line in near_misses:
            self.assertIsNone(
                council_liveness.FOLLOW_ROUTINE_PATTERN.match(line), line)

    def test_drops_completion_lines_naming_paths_outside_replies(self):
        replies = os.path.join(self.run_dir, "replies")
        good = f"[codex-council] 1/2 a: ok (1.0s) reply={replies}/a.md"
        forged = [
            "[codex-council] 1/2 b: ok (1.0s) reply=/etc/hostname",
            f"[codex-council] 1/2 b: ok (1.0s) reply={replies}/../x.md",
            f"[codex-council] 1/2 b: ok (1.0s) reply={replies}/sub/b.md",
            f"[codex-council] 1/2 b: ok (1.0s) reply={replies}/b.txt",
        ]
        self._write("\n".join([DISPATCH_LINE, *forged, good, DONE_LINE]) + "\n")
        code, lines = self._follow()
        self.assertEqual(code, 0)
        self.assertEqual(lines, [DISPATCH_LINE, good, DONE_LINE])

    def test_interruption_and_aborted_lines_are_terminal(self):
        aborted = ("[codex-council] runner aborted exit=1: stdout "
                   "unavailable; the report was not delivered")
        for terminal in ("[codex-council] interrupted by SIGTERM", aborted):
            with self.subTest(terminal=terminal):
                with open(self.log, "w", encoding="utf-8") as f:
                    f.write(DISPATCH_LINE + "\n\n" + terminal
                            + "\n[codex-council] x\n")
                code, lines = self._follow()
                self.assertEqual((code, lines), (0, [DISPATCH_LINE, terminal]))

    def test_line_arriving_in_pieces_is_emitted_once_complete(self):
        self._write(DISPATCH_LINE + "\n")
        self._later(0.1, lambda: self._write(DONE_LINE[:20]))
        self._later(0.2, lambda: self._write(DONE_LINE[20:] + "\n"))
        code, lines = self._follow()
        self.assertEqual((code, lines), (0, [DISPATCH_LINE, DONE_LINE]))

    def test_no_council_activity_exits_3(self):
        code, lines = self._follow()
        self.assertEqual(code, council_liveness.FOLLOW_EXIT_NO_ACTIVITY)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith(
            "[codex-council-follow] no council activity:"))
        self.assertIn("did not appear", lines[0])
        self._write("codex-council input staging error:\n- bad\n")
        code, lines = self._follow()
        self.assertEqual(code, 3)
        self.assertIn("no dispatch line", lines[-1])

    def test_err_log_appearing_late_is_followed(self):
        self._later(0.1, lambda: self._write(
            DISPATCH_LINE + "\n" + DONE_LINE + "\n"))
        code, lines = self._follow()
        self.assertEqual((code, lines), (0, [DISPATCH_LINE, DONE_LINE]))

    def test_traceback_is_advisory_not_terminal(self):
        self._write(DISPATCH_LINE + "\nTraceback (most recent call last):\n"
                    "  File x\nTraceback (most recent call last):\n"
                    + DONE_LINE + "\n")
        code, lines = self._follow()
        self.assertEqual(code, 0)
        self.assertEqual(
            len([ln for ln in lines if "Python traceback" in ln]), 1)
        self.assertEqual(lines[-1], DONE_LINE)

    def test_runner_gone_is_one_line_and_exit_4(self):
        self._write(DISPATCH_LINE + "\n")
        _status(self.run_dir, pid=_dead_pid(), roles={
            "architect": {"state": "active", "attempt": 1},
            "done": {"state": "settled", "outcome": "ok"},
            "later": {"state": "queued"}})
        code, lines = self._follow()
        self.assertEqual(code, council_liveness.FOLLOW_EXIT_RUNNER_GONE)
        self.assertEqual(lines[0], DISPATCH_LINE)
        self.assertRegex(
            lines[1],
            r"^\[codex-council-follow\] runner gone: pid=\d+; "
            r"unfinished=architect, later; live codex groups=none; "
            r"run --status$")
        self.assertEqual(len(lines), 2)

    def test_live_codex_groups_are_named_when_the_runner_is_gone(self):
        self._write(DISPATCH_LINE + "\n")
        sleeper, identity = _sleeper_group(self)
        _status(self.run_dir, pid=_dead_pid(), roles={"architect": {
            "state": "active", "attempt": 1, "pid": sleeper.pid,
            "pgid": sleeper.pid, "start_identity": identity}})
        code, lines = self._follow()
        self.assertEqual(code, 4)
        self.assertIn(f"live codex groups={sleeper.pid};", lines[-1])

    def test_a_terminal_line_or_terminal_status_beats_runner_gone(self):
        self._write(DISPATCH_LINE + "\n")
        _status(self.run_dir, pid=_dead_pid(), state="done", exit_code=0)
        code, lines = self._follow()
        self.assertEqual(code, 0)
        self.assertEqual(lines[-1], "[codex-council-follow] runner finished: "
                                    "state=done exit=0; err.log has no "
                                    "terminal line")

    def test_stale_tick_warns_once_then_recovers(self):
        pid, identity = _own_runner()
        self._write(DISPATCH_LINE + "\n")
        _status(self.run_dir, pid=pid, identity=identity, tick_age=130)
        self._later(0.4, lambda: _status(self.run_dir, pid=pid,
                                         identity=identity))
        self._later(0.6, lambda: self._write(DONE_LINE + "\n"))
        code, lines = self._follow()
        self.assertEqual(code, 0)
        self.assertEqual(len(lines), 4, lines)
        self.assertRegex(lines[1],
                         r"^\[codex-council-follow\] runner not responding: "
                         rf"no status tick for 13\ds \(pid {pid} still "
                         r"present\); run --status$")
        self.assertEqual(lines[2],
                         "[codex-council-follow] runner responding again")

    def test_a_tick_as_old_as_the_give_up_threshold_exits_4(self):
        pid, identity = _own_runner()
        self._write(DISPATCH_LINE + "\n")
        _status(self.run_dir, pid=pid, identity=identity, tick_age=130)
        self._later(0.3, lambda: _status(self.run_dir, pid=pid,
                                         identity=identity, tick_age=305))
        code, lines = self._follow()
        self.assertEqual(code, council_liveness.FOLLOW_EXIT_RUNNER_GONE)
        notes = [ln for ln in lines if "runner not responding" in ln]
        self.assertEqual(len(notes), 2)
        self.assertIn("no status tick for 30", notes[-1])

    def test_a_system_suspend_restarts_the_tick_age(self):
        """A wall-clock jump the monotonic clock does not share (a laptop
        suspend) must not read as a stopped runner."""
        pid, identity = _own_runner()
        self._write(DISPATCH_LINE + "\n")
        _status(self.run_dir, pid=pid, identity=identity)
        real_time = time.time
        calls = {"n": 0}

        def jumped_time():
            calls["n"] += 1
            return real_time() + (3600 if calls["n"] > 3 else 0)

        self._later(0.4, lambda: self._write(DONE_LINE + "\n"))
        with patch.object(council_liveness.time, "time", jumped_time):
            code, lines = self._follow()
        self.assertEqual((code, lines), (0, [DISPATCH_LINE, DONE_LINE]))

    def test_missing_status_follows_err_log_alone(self):
        self._write(DISPATCH_LINE + "\n")
        self._later(0.6, lambda: self._write(DONE_LINE + "\n"))
        code, lines = self._follow()
        self.assertEqual((code, lines), (0, [DISPATCH_LINE, DONE_LINE]))

    UNAVAILABLE_LINE = ("[codex-council-follow] runner liveness unavailable: "
                        "no usable status.json; following err.log only; run "
                        "--status")

    def test_status_never_usable_after_dispatch_warns_once(self):
        self._write(DISPATCH_LINE + "\n")
        self._later(0.8, lambda: self._write(DONE_LINE + "\n"))
        with patch.object(council_liveness, "STATUS_UNUSABLE_WARN_SECS", 0.2):
            code, lines = self._follow()
        self.assertEqual((code, lines),
                         (0, [DISPATCH_LINE, self.UNAVAILABLE_LINE, DONE_LINE]))

    def test_status_lost_after_dispatch_warns_once_and_relays_on(self):
        """A file that was usable and then goes missing or unreadable gets
        the same one line, never repeated, and err.log is still relayed."""
        pid, identity = _own_runner()
        self._write(DISPATCH_LINE + "\n")
        _status(self.run_dir, pid=pid, identity=identity)
        path = os.path.join(self.run_dir, council_liveness.STATUS_FILENAME)
        self._later(0.3, lambda: os.remove(path))

        def unreadable():
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json")

        self._later(0.9, unreadable)
        self._later(1.3, lambda: self._write(DONE_LINE + "\n"))
        with patch.object(council_liveness, "STATUS_UNUSABLE_WARN_SECS", 0.4):
            code, lines = self._follow()
        self.assertEqual((code, lines),
                         (0, [DISPATCH_LINE, self.UNAVAILABLE_LINE, DONE_LINE]))

    def test_a_new_parent_ends_the_follower_quietly(self):
        self._write(DISPATCH_LINE + "\n")
        pid, identity = _own_runner()
        _status(self.run_dir, pid=pid, identity=identity)
        parents = iter([4242, 4242, 1])
        with patch.object(council_liveness.os, "getppid",
                          side_effect=lambda: next(parents, 1)):
            code, lines = self._follow()
        self.assertEqual(code, council_liveness.FOLLOW_EXIT_PARENT_GONE)
        self.assertEqual(lines, [DISPATCH_LINE])

    def test_bad_run_directories_are_usage_errors(self):
        os.chmod(self.run_dir, 0o755)
        assert_usage_exit(self, lambda: council_liveness.follow(self.run_dir),
                          expect_in_stderr="--follow: ")
        os.chmod(self.run_dir, 0o700)
        link = self.run_dir + "-link"
        os.symlink(self.run_dir, link)
        self.addCleanup(os.remove, link)
        assert_usage_exit(self, lambda: council_liveness.follow(link),
                          expect_in_stderr="is a symlink")
        os.mkfifo(self.log)
        assert_usage_exit(self, lambda: council_liveness.follow(self.run_dir),
                          expect_in_stderr="not a regular file")

    def test_follow_never_writes(self):
        self._write(DISPATCH_LINE + "\n" + DONE_LINE + "\n")
        _status(self.run_dir, pid=_dead_pid())
        def contents():
            found = {}
            for name in os.listdir(self.run_dir):
                with open(os.path.join(self.run_dir, name), "rb") as f:
                    found[name] = f.read()
            return found

        before = contents()
        self._follow()
        self.assertEqual(contents(), before)


class FollowProcessTests(unittest.TestCase):
    """The follower as a real process: its stdout reader goes away."""

    def test_a_closed_reader_ends_the_follower_with_exit_1(self):
        run_dir = _private_dir(self)
        with open(os.path.join(run_dir, "err.log"), "w",
                  encoding="utf-8") as f:
            f.write(DISPATCH_LINE + "\n")
        pid, identity = _own_runner()
        _status(run_dir, pid=pid, identity=identity)
        proc = subprocess.Popen(
            [sys.executable, SCRIPT, "--follow", run_dir],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=clean_env())
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.assertEqual(proc.stdout.readline().decode().rstrip("\n"),
                         DISPATCH_LINE)
        proc.stdout.close()
        self.assertEqual(proc.wait(timeout=15), 1)
        self.assertNotIn(b"Traceback", proc.stderr.read())


# ---------- --status and --reap ----------

class StatusCommandTests(unittest.TestCase):
    def setUp(self):
        self.run_dir = _private_dir(self)

    def _lines(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(council_liveness.status_command(self.run_dir), 0)
        return out.getvalue().splitlines()

    def test_no_status_file(self):
        lines = self._lines()
        self.assertTrue(lines[0].startswith("runner: unknown (no usable "))
        self.assertTrue(lines[-1].startswith("next: read err.log"))

    def test_running_runner_and_unfinished_roles(self):
        pid, identity = _own_runner()
        roles = {f"r{i}": {"state": "queued"} for i in range(8)}
        roles["r0"] = {"state": "active", "attempt": 1, "pid": pid,
                       "pgid": os.getpgrp(), "start_identity": identity,
                       "output_at": time.time() - 12}
        roles["done"] = {"state": "settled", "outcome": "ok"}
        _status(self.run_dir, pid=pid, identity=identity, tick_age=3,
                roles=roles)
        lines = self._lines()
        self.assertLessEqual(len(lines), 10)
        self.assertRegex(lines[0],
                         rf"^runner: running \(pid {pid}; status tick \ds ago\)$")
        self.assertEqual(lines[1], "roles: 1 of 9 settled")
        self.assertRegex(lines[2], rf"^  r0: active, attempt 1, quiet 1\ds, "
                                   rf"codex pid {pid} alive$")
        self.assertEqual(lines[3], "  r1: queued")
        self.assertEqual(lines[-2], "  ... and 3 more unfinished")
        self.assertEqual(lines[-1], "next: keep following; do not relaunch")

    def test_not_responding_runner_is_never_reaped(self):
        pid, identity = _own_runner()
        _status(self.run_dir, pid=pid, identity=identity, tick_age=200)
        lines = self._lines()
        self.assertRegex(lines[0], r"^runner: not responding \(pid \d+ "
                                   r"present; last status tick 20\ds ago\)$")
        self.assertIn("never reap it", lines[-1])

    def test_gone_runner_names_live_groups_and_the_reap(self):
        sleeper, identity = _sleeper_group(self)
        _status(self.run_dir, pid=_dead_pid(), roles={"architect": {
            "state": "active", "attempt": 1, "pid": sleeper.pid,
            "pgid": sleeper.pid, "start_identity": identity}})
        lines = self._lines()
        self.assertTrue(lines[0].startswith("runner: gone (pid "))
        self.assertIn(f"live codex groups: {sleeper.pid} (architect)", lines)
        self.assertIn("--reap", lines[-1])

    def test_terminal_states(self):
        for state, code, action in (("done", 0, "read out.md"),
                                    ("interrupted", 143, "err.log")):
            with self.subTest(state=state):
                _status(self.run_dir, pid=_dead_pid(), state=state,
                        exit_code=code)
                lines = self._lines()
                self.assertEqual(lines[0], f"runner: {state} (exit {code})")
                self.assertIn(action, lines[-1])


class ReapCommandTests(unittest.TestCase):
    def setUp(self):
        self.run_dir = _private_dir(self)

    def _reap(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = council_liveness.reap_command(self.run_dir)
        return code, out.getvalue()

    def test_refused_without_status_or_while_the_runner_is_present(self):
        code, out = self._reap()
        self.assertEqual(code, 1)
        self.assertIn("--reap refused: no usable status.json", out)
        sleeper, identity = _sleeper_group(self)
        pid, runner_identity = _own_runner()
        _status(self.run_dir, pid=pid, identity=runner_identity, roles={
            "architect": {"state": "active", "pid": sleeper.pid,
                          "pgid": sleeper.pid, "start_identity": identity}})
        code, out = self._reap()
        self.assertEqual(code, 1)
        self.assertIn("still present", out)
        self.assertTrue(pid_running(sleeper.pid))

    def test_reaps_verified_groups_and_leaves_unverified_ones(self):
        ours, identity = _sleeper_group(self)
        foreign, _ = _sleeper_group(self)
        _status(self.run_dir, pid=_dead_pid(), roles={
            "ours": {"state": "active", "pid": ours.pid, "pgid": ours.pid,
                     "start_identity": identity},
            "reused": {"state": "active", "pid": foreign.pid,
                       "pgid": foreign.pid,
                       "start_identity": "Thu Jan  1 00:00:00 1970"},
            "settled": {"state": "settled", "outcome": "ok"}})
        code, out = self._reap()
        self.assertEqual(code, 0, out)
        self.assertIn(f"reaped ours: process group {ours.pid} (1 process) "
                      "terminated", out)
        self.assertIn(f"left alone reused: process group {foreign.pid}", out)
        self.assertTrue(pid_gone(ours.pid))
        self.assertTrue(pid_running(foreign.pid))
        code, out = self._reap()
        self.assertEqual(code, 0)
        self.assertNotIn("reaped", out)


    def test_reap_also_ends_tool_sessions_the_codex_started(self):
        path = os.path.join(self.run_dir, "tool.pid")
        leader = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""\
            import subprocess, sys, time
            tool = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                start_new_session=True, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with open({path!r}, "w") as f:
                f.write(str(tool.pid))
            time.sleep(60)
            """)], start_new_session=True)
        self.addCleanup(lambda: leader.poll() is None and leader.kill())
        deadline = time.monotonic() + 10
        while not os.path.exists(path) and time.monotonic() < deadline:
            time.sleep(0.02)
        with open(path, encoding="utf-8") as f:
            tool = int(f.read())
        self.addCleanup(lambda: pid_running(tool) and os.kill(
            tool, signal.SIGKILL))
        _status(self.run_dir, pid=_dead_pid(), roles={"ours": {
            "state": "active", "pid": leader.pid, "pgid": leader.pid,
            "start_identity":
                council_liveness.process_start_identity(leader.pid)}})
        code, out = self._reap()
        self.assertEqual(code, 0, out)
        self.assertIn("and 1 more process group it started terminated", out)
        leader.wait(timeout=5)
        self.assertTrue(pid_gone(tool))


class DescendantTargetsTests(unittest.TestCase):
    def test_walks_the_tree_and_names_own_groups_and_stray_pids(self):
        own = os.getpgrp()
        table = {
            100: (100, "S", "t", 1),      # codex, leading its group
            101: (101, "Ss", "t", 100),   # tool command in its own session
            102: (101, "S", "t", 101),    # that tool's child
            103: (100, "S", "t", 100),    # in codex's group: killpg covers it
            104: (999, "S", "t", 100),    # in a group it does not lead
            105: (own, "S", "t", 100),    # in this process's group: never
            200: (200, "S", "t", 1),      # unrelated
        }
        self.assertEqual(council_liveness.descendant_targets(100, table),
                         ([101], [104]))

    def test_unknown_root_or_table_means_nothing_to_signal(self):
        self.assertEqual(council_liveness.descendant_targets(7, {}), ([], []))
        self.assertEqual(
            council_liveness.descendant_targets(7, {8: (8, "S", "t", 1)}),
            ([], []))


# ---------- the runner's codex lifecycle ----------

class CodexLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """The real _run_codex_subprocess with scripted children."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pid_file = os.path.join(self.tmp.name, "descendant.pid")
        patcher = patch.object(codex_council, "POST_EXIT_DRAIN_SECS", 0.5)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _script(self, body):
        path = os.path.join(self.tmp.name, "child.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent("""\
                import json, subprocess, sys
                def emit(obj):
                    sys.stdout.write(json.dumps(obj) + "\\n")
                    sys.stdout.flush()
                def descendant(**kwargs):
                    child = subprocess.Popen(
                        [sys.executable, "-c", "import time; time.sleep(60)"],
                        **kwargs)
                    with open(%r, "w") as f:
                        f.write(str(child.pid))
                sys.stdin.read()
                """ % self.pid_file) + body)
        return [sys.executable, path]

    def _descendant(self):
        with open(self.pid_file, encoding="utf-8") as f:
            pid = int(f.read())
        self.addCleanup(lambda: pid_running(pid) and os.kill(
            pid, signal.SIGKILL))
        return pid

    _REPLY = textwrap.dedent("""\
        emit({"type": "thread.started", "thread_id": "sid"})
        emit({"type": "item.completed",
              "item": {"type": "agent_message", "text": "done"}})
        emit({"type": "turn.completed"})
        """)

    async def test_a_held_pipe_is_bounded_and_the_reply_kept(self):
        cmd = self._script("descendant()\n" + self._REPLY)
        err = io.StringIO()
        started = time.monotonic()
        with contextlib.redirect_stderr(err):
            run = await codex_council._run_codex_subprocess(cmd, "p", "leaky")
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(run.returncode, 0)
        self.assertEqual(codex_council.extract_final_message(run.stdout),
                         "done")
        self.assertEqual(run.warning, codex_council.POST_EXIT_DRAIN_WARNING)
        self.assertIn(f"[codex-council:leaky] "
                      f"{codex_council.POST_EXIT_DRAIN_WARNING}",
                      err.getvalue())
        self.assertTrue(pid_gone(self._descendant()))

    async def test_descendants_left_in_the_group_are_swept_quietly(self):
        cmd = self._script(
            "descendant(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,"
            " stderr=subprocess.DEVNULL)\n" + self._REPLY)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            run = await codex_council._run_codex_subprocess(cmd, "p", "tidy")
        self.assertIsNone(run.warning)
        self.assertEqual(err.getvalue(), "")
        self.assertTrue(pid_gone(self._descendant()))

    async def test_cancellation_during_the_drain_still_ends_the_group(self):
        patcher = patch.object(codex_council, "POST_EXIT_DRAIN_SECS", 30)
        patcher.start()
        self.addCleanup(patcher.stop)
        cmd = self._script("descendant()\n" + self._REPLY)
        task = asyncio.create_task(
            codex_council._run_codex_subprocess(cmd, "p", "leaky"))
        deadline = time.monotonic() + 10
        while not os.path.exists(self.pid_file) and \
                time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(pid_gone(self._descendant()))

    _TOOL_SESSION = (
        "descendant(start_new_session=True, stdin=subprocess.DEVNULL,"
        " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n")

    async def test_a_stall_kill_also_ends_tool_sessions_codex_started(self):
        # Current codex runs each tool command in its own session, outside
        # its process group: killing the group alone would leave it running.
        cmd = self._script(self._TOOL_SESSION
                           + "import time\ntime.sleep(60)\n")
        with patch.dict(os.environ, {"CODEX_COUNCIL_STALL_SECS": "1"}), \
                contextlib.redirect_stderr(io.StringIO()):
            run = await codex_council._run_codex_subprocess(cmd, "p", "tool")
        self.assertTrue(run.stalled)
        self.assertTrue(pid_gone(self._descendant()))

    async def test_cancellation_also_ends_tool_sessions_codex_started(self):
        cmd = self._script(self._TOOL_SESSION
                           + "import time\ntime.sleep(60)\n")
        task = asyncio.create_task(
            codex_council._run_codex_subprocess(cmd, "p", "tool"))
        deadline = time.monotonic() + 10
        while not os.path.exists(self.pid_file) and \
                time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(pid_gone(self._descendant()))

    async def test_undecodable_stdout_lines_never_stop_the_pump(self):
        cmd = self._script(
            'sys.stdout.write("[" * 100000 + "\\n")\n'
            'sys.stdout.write("1" + "0" * 5000 + "\\n")\n' + self._REPLY)
        run = await codex_council._run_codex_subprocess(cmd, "p")
        self.assertEqual(codex_council.extract_final_message(run.stdout),
                         "done")
        self.assertTrue(run.turn_completed)
        # An unreadable line could have been tool work.
        self.assertTrue(run.unsafe_to_replay)

    _BAD_ITEM = textwrap.dedent("""\
        import time
        emit({"type": "thread.started", "thread_id": "sid"})
        emit({"type": "turn.started"})
        emit({"type": "item.started", "item": {"type": []}})
        time.sleep(0.3)
        """)

    async def test_a_reply_after_a_malformed_item_is_still_extracted(self):
        cmd = self._script(self._BAD_ITEM + self._REPLY)
        run = await codex_council._run_codex_subprocess(cmd, "p")
        self.assertEqual(codex_council.extract_final_message(run.stdout),
                         "done")
        self.assertTrue(run.turn_completed)
        self.assertTrue(run.unsafe_to_replay)
        self.assertIsNone(run.warning)

    async def test_a_malformed_item_then_silence_is_never_replayed(self):
        """The verification council's reproduction: an item whose type is a
        list, a side effect, then silence under a 1s watchdog. The effect
        happens once and the stall is terminal, never retried."""
        effects = os.path.join(self.tmp.name, "effects.txt")
        cmd = self._script(self._BAD_ITEM + textwrap.dedent(f"""\
            with open({effects!r}, "a") as f:
                f.write("performed effect\\n")
            time.sleep(60)
            """))
        role = codex_council.Role(
            "review", "Review",
            "Review. If nothing material, say so. Thoroughness beats speed.")
        env = dict(clean_env(), XDG_STATE_HOME=self.tmp.name,
                   CODEX_COUNCIL_STALL_SECS="1")
        with patch.dict(os.environ, env, clear=True), \
                patch.object(codex_council, "STATE_DIR",
                             os.path.join(self.tmp.name, "state")), \
                patch.object(codex_council, "_fresh_cmd", return_value=cmd), \
                patch.object(codex_council, "RETRY_BACKOFF_SECS", 0), \
                contextlib.redirect_stderr(io.StringIO()):
            result = await codex_council._run_role_attempts(role, "p")
        with open(effects, encoding="utf-8") as f:
            self.assertEqual(f.read().splitlines(), ["performed effect"])
        self.assertFalse(result.ok)
        self.assertEqual(result.attempts, 1)
        self.assertTrue(result.error.startswith("[stall]"), result.error)

    async def test_a_failed_output_reader_makes_the_attempt_unsafe(self):
        """Whatever ends a pump early, the lost output could have held tool
        work: the attempt is never replay-safe and says why."""
        cmd = self._script('emit({"type": "turn.started"})\n'
                           "import time\ntime.sleep(60)\n")

        def broken_feed(scanner, chunk):
            raise RuntimeError("reader broke")

        err = io.StringIO()
        with patch.dict(os.environ, {"CODEX_COUNCIL_STALL_SECS": "1"}), \
                patch.object(codex_council._EventFlagScanner, "feed",
                             broken_feed), \
                contextlib.redirect_stderr(err):
            run = await codex_council._run_codex_subprocess(cmd, "p", "torn")
        self.assertTrue(run.stalled)
        self.assertTrue(run.unsafe_to_replay)
        self.assertEqual(
            run.warning,
            codex_council.IO_FAILED_WARNING.format("RuntimeError"))
        self.assertIn(f"[codex-council:torn] {run.warning}", err.getvalue())

    async def test_the_run_status_follows_the_codex_process(self):
        seen = []
        real_update = codex_council._RUN.update

        def update(role_id, **fields):
            seen.append(fields)
            real_update(role_id, **fields)

        codex_council._RUN.begin(["watched"])
        self.addCleanup(codex_council._RUN.begin, [])
        cmd = self._script(self._REPLY)
        with patch.object(codex_council._RUN, "update", side_effect=update):
            await codex_council._run_codex_subprocess(cmd, "p", "watched")
        self.assertEqual(seen[0]["pid"], seen[0]["pgid"])
        self.assertEqual(seen[-1], {"pid": None, "pgid": None,
                                    "start_identity": None})

    async def test_a_run_warning_reaches_the_role_result(self):
        async def fake_subprocess(cmd, prompt, role_id=""):
            return codex_council.CodexRun(
                returncode=0, stderr="",
                stdout="\n".join(json.dumps(e) for e in (
                    {"type": "thread.started", "thread_id": "sid-1"},
                    {"type": "item.completed",
                     "item": {"type": "agent_message", "text": "ok"}})),
                warning=codex_council.POST_EXIT_DRAIN_WARNING)

        role = codex_council.Role(
            "architect", "Architect",
            "Review. If nothing material, say so. Thoroughness beats speed.")
        env = {key: value for key, value in clean_env().items()}
        env["XDG_STATE_HOME"] = self.tmp.name
        with patch.dict(os.environ, env, clear=True), \
                patch.object(codex_council, "STATE_DIR",
                             os.path.join(self.tmp.name, "state")), \
                patch.object(codex_council, "_run_codex_subprocess",
                             side_effect=fake_subprocess), \
                contextlib.redirect_stderr(io.StringIO()):
            result = await codex_council._run_role_once(role, "p", 1)
        self.assertTrue(result.ok)
        self.assertEqual(result.warning,
                         codex_council.POST_EXIT_DRAIN_WARNING)


# ---------- the scenarios, end to end ----------

def _golden_s0():
    """S0's exact out.md and reply files (timings and the run path
    normalized): the happy path's output must not change."""
    native = ("_Model selection: native inheritance (no model or effort "
              "override sent)_")

    def section(role):
        return f"## {role.title()} ({role})\n\n{native}\n\nfake reply from codex\n"

    roles = ("alpha", "beta", "gamma")
    out = ("# Codex Council — 3/3 roles responded (T)\n\n## Summary\n\n"
           + "".join(f"- **{r.title()}** [{r}]: ok — T\n" for r in roles)
           + "\nModel selection: launch discovery not run (no runtime-"
           "grounded selections). codex exec does not report the model or "
           "effort that served a turn; values above are what the council "
           "sent, and \"native inheritance\" means no override was sent.\n\n"
           + "\n".join(section(r) for r in roles))
    golden = {"out.md": out}
    for r in roles:
        golden[f"replies/{r}.md"] = (
            f"<!-- codex-council reply id={r} status=ok elapsed=T attempts=1 "
            f"selection=native -->\n\n{section(r)}")
    return golden


class LivenessScenarioTests(unittest.TestCase):
    """Every scenario in liveness_scenarios.py, run once, concurrently."""

    # Follower lines S0 gives when every routine line is relayed too
    # (dispatch, model selection, three starts, three completions, done).
    S0_ALL_LINES = 9

    @classmethod
    def setUpClass(cls):
        cls.outcomes = {o.scenario: o
                        for o in liveness_scenarios.run_scenarios()}

    def _assert_passed(self, name):
        outcome = self.outcomes[name]
        self.assertTrue(outcome.passed, outcome.line())
        return outcome

    def test_s0_happy_path_is_unchanged_with_fewer_lines(self):
        outcome = self._assert_passed("S0")
        self.assertEqual(outcome.outputs, _golden_s0())
        self.assertEqual(outcome.follower_lines, 6)
        self.assertLess(outcome.follower_lines, self.S0_ALL_LINES)

    def test_s1_held_pipe(self):
        self._assert_passed("S1")

    def test_s2_runner_killed(self):
        self._assert_passed("S2")

    def test_s3_runner_stopped(self):
        self._assert_passed("S3")

    def test_s4_follower_orphaned(self):
        self._assert_passed("S4")

    def test_s5_silent_role(self):
        self._assert_passed("S5")

    def test_s6_malformed_lines(self):
        self._assert_passed("S6")


if __name__ == "__main__":
    unittest.main()
