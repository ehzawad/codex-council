#!/usr/bin/env python3
"""Liveness scenarios: the real runner CLI end to end against a fake codex.

Each scenario stages a private run directory, launches the council the way
the tracked fallback does (stdout to out.md, stderr to err.log; S8-S12 use
--start) with the fake `codex` from fake_codex.py first on PATH, follows it
with `--follow`, disturbs it, and prints one verdict line with the number
of lines the follower emitted:

  S0  happy path: three roles succeed.
  S1  a descendant of codex holds codex's stdout open after codex exits.
  S2  the runner is SIGKILLed mid-run while codex keeps running.
  S3  the runner is alive but its event loop is blocked (SIGSTOP).
  S4  the follower's parent process dies.
  S5  a role is byte-silent for a while, then succeeds.
  S6  a role writes stdout lines no JSON parser accepts while its stderr
      keeps printing, then succeeds.
  S7  the host stops a tracked launch (SIGTERM to its process group) while
      a role is silent: the interruption is clean and settled replies stay.
  S8  --start from a shell in its own session; after it returns, that
      shell's group gets SIGHUP, SIGTERM, and SIGKILL: the council finishes.
  S9  the --start process itself is SIGKILLed right after it spawned the
      supervisor: the council still finishes.
  S10 two concurrent --start calls on one directory, five times: one
      supervisor, the loser exits 2 and truncates nothing.
  S11 --cancel on a hanging role with a tool session: a clean interruption
      and every fake process gone.
  S12 only the supervisor is SIGKILLed: its lock is free at once, --status
      says gone, --follow exits 4, --reap ends codex, and the directory
      stays used up while a new one starts.

S3 compresses time: while the runner is stopped nothing rewrites
status.json, so the scenario backdates its tick to stand for the minutes a
stopped runner takes to cross the follower's thresholds.

Usage:
    python3 tests/liveness_scenarios.py [--runner PATH] [--only S1,S3]

--runner points at another codex_council.py (for example a release
extracted with `git archive`) to record its baseline. Exits 0 when every
selected scenario passes. test_liveness.py runs the same scenarios.
"""

import argparse
import concurrent.futures
import contextlib
import dataclasses
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

import fake_codex  # noqa: E402

RUNNER = os.path.abspath(os.path.join(
    TESTS_DIR, "..", "plugins", "codex-council", "skills", "codex-council",
    "scripts", "codex_council.py",
))
PYTHON = sys.executable
SENTINELS = fake_codex.EXEC_SENTINELS

# Routine per-attempt and heartbeat lines: err.log keeps them, the default
# follower does not relay them.
ROUTINE_LINE = re.compile(
    r"^\[codex-council\] (\S+: started \((fresh|resume)\) |still running after )"
)
FOLLOWER_NOTE = "[codex-council-follow]"

# Starts the follower as its child, records the child's pid, and waits; the
# follower inherits this process's stdout, so its lines still reach us.
_PARENT = (
    "import subprocess, sys\n"
    "child = subprocess.Popen(sys.argv[2:])\n"
    "with open(sys.argv[1], 'w') as f:\n"
    "    f.write(str(child.pid))\n"
    "child.wait()\n"
)


@dataclasses.dataclass
class Outcome:
    scenario: str
    title: str
    passed: bool
    detail: str
    follower_lines: int
    outputs: dict = dataclasses.field(default_factory=dict)

    def line(self):
        verdict = "PASS" if self.passed else "FAIL"
        digest = ""
        if self.outputs:
            joined = "\0".join(f"{k}\0{v}" for k, v in sorted(self.outputs.items()))
            digest = f" outputs={hashlib.sha256(joined.encode()).hexdigest()[:12]}"
        return (f"{self.scenario} {verdict} follower_lines={self.follower_lines}"
                f"{digest} | {self.title}: {self.detail}")


def _role(role_id, *sentences):
    return {
        "id": role_id, "label": role_id.title(),
        "instruction": [*sentences,
                        "If nothing material falls in your lens, say so.",
                        "Thoroughness beats speed."],
    }


