"""Opt-in real-Codex sandbox gates. These consume the signed-in account's usage.

Run only deliberately: CODEX_COUNCIL_LIVE_WORKFLOW=1 python -m unittest discover
-s tests -p test_live_workflow.py -v. Uses temporary canaries, never real user files.
Retains private evidence directories for local inspection; prints their paths.
"""
import contextlib
import json
import os
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
PROBE = '''import json, pathlib, socket
outcome = {{}}
try:
    pathlib.Path("inside-canary.txt").write_text("inside")
    outcome["inside_write"] = True
except PermissionError:
    outcome["inside_write"] = False
try:
    pathlib.Path({outside!r}).write_text("unexpected-outside-write")
    outcome["outside_denied"] = False
except PermissionError:
    outcome["outside_denied"] = True
try:
    with socket.create_connection(("127.0.0.1", {port}), timeout=2) as sock:
        outcome["command_network_denied"] = False
except OSError:
    outcome["command_network_denied"] = True
print("COUNCIL_BOUNDARY_PROBE=" + json.dumps(outcome, sort_keys=True), flush=True)
'''


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
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.accept, daemon=True)
        self.thread.start()
        self.addCleanup(self.cleanup)
        self.port = self.listener.getsockname()[1]
        with socket.create_connection(("127.0.0.1", self.port), timeout=2):
            pass  # establish that this host-local target is reachable outside Codex
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
                connection.close()
            except socket.timeout:
                pass
            except OSError:
                return

    def cli(self, *args, timeout=90):
        return subprocess.run([sys.executable, str(RUNNER), *map(str, args),
                               "--skill-contract", "5"], cwd=self.source, env=self.env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=timeout)

    def cleanup(self):
        if (self.run / "supervisor.json").exists():
            with contextlib.suppress(Exception):
                self.cli("--cancel", self.run)
            with contextlib.suppress(Exception):
                self.cli("--reap", self.run)
        self.stop.set()
        self.listener.close()
        self.thread.join(timeout=2)
        # Evidence is intentionally retained, not uploaded. No real user files
        # were used. Inspect/remove the printed private directory deliberately.

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
        observations = []
        for log in (self.run / "evidence/probe").glob("*.stdout.jsonl"):
            for line in log.read_text().splitlines():
                event = json.loads(line)
                item = event.get("item", {})
                if event.get("type") == "item.completed" and item.get("type") == "command_execution":
                    output = item.get("aggregated_output", "")
                    for output_line in output.splitlines():
                        if output_line.startswith("COUNCIL_BOUNDARY_PROBE="):
                            observations.append(json.loads(output_line.split("=", 1)[1]))
                self.assertNotEqual(item.get("type"), "mcp_tool_call")
        self.assertTrue(observations, "no actual command-protocol evidence of the probe")
        expected = {"inside_write": sandbox == "workspace-write", "outside_denied": True,
                    "command_network_denied": True}
        self.assertTrue(all(observed == expected for observed in observations), observations)

    def test_workspace_writes_stay_in_scratch_and_command_network_is_denied(self):
        self.check_policy("workspace-write")

    def test_read_only_denies_inside_and_outside_writes(self):
        self.check_policy("read-only")


if __name__ == "__main__":
    unittest.main()
