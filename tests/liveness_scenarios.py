#!/usr/bin/env python3
"""Liveness scenarios: the real runner CLI end to end against a fake codex.

Each scenario stages a private run directory, launches the council the way
SKILL.md does (stdout to out.md, stderr to err.log) with the fake `codex`
from fake_codex.py first on PATH, follows it with `--follow`, disturbs it,
and prints one verdict line with the number of lines the follower emitted:

  S0  happy path: three roles succeed.
  S1  a descendant of codex holds codex's stdout open after codex exits.
  S2  the runner is SIGKILLed mid-run while codex keeps running.
  S3  the runner is alive but its event loop is blocked (SIGSTOP).
  S4  the follower's parent process dies.
  S5  a role is byte-silent for a while, then succeeds.
  S6  a role writes stdout lines no JSON parser accepts while its stderr
      keeps printing, then succeeds.

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
        self.base = tempfile.mkdtemp(prefix="council-liveness-")
        self.run_dir = os.path.join(self.base, "run")
        os.mkdir(self.run_dir, 0o700)
        self.pid_dir = os.path.join(self.base, "pids")
        os.mkdir(self.pid_dir)
        with open(os.path.join(self.run_dir, "roles.json"), "w",
                  encoding="utf-8") as f:
            json.dump(roles, f)
        with open(os.path.join(self.run_dir, "context.md"), "w",
                  encoding="utf-8") as f:
            f.write("Liveness scenario context.\n")
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

    def path(self, *parts):
        return os.path.join(self.run_dir, *parts)

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

    def cli(self, *args):
        return subprocess.run([PYTHON, self.runner, *args, self.run_dir],
                              capture_output=True, text=True, env=self.env,
                              timeout=60)

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

    def status(self):
        try:
            with open(self.path("status.json"), encoding="utf-8") as f:
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

    def read(self, *parts):
        try:
            with open(self.path(*parts), encoding="utf-8") as f:
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
        for pid in self.recorded("exec") + self.recorded("holder"):
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


SCENARIOS = {
    "S0": s0_happy_path,
    "S1": s1_held_pipe,
    "S2": s2_runner_killed,
    "S3": s3_runner_stopped,
    "S4": s4_follower_orphaned,
    "S5": s5_silent_role,
    "S6": s6_malformed_lines,
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
