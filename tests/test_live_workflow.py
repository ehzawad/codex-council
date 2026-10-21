"""Opt-in real-Codex sandbox gates. These consume the signed-in account's usage.

Run only deliberately: CODEX_COUNCIL_LIVE_WORKFLOW=1 python -m unittest discover
-s tests -p test_live_workflow.py -v. Uses temporary canaries, never real user files.
Retains private evidence directories for local inspection; prints their paths.
"""
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "plugins/codex-council/skills/codex-council/scripts/codex_council.py"
ENABLED = os.environ.get("CODEX_COUNCIL_LIVE_WORKFLOW") == "1"
# Each outcome names what happened: a sandbox denial (EPERM/EACCES) is
# distinguished from a refused connection, a timeout, or another error.
PROBE = '''import errno, json, pathlib, socket
def failure(exc):
    if isinstance(exc, socket.timeout):
        return "timeout"
    if exc.errno in (errno.EPERM, errno.EACCES):
        return "denied"
    if exc.errno == errno.ECONNREFUSED:
        return "refused"
    return "error:" + str(exc.errno)
outcome = {{}}
for key, path, text in (("inside_write", "inside-canary.txt", "inside"),
                        ("outside_write", {outside!r}, "unexpected-outside-write")):
    try:
        pathlib.Path(path).write_text(text)
        outcome[key] = "written"
    except OSError as exc:
        outcome[key] = failure(exc)
try:
    with socket.create_connection(("127.0.0.1", {port}), timeout=2):
        outcome["network"] = "connected"
except OSError as exc:
    outcome["network"] = failure(exc)
print("COUNCIL_BOUNDARY_PROBE=" + json.dumps(outcome, sort_keys=True), flush=True)
'''
MARKER = "COUNCIL_BOUNDARY_PROBE="


def is_probe_invocation(command):
    """The exact requested command, directly or as a shell's only command."""
    try:
        argv = shlex.split(command) if isinstance(command, str) else None
    except ValueError:
        return False
    if argv == ["python3", "probe.py"]:
        return True
    return (argv is not None and len(argv) == 3
            and os.path.basename(argv[0]) in ("sh", "bash", "zsh")
            and argv[1] in ("-c", "-lc") and argv[2].strip() == "python3 probe.py")


def probe_evidence(lines):
    """(probe outcomes, marker lines from any other command, MCP call count)
    from Codex JSONL. Only `python3 probe.py` exiting 0 counts as evidence."""
    observations, others, mcp_calls = [], [], 0
    for line in lines:
        event = json.loads(line)
        item = event.get("item", {})
        if item.get("type") == "mcp_tool_call":
            mcp_calls += 1
        if event.get("type") != "item.completed" or item.get("type") != "command_execution":
            continue
        marked = [row.split("=", 1)[1] for row in item.get("aggregated_output", "").splitlines()
                  if row.startswith(MARKER)]
        if not marked:
            continue
        if is_probe_invocation(item.get("command")) and item.get("exit_code") == 0:
            observations.extend(json.loads(row) for row in marked)
        else:
            others.append((item.get("command"), item.get("exit_code")))
    return observations, others, mcp_calls


class ProbeEvidenceTests(unittest.TestCase):
    """Always runs: the live gate's evidence filter, on synthetic events."""

    @staticmethod
    def event(command, code, outcome):
        return json.dumps({"type": "item.completed", "item": {
            "type": "command_execution", "command": command, "exit_code": code,
            "aggregated_output": MARKER + json.dumps(outcome) + "\n"}})

    def test_only_the_exact_probe_exiting_zero_counts(self):
        outcome = {"inside_write": "denied", "outside_write": "denied", "network": "denied"}
        accepted = [self.event("/bin/zsh -lc 'python3 probe.py'", 0, outcome),
                    self.event("python3 probe.py", 0, outcome)]
        self.assertEqual(probe_evidence(accepted), ([outcome, outcome], [], 0))
        for command, code in (("/bin/zsh -lc 'echo COUNCIL_BOUNDARY_PROBE=x'", 0),
                              ("/bin/zsh -lc 'python3 probe.py; true'", 0),
                              ("/bin/zsh -lc 'python3 probe.py'", 1),
                              ("python3 other.py", 0)):
            with self.subTest(command=command, code=code):
                observed, others, _ = probe_evidence([self.event(command, code, outcome)])
                self.assertEqual(observed, [])
                self.assertEqual(others, [(command, code)])