def _alive(pid):
    """True while pid runs (a zombie awaiting its reaper counts as dead)."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                           capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def _wait_gone(pid, timeout):
    """Seconds until pid is gone, or None if it outlives `timeout`."""
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if not _alive(pid):
            return time.monotonic() - started
        time.sleep(0.05)
    return None


def _ours(pid):
    """A process this harness's fake started (never signal a reused pid)."""
    command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True).stdout
    return "fake_codex_impl.py" in command or "time.sleep(120)" in command


def _our_supervisor(pid, runner):
    """A detached runner this harness started (never a reused pid)."""
    command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True).stdout
    return runner in command and "--supervisor-lock-fd" in command


def _lock(path):
    """"held", "free", or "absent" for a supervisor.lock: the same
    non-blocking shared-lock probe --status makes."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return "absent"
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return "held"
    finally:
        os.close(fd)
    return "free"


def _children(pid):
    """Pids whose parent is pid, from one ps snapshot."""
    out = subprocess.run(["ps", "-A", "-o", "pid=", "-o", "ppid="],
                         capture_output=True, text=True).stdout
    found = []
    for line in out.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1] == str(pid):
            found.append(int(fields[0]))
    return found


def _started_pid(stdout):
    match = re.search(r"^\[codex-council\] started: pid=(\d+) ", stdout, re.M)
    return int(match.group(1)) if match else None


class Follower:
    """A `--follow` process whose stdout lines are collected as they come."""

    def __init__(self, argv, env, pid_file=None):
        self.lines = []
        self._cond = threading.Condition()
        self.proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, env=env)
        self.pid_file = pid_file
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self):
        for raw in self.proc.stdout:
            with self._cond:
                self.lines.append(raw.rstrip("\n"))
                self._cond.notify_all()

    def pid(self, timeout=10):
        """The follower's own pid (behind the parent when there is one)."""
        if self.pid_file is None:
            return self.proc.pid
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with contextlib.suppress(OSError, ValueError):
                with open(self.pid_file, encoding="utf-8") as f:
                    return int(f.read())
            time.sleep(0.05)
        return None

    def wait_for(self, pattern, timeout):
        """The first line matching pattern, waiting up to timeout."""
        regex = re.compile(pattern)
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                for line in self.lines:
                    if regex.search(line):
                        return line
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._cond.wait(min(left, 0.2))

    def wait_exit(self, timeout):
        try:
            code = self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            return None
        self._reader.join(5)
        return code

    def kill(self):
        pid = self.pid(0) if self.pid_file else None
        with contextlib.suppress(OSError):
            self.proc.kill()
        self.proc.wait()
        if pid and _alive(pid):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)