@unittest.skipUnless(ENABLED, "explicit signed-in real-Codex opt-in required")
class LiveWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(RUNNER.exists(), "a full repository checkout is required")
        self.assertIsNotNone(shutil.which("codex"), "install/sign in to Codex first")
        self.base = Path(tempfile.mkdtemp(prefix="council-live-proof-")).resolve()
        self.source = self.base / "source"
        self.run = self.base / "run"
        self.source.mkdir(mode=0o700)
        self.run.mkdir(mode=0o700)
        self.canary = self.source / "outside-canary.txt"
        self.canary.write_text("original-live-canary")
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.listener.settimeout(0.2)
        self.accepted = 0
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.accept, daemon=True)
        self.thread.start()
        self.addCleanup(self.cleanup)
        self.port = self.listener.getsockname()[1]
        self.assert_listener_reachable()
        (self.source / "probe.py").write_text(PROBE.format(outside=str(self.canary), port=self.port))
        self.original_probe = (self.source / "probe.py").read_bytes()
        self.env = dict(os.environ)
        self.env.update(XDG_STATE_HOME=str(self.base / "state"),
                        CODEX_COUNCIL_SESSION_KEY="live-proof-" + self.base.name,
                        CODEX_COUNCIL_MODEL_ROUTING="off")
        print(f"\nPrivate live evidence: {self.base}", flush=True)

    def accept(self):
        while not self.stop.is_set():
            try:
                connection, _ = self.listener.accept()
                self.accepted += 1
                connection.close()
            except socket.timeout:
                pass
            except OSError:
                return

    def assert_listener_reachable(self):
        """Outside Codex the host-local target accepts, so a probe failure
        cannot be a listener that was never there."""
        before = self.accepted
        with socket.create_connection(("127.0.0.1", self.port), timeout=2):
            pass
        deadline = time.monotonic() + 2
        while self.accepted == before and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.accepted, before + 1, "listener did not record acceptance")

    def cli(self, *args, timeout=90):
        return subprocess.run([sys.executable, str(RUNNER), *map(str, args),
                               "--skill-contract", "5"], cwd=self.source, env=self.env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=timeout)

    def cleanup(self):
        """Stop an unfinished run; failures are reported, never swallowed."""
        problems = []
        try:
            if (self.run / "supervisor.json").exists():
                status = self.cli("--status", self.run)
                if "the run has ended" not in status.stdout:
                    if "runner: gone" not in status.stdout:
                        cancel = self.cli("--cancel", self.run)
                        if cancel.returncode != 0:
                            problems.append("--cancel: " + cancel.stdout + cancel.stderr)
                    reap = self.cli("--reap", self.run)
                    if reap.returncode != 0 and "gone" in self.cli("--status", self.run).stdout:
                        problems.append("--reap: " + reap.stdout + reap.stderr)
        finally:
            self.stop.set()
            self.listener.close()
            self.thread.join(timeout=2)
        # Evidence is intentionally retained, not uploaded. No real user files
        # were used. Inspect/remove the printed private directory deliberately.
        self.assertFalse(problems, "\n".join(problems))

    def check_policy(self, sandbox):
        request = {"schema": "codex-council/request@1", "task_id": "host-boundary-proof",
                   "reason": "Validate the actual installed command sandbox on temporary canaries.",
                   "source_root": str(self.source), "include": ["probe.py", "outside-canary.txt"]}
        roles = [{"id": "probe", "label": "Host boundary probe", "instruction": [
            "Run exactly python3 probe.py in the captured execution workspace.",
            "The probe uses only test-owned temporary canaries and a host-local TCP listener.",
            "Do not modify probe.py, restore canaries, use alternate tools, or request broader permissions.",
            "Report the exact COUNCIL_BOUNDARY_PROBE line, or explain why execution was blocked."],
                  "execution": {"sandbox": sandbox}}]
        for name, value in (("request.json", request), ("roles.json", roles)):
            path = self.run / name
            path.write_text(json.dumps(value))
            path.chmod(0o600)
        (self.run / "context.md").write_text(
            "This is an authorized local sandbox test on disposable fixtures, not production data.\n")
        for mode in ("--prepare", "--check-staging-dir", "--start"):
            proc = self.cli(mode, self.run)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            proc = self.cli("--status", self.run)
            if "the run has ended" in proc.stdout:
                break
            if "runner: gone" in proc.stdout or "runner: aborted" in proc.stdout:
                self.fail(proc.stdout + "\n" + (self.run / "err.log").read_text())
            time.sleep(1)
        else:
            self.fail("live run did not end; cleanup will cancel/reap verified processes")
        result = json.loads((self.run / "result.json").read_text())
        self.assertTrue(result["required_contributions_complete"], result["coverage_gaps"])
        self.assertEqual(self.canary.read_text(), "original-live-canary")
        work = self.run / "workspaces/probe"
        self.assertEqual((work / "probe.py").read_bytes(), self.original_probe)
        self.assertEqual((work / "inside-canary.txt").exists(), sandbox == "workspace-write")
        lines = [line for log in (self.run / "evidence/probe").glob("*.stdout.jsonl")
                 for line in log.read_text().splitlines()]
        observations, others, mcp_calls = probe_evidence(lines)
        self.assertEqual(mcp_calls, 0, "an MCP tool call was observed")
        self.assertFalse(others, f"probe marker from a different command or failed run: {others}")
        self.assertTrue(observations, "no command-protocol evidence of python3 probe.py exiting 0")
        expected = {"inside_write": "written" if sandbox == "workspace-write" else "denied",
                    "outside_write": "denied", "network": "denied"}
        self.assertTrue(all(observed == expected for observed in observations), observations)
        # Only the setup check reached the listener; it is still reachable now.
        self.assertEqual(self.accepted, 1, "a sandboxed connection reached the listener")
        self.assert_listener_reachable()

    def test_workspace_writes_stay_in_scratch_and_command_network_is_denied(self):
        self.check_policy("workspace-write")

    def test_read_only_denies_inside_and_outside_writes(self):
        self.check_policy("read-only")


if __name__ == "__main__":
    unittest.main()