class Council:
    """One staged run directory, its runner, followers, and cleanup."""

    def __init__(self, runner, fake_bin, roles):
        self.runner = runner
        self.roles = roles
        self.base = tempfile.mkdtemp(prefix="council-liveness-")
        self.staged = []
        self.run_dir = self.stage("run")
        self.pid_dir = os.path.join(self.base, "pids")
        os.mkdir(self.pid_dir)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("CODEX_", "FAKE_CODEX_"))}
        env.update(
            PATH=fake_bin + os.pathsep + os.environ.get("PATH", ""),
            XDG_STATE_HOME=os.path.join(self.base, "state"),
            CODEX_HOME=os.path.join(self.base, "codex-home"),
            FAKE_CODEX_PID_DIR=self.pid_dir,
            CODEX_COUNCIL_SESSION_KEY=f"liveness-{os.path.basename(self.base)}",
        )
        self.env = env
        self.proc = None
        self.followers = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def stage(self, name, roles=None):
        """A new private run directory holding roles.json and context.md."""
        run_dir = os.path.join(self.base, name)
        os.mkdir(run_dir, 0o700)
        with open(os.path.join(run_dir, "roles.json"), "w",
                  encoding="utf-8") as f:
            json.dump(self.roles if roles is None else roles, f)
        with open(os.path.join(run_dir, "context.md"), "w",
                  encoding="utf-8") as f:
            f.write("Liveness scenario context.\n")
        self.staged.append(run_dir)
        return run_dir

    def path(self, *parts):
        return os.path.join(self.run_dir, *parts)

    def start_argv(self, run_dir=None):
        return [PYTHON, self.runner, "--start", run_dir or self.run_dir]

    def start(self, run_dir=None):
        """--start as an ordinary foreground command."""
        return subprocess.run(self.start_argv(run_dir), capture_output=True,
                              text=True, env=self.env, cwd=self.run_dir,
                              stdin=subprocess.DEVNULL, timeout=60)

    def supervisor(self, run_dir=None):
        try:
            with open(os.path.join(run_dir or self.run_dir,
                                   "supervisor.json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def lock(self, run_dir=None):
        return _lock(os.path.join(run_dir or self.run_dir, "supervisor.lock"))

    def wait_detached_end(self, timeout, run_dir=None):
        """status.json once the supervisor lock is free, or None."""
        run_dir = run_dir or self.run_dir
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.lock(run_dir) == "free":
                return self.status(run_dir) or {}
            time.sleep(0.05)
        return None

    def launch(self):
        with open(self.path("out.md"), "wb") as out, \
                open(self.path("err.log"), "wb") as err:
            self.proc = subprocess.Popen(
                [PYTHON, self.runner,
                 "--roles-file", self.path("roles.json"),
                 "--context-file", self.path("context.md")],
                stdout=out, stderr=err, env=self.env, cwd=self.run_dir,
                start_new_session=True)

    def follow(self, via_parent=False):
        argv = [PYTHON, self.runner, "--follow", self.run_dir]
        pid_file = None
        if via_parent:
            pid_file = os.path.join(self.base, "follower.pid")
            argv = [PYTHON, "-c", _PARENT, pid_file, *argv]
        follower = Follower(argv, self.env, pid_file)
        self.followers.append(follower)
        return follower

    def cli(self, *args, run_dir=None):
        return subprocess.run([PYTHON, self.runner, *args,
                                run_dir or self.run_dir],
                               capture_output=True, text=True, env=self.env,
                               timeout=90)

    def wait_runner(self, timeout):
        try:
            return self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            return None

    def recorded(self, kind, timeout=0.0):
        """Pids the fake recorded as `kind` (exec or holder)."""
        deadline = time.monotonic() + timeout
        while True:
            pids = []
            for name in sorted(os.listdir(self.pid_dir)):
                if name.startswith(f"{kind}-"):
                    with contextlib.suppress(OSError, ValueError):
                        with open(os.path.join(self.pid_dir, name)) as f:
                            pids.append(int(f.read()))
            if pids or time.monotonic() >= deadline:
                return pids
            time.sleep(0.05)

    def status(self, run_dir=None):
        try:
            with open(os.path.join(run_dir or self.run_dir, "status.json"),
                      encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def wait_status(self, predicate, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.status()
            if status is not None and predicate(status):
                return status
            time.sleep(0.05)
        return None

    def backdate_tick(self, seconds):
        """Rewrite status.json as if its last tick were `seconds` old."""
        status = self.status()
        status["tick"]["at"] = round(time.time() - seconds, 3)
        tmp = self.path(".status.json.harness")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(status, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path("status.json"))

    def read(self, *parts, run_dir=None):
        try:
            with open(os.path.join(run_dir or self.run_dir, *parts),
                      encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    def outputs(self):
        """out.md and reply files with timings and the run path normalized."""
        def norm(text):
            text = text.replace(self.run_dir, "RUNDIR")
            return re.sub(r"\d+\.\ds", "T", text)
        found = {"out.md": norm(self.read("out.md"))}
        with contextlib.suppress(OSError):
            for name in sorted(os.listdir(self.path("replies"))):
                found[f"replies/{name}"] = norm(self.read("replies", name))
        return found

    def close(self):
        for follower in self.followers:
            follower.kill()
        if self.proc is not None and self.proc.poll() is None:
            with contextlib.suppress(OSError):
                os.kill(self.proc.pid, signal.SIGCONT)
                os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait()
        for run_dir in self.staged:
            record = self.supervisor(run_dir) or {}
            pid = record.get("pid")
            if isinstance(pid, int) and _alive(pid) and _our_supervisor(
                    pid, self.runner):
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)
        for pid in (self.recorded("exec") + self.recorded("holder")
                    + self.recorded("tool")):
            if _alive(pid) and _ours(pid):
                with contextlib.suppress(OSError):
                    os.killpg(pid, signal.SIGKILL)
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)
        shutil.rmtree(self.base, ignore_errors=True)


def _role_active(role_id):
    def check(status):
        role = (status.get("roles") or {}).get(role_id) or {}
        return role.get("state") == "active" and role.get("pgid")
    return check


# ---------- scenarios ----------

def s0_happy_path(runner, fake_bin):
    roles = [_role(r, f"Review the {r} lens.") for r in ("alpha", "beta", "gamma")]
    with Council(runner, fake_bin, roles) as c:
        c.launch()
        follower = c.follow()
        code = follower.wait_exit(60)
        runner_code = c.wait_runner(30)
        outputs = c.outputs()
        replies = [k for k in outputs if k.startswith("replies/")]
        routine = [ln for ln in follower.lines if ROUTINE_LINE.search(ln)]
        passed = (code == 0 and runner_code == 0 and len(replies) == 3
                  and "3/3 roles responded" in outputs["out.md"])
        return Outcome(
            "S0", "happy path (3 roles)", passed,
            f"follower exit={code}; runner exit={runner_code}; "
            f"replies={len(replies)}; routine lines relayed={len(routine)}",
            len(follower.lines), outputs)


def s1_held_pipe(runner, fake_bin):
    roles = [_role("leaky", f"{SENTINELS['leak_output_holder']}.")]
    bound = 45
    with Council(runner, fake_bin, roles) as c:
        started = time.monotonic()
        c.launch()
        follower = c.follow()
        code = follower.wait_exit(bound)
        settled = time.monotonic() - started
        runner_code = c.wait_runner(10)
        holders = c.recorded("holder")
        survivors = [pid for pid in holders if _alive(pid)]
        reply = c.read("replies", "leaky.md")
        warned = "kept its output open" in reply
        passed = (code == 0 and runner_code == 0 and holders and not survivors
                  and warned and "status=ok" in reply)
        if code is None:
            detail = (f"role not settled within {bound}s: the runner is still "
                      "waiting on the held pipe")
        else:
            detail = (f"settled in {settled:.1f}s; follower exit={code}; "
                      f"reply ok={'status=ok' in reply}; warning={warned}; "
                      f"pipe holder alive={bool(survivors)}")
        return Outcome("S1", "descendant holds codex stdout after exit",
                       passed, detail, len(follower.lines))


def s2_runner_killed(runner, fake_bin):
    roles = [_role("sleeper", f"{SENTINELS['sleep_secs']}300.")]
    bound = 20
    with Council(runner, fake_bin, roles) as c:
        c.launch()
        follower = c.follow()
        codex = c.recorded("exec", timeout=20)
        c.wait_status(_role_active("sleeper"), 5)
        follower.wait_for(r"dispatching", 10)
        os.kill(c.proc.pid, signal.SIGKILL)
        c.proc.wait()
        killed = time.monotonic()
        gone = follower.wait_for(r"^\[codex-council-follow\] runner gone", bound)
        latency = time.monotonic() - killed
        code = follower.wait_exit(5)
        orphan = bool(codex) and _alive(codex[0])
        status = c.cli("--status")
        reap = c.cli("--reap")
        reaped = bool(codex) and _wait_gone(codex[0], 10) is not None
        status_first = (status.stdout or status.stderr).strip().splitlines()[:1]
        passed = (gone is not None and code == 4 and latency <= 10 and orphan
                  and status.returncode == 0 and "gone" in status.stdout
                  and reap.returncode == 0 and reaped)
        if gone is None:
            detail = (f"follower silent {bound}s after the runner SIGKILL "
                      f"(exit={code}); orphan codex alive={orphan}; "
                      f"--status exit={status.returncode}; "
                      f"--reap exit={reap.returncode}; orphan reaped={reaped}")
        else:
            detail = (f"follower reported runner gone {latency:.1f}s after "
                      f"SIGKILL (exit={code}); orphan codex alive={orphan}; "
                      f"--status exit={status.returncode} "
                      f"{status_first[0] if status_first else ''!r}; "
                      f"--reap exit={reap.returncode}; orphan reaped={reaped}")
        return Outcome("S2", "runner SIGKILLed mid-run", passed, detail,
                       len(follower.lines))


def s3_runner_stopped(runner, fake_bin):
    roles = [_role("sleeper", f"{SENTINELS['sleep_secs']}20.")]
    with Council(runner, fake_bin, roles) as c:
        c.launch()
        follower = c.follow()
        c.recorded("exec", timeout=20)
        follower.wait_for(r"dispatching", 10)
        has_status = c.wait_status(_role_active("sleeper"), 5) is not None
        os.kill(c.proc.pid, signal.SIGSTOP)
        try:
            if not has_status:
                note = follower.wait_for(r"^\[codex-council-follow\]", 10)
                detail = ("no status.json: nothing tells the follower the "
                          "runner stopped; follower lines while stopped "
                          f"(10s)={0 if note is None else 1}")
                return Outcome("S3", "runner alive, event loop blocked",
                               False, detail, len(follower.lines))
            c.backdate_tick(125)
            warned = follower.wait_for(
                r"^\[codex-council-follow\] runner not responding", 10)
            c.backdate_tick(305)
            code = follower.wait_exit(10)
        finally:
            os.kill(c.proc.pid, signal.SIGCONT)
        runner_code = c.wait_runner(60)
        passed = warned is not None and code == 4 and runner_code == 0
        detail = (f"warning at tick age 125s={warned is not None}; "
                  f"follower exit at 305s={code}; runner after SIGCONT "
                  f"exit={runner_code}")
        return Outcome("S3", "runner alive, event loop blocked", passed,
                       detail, len(follower.lines))


def s4_follower_orphaned(runner, fake_bin):
    roles = [_role("sleeper", f"{SENTINELS['sleep_secs']}15.")]
    bound = 10
    with Council(runner, fake_bin, roles) as c:
        c.launch()
        follower = c.follow(via_parent=True)
        follower.wait_for(r"dispatching", 20)
        pid = follower.pid()
        lines_before = len(follower.lines)
        follower.proc.kill()
        follower.proc.wait()
        latency = _wait_gone(pid, bound) if pid else None
        runner_code = c.wait_runner(60)
        passed = latency is not None and latency <= 6 and runner_code == 0
        if latency is None:
            detail = (f"follower still running {bound}s after its parent "
                      f"died; runner exit={runner_code}")
        else:
            detail = (f"follower exited {latency:.1f}s after its parent died "
                      f"(lines after: {len(follower.lines) - lines_before}); "
                      f"runner exit={runner_code}")
        return Outcome("S4", "follower's parent dies", passed, detail,
                       len(follower.lines))


def s5_silent_role(runner, fake_bin):
    roles = [_role("quiet", f"{SENTINELS['sleep_secs']}20."),
             _role("prompt", "Review the prompt lens.")]
    with Council(runner, fake_bin, roles) as c:
        c.launch()
        follower = c.follow()
        code = follower.wait_exit(90)
        runner_code = c.wait_runner(30)
        out = c.read("out.md")
        flagged = [ln for ln in follower.lines
                   if ln.startswith(FOLLOWER_NOTE) or "stall threshold" in ln]
        passed = (code == 0 and runner_code == 0 and not flagged
                  and "2/2 roles responded" in out)
        detail = (f"follower exit={code}; runner exit={runner_code}; "
                  f"both ok={'2/2 roles responded' in out}; "
                  f"anomaly lines={len(flagged)}")
        return Outcome("S5", "silent role, then success", passed, detail,
                       len(follower.lines))


def s6_malformed_lines(runner, fake_bin):
    roles = [_role("garbled", f"{SENTINELS['malformed_lines']}.")]
    with Council(runner, fake_bin, roles) as c:
        c.launch()
        follower = c.follow()
        code = follower.wait_exit(60)
        runner_code = c.wait_runner(30)
        reply = c.read("replies", "garbled.md")
        err = c.read("err.log")
        ok = "status=ok" in reply and "fake reply from codex" in reply
        crashed = "Traceback" in err or "crashed" in err
        passed = code == 0 and runner_code == 0 and ok and not crashed
        detail = (f"follower exit={code}; runner exit={runner_code}; "
                  f"reply ok={ok}; traceback or crash in err.log={crashed}")
        return Outcome("S6", "malformed stdout lines, chatty stderr", passed,
                       detail, len(follower.lines))


def _fake_survivors(c, timeout=10):
    """Recorded fake codex, holder, and tool pids still alive after
    `timeout`."""
    pids = c.recorded("exec") + c.recorded("holder") + c.recorded("tool")
    return [pid for pid in pids if _wait_gone(pid, timeout) is None]


def s7_tracked_stop(runner, fake_bin):
    roles = [_role("quick", "Review the quick lens."),
             _role("silent", f"{SENTINELS['sleep_secs']}300.")]
    with Council(runner, fake_bin, roles) as c:
        c.launch()
        follower = c.follow()
        settled = follower.wait_for(r"^\[codex-council\] \d/2 quick: ok ", 30)
        active = c.wait_status(_role_active("silent"), 15) is not None
        # What the host does at its background time limit: SIGTERM to the
        # tracked task's process group (the runner leads it).
        os.killpg(c.proc.pid, signal.SIGTERM)
        runner_code = c.wait_runner(30)
        code = follower.wait_exit(15)
        err = c.read("err.log")
        status = c.status() or {}
        runner_state = status.get("runner") or {}
        survivors = _fake_survivors(c)
        kept = "status=ok" in c.read("replies", "quick.md")
        passed = (settled is not None and active and runner_code == 143
                  and code == 0
                  and "[codex-council] interrupted by SIGTERM" in err
                  and runner_state.get("state") == "interrupted"
                  and runner_state.get("exit") == 143
                  and not survivors and kept)
        detail = (f"runner exit={runner_code}; follower exit={code}; "
                  f"status={runner_state.get('state')}/"
                  f"{runner_state.get('exit')}; settled reply kept={kept}; "
                  f"fake survivors={len(survivors)}")
        return Outcome("S7", "host stops a tracked launch", passed, detail,
                       len(follower.lines))


# Runs --start from a shell leading its own session, records that --start
# returned, then keeps the shell (and so its process group) alive.
# After --start returns, the wrapper ignores HUP and TERM (set only then, so
# --start and its supervisor inherit ordinary dispositions), so that all
# three signals S8 sends reach a live wrapper group; only SIGKILL ends it.
_WRAPPER = ('"$0" "$1" --start "$2" > "$3" 2>&1; rc=$?; trap \'\' HUP TERM; '
            'echo "$rc" > "$4"; exec sleep 60')


def _detached_done(c, timeout, run_dir=None):
    """(ended status, err.log, out.md) of a detached run once it ends."""
    status = c.wait_detached_end(timeout, run_dir) or {}
    return (status, c.read("err.log", run_dir=run_dir),
            c.read("out.md", run_dir=run_dir))


def _completed(status, err, out, total=1):
    runner_state = status.get("runner") or {}
    return (runner_state.get("state") == "done"
            and f"CODEX_COUNCIL_DONE ok={total} total={total} " in err
            and f"{total}/{total} roles responded" in out)


def s8_parent_death(runner, fake_bin):
    roles = [_role("steady", f"{SENTINELS['sleep_secs']}4.")]
    with Council(runner, fake_bin, roles) as c:
        started_out = os.path.join(c.base, "start.out")
        returned = os.path.join(c.base, "start.code")
        wrapper = subprocess.Popen(
            ["/bin/sh", "-c", _WRAPPER, PYTHON, runner, c.run_dir,
             started_out, returned],
            env=c.env, cwd=c.run_dir, stdin=subprocess.DEVNULL,
            start_new_session=True)
        try:
            deadline = time.monotonic() + 30
            while not os.path.exists(returned) and time.monotonic() < deadline:
                time.sleep(0.05)
            with open(returned, encoding="utf-8") as f:
                start_code = int(f.read().strip() or -1)
            record = c.supervisor() or {}
            active = c.wait_status(_role_active("steady"), 15) is not None
            # Each signal must reach the wrapper's live group: a failed
            # killpg, or a wrapper that died before SIGKILL, fails S8.
            delivered = []
            for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(wrapper.pid, sig)
                except OSError:
                    delivered.append(False)
                    continue
                time.sleep(0.2)
                delivered.append(sig == signal.SIGKILL
                                 or wrapper.poll() is None)
            wrapper.wait(10)
            delivered.append(wrapper.returncode == -signal.SIGKILL)
            status, err, out = _detached_done(c, 60)
        finally:
            with contextlib.suppress(OSError):
                os.killpg(wrapper.pid, signal.SIGKILL)
            wrapper.wait()
        runner_pid = (status.get("runner") or {}).get("pid")
        other_session = (record.get("sid") is not None
                         and record.get("sid") != wrapper.pid)
        passed = (start_code == 0 and active and all(delivered)
                  and _completed(status, err, out)
                  and other_session and runner_pid == record.get("pid"))
        detail = (f"--start exit={start_code}; wrapper group killed while the "
                  f"role ran={active}; HUP, TERM, KILL each reached the live "
                  f"group and KILL ended it={all(delivered)}; completed="
                  f"{_completed(status, err, out)}; supervisor sid="
                  f"{record.get('sid')} vs wrapper sid={wrapper.pid}; same "
                  f"runner={runner_pid == record.get('pid')}")
        return Outcome("S8", "--start's shell dies after it returns", passed,
                       detail, 0)


def s9_start_killed(runner, fake_bin):
    roles = [_role("steady", f"{SENTINELS['sleep_secs']}3.")]
    with Council(runner, fake_bin, roles) as c:
        launcher = subprocess.Popen(
            c.start_argv(), env=c.env, cwd=c.run_dir,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        child = None
        deadline = time.monotonic() + 20
        while child is None and launcher.poll() is None \
                and time.monotonic() < deadline:
            found = _children(launcher.pid)
            if found:
                child = found[0]
                with contextlib.suppress(OSError):
                    os.killpg(launcher.pid, signal.SIGKILL)
        launcher.wait()
        killed = launcher.returncode == -signal.SIGKILL
        status, err, out = _detached_done(c, 60)
        record = c.supervisor() or {}
        passed = (killed and child is not None and record.get("pid") == child
                  and _completed(status, err, out))
        detail = (f"--start killed right after spawning={killed}; "
                  f"supervisor pid matches its child="
                  f"{record.get('pid') == child}; completed="
                  f"{_completed(status, err, out)}")
        return Outcome("S9", "--start killed right after spawn", passed,
                       detail, 0)


def s10_concurrent_starts(runner, fake_bin):
    roles = [_role("single", "Review the single lens.")]
    rounds, problems = 5, []
    with Council(runner, fake_bin, roles) as c:
        for i in range(rounds):
            run_dir = c.stage(f"dup{i}")
            before = len(c.recorded("exec"))
            procs = [subprocess.Popen(
                c.start_argv(run_dir), env=c.env, cwd=run_dir,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True) for _ in range(2)]
            results = [(p.returncode, out, err) for p in procs
                       for out, err in [p.communicate(60)]]
            codes = sorted(code for code, _, _ in results)
            winner = next((r for r in results if r[0] == 0), None)
            loser = next((r for r in results if r[0] == 2), None)
            status, err_log, out = _detached_done(c, 60, run_dir)
            record = c.supervisor(run_dir) or {}
            execs = len(c.recorded("exec")) - before
            ok = (codes == [0, 2] and winner is not None and loser is not None
                  and "already holds a council launch" in loser[2]
                  and _started_pid(winner[1]) == record.get("pid")
                  and (status.get("runner") or {}).get("pid")
                  == record.get("pid")
                  and err_log.startswith("[codex-council] dispatching ")
                  and err_log.count("CODEX_COUNCIL_DONE") == 1
                  and _completed(status, err_log, out) and execs == 1)
            if not ok:
                problems.append(f"round {i}: exits={codes}; codex runs="
                                f"{execs}; completed="
                                f"{_completed(status, err_log, out)}")
    detail = ("; ".join(problems) if problems else
              f"{rounds} rounds: one exit 0 and one exit 2 each, one "
              "supervisor and one codex run, nothing truncated")
    return Outcome("S10", "concurrent --start on one directory",
                   not problems, detail, 0)


def s11_cancel(runner, fake_bin):
    roles = [_role("hanger", f"{SENTINELS['tool_session']}.",
                   f"{SENTINELS['sleep_secs']}300.")]
    with Council(runner, fake_bin, roles) as c:
        start = c.start()
        active = c.wait_status(_role_active("hanger"), 20) is not None
        tools = c.recorded("tool", timeout=10)
        cancel = c.cli("--cancel")
        status = c.status() or {}
        runner_state = status.get("runner") or {}
        err = c.read("err.log")
        lock = c.lock()
        survivors = _fake_survivors(c)
        passed = (start.returncode == 0 and active and bool(tools)
                  and cancel.returncode == 0
                  and "[codex-council] interrupted by SIGTERM" in err
                  and runner_state.get("state") == "interrupted"
                  and runner_state.get("exit") == 143 and lock == "free"
                  and not survivors)
        detail = (f"--cancel exit={cancel.returncode}; status="
                  f"{runner_state.get('state')}/{runner_state.get('exit')}; "
                  f"lock={lock}; tool sessions={len(tools)}; fake survivors="
                  f"{len(survivors)}")
        return Outcome("S11", "--cancel a hanging role", passed, detail, 0)


def s12_supervisor_killed(runner, fake_bin):
    roles = [_role("sleeper", f"{SENTINELS['sleep_secs']}300.")]
    with Council(runner, fake_bin, roles) as c:
        start = c.start()
        c.wait_status(_role_active("sleeper"), 20)
        codex = c.recorded("exec", timeout=10)
        pid = (c.supervisor() or {}).get("pid")
        os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 2
        while c.lock() != "free" and time.monotonic() < deadline:
            time.sleep(0.02)
        freed = c.lock() == "free"
        orphan = bool(codex) and _alive(codex[0])
        status = c.cli("--status")
        follower = c.follow()
        follow_code = follower.wait_exit(30)
        reap = c.cli("--reap")
        reaped = bool(codex) and _wait_gone(codex[0], 10) is not None
        again = c.start()
        fresh_dir = c.stage("fresh", [_role("fresh", "Review the lens.")])
        fresh = c.start(fresh_dir)
        fresh_status, fresh_err, fresh_out = _detached_done(c, 60, fresh_dir)
        first = (status.stdout.splitlines() or [""])[0]
        passed = (start.returncode == 0 and freed and orphan
                  and first.startswith("runner: gone ")
                  and f"live codex groups: {codex[0]} (sleeper)"
                  in status.stdout
                  and follow_code == 4 and reap.returncode == 0 and reaped
                  and again.returncode == 2 and fresh.returncode == 0
                  and _completed(fresh_status, fresh_err, fresh_out))
        detail = (f"lock free at once={freed}; codex orphaned={orphan}; "
                  f"--status {first!r}; --follow exit={follow_code}; --reap "
                  f"exit={reap.returncode} (codex ended={reaped}); --start "
                  f"here exit={again.returncode}; new directory exit="
                  f"{fresh.returncode}")
        return Outcome("S12", "only the supervisor is SIGKILLed", passed,
                       detail, len(follower.lines))


SCENARIOS = {
    "S0": s0_happy_path,
    "S1": s1_held_pipe,
    "S2": s2_runner_killed,
    "S3": s3_runner_stopped,
    "S4": s4_follower_orphaned,
    "S5": s5_silent_role,
    "S6": s6_malformed_lines,
    "S7": s7_tracked_stop,
    "S8": s8_parent_death,
    "S9": s9_start_killed,
    "S10": s10_concurrent_starts,
    "S11": s11_cancel,
    "S12": s12_supervisor_killed,
}


def run_scenarios(runner=RUNNER, only=None):
    """Run the selected scenarios concurrently; return their Outcomes."""
    names = [name for name in SCENARIOS if not only or name in only]
    with tempfile.TemporaryDirectory() as fake_bin:
        fake_codex.install(fake_bin)
        with concurrent.futures.ThreadPoolExecutor(len(names)) as pool:
            futures = {name: pool.submit(SCENARIOS[name], runner, fake_bin)
                       for name in names}
            outcomes = []
            for name in names:
                try:
                    outcomes.append(futures[name].result())
                except Exception as e:  # a harness defect is a failure too
                    outcomes.append(Outcome(name, "scenario error", False,
                                            f"{type(e).__name__}: {e}", 0))
    return outcomes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runner", default=RUNNER,
                        help="codex_council.py to exercise")
    parser.add_argument("--only", default="",
                        help="comma-separated scenario ids (default: all)")
    args = parser.parse_args(argv)
    only = {name.strip() for name in args.only.split(",") if name.strip()}
    outcomes = run_scenarios(os.path.abspath(args.runner), only)
    for outcome in outcomes:
        print(outcome.line(), flush=True)
    return 0 if all(o.passed for o in outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
