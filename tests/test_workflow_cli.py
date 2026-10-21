"""The real council CLI/supervisor with a fake Codex protocol, never paid inference.

These tests prove capture, routing, argv, continuity, evidence and cancellation
integration. They do not claim the fake enforces a real OS sandbox.
"""
import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/codex-council/skills/codex-council/scripts"
RUNNER = SCRIPTS / "codex_council.py"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests"))

# The authoring environment can run pure-module tests without a full checkout.
# CI and every clone contain the runner and must run this integration suite.
HAS_RUNNER = RUNNER.exists()
if HAS_RUNNER:
    import codex_council  # noqa: E402
    import council_liveness  # noqa: E402
    import council_testlib  # noqa: E402


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


@unittest.skipUnless(HAS_RUNNER, "integration requires the runner checkout")
class WorkflowCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / "source"
        self.source.mkdir(mode=0o700)
        (self.source / "subject.py").write_text("value = 1\n")
        self.bin = self.base / "bin"
        self.bin.mkdir()
        executable = self.bin / "codex"
        # Absolute interpreter makes the fixture independent of user PATH shims.
        executable.write_text(f"#!{sys.executable}\n" +
            (ROOT / "tests/fake_workflow_codex.py").read_text().split("\n", 1)[1])
        executable.chmod(0o700)
        self.log = self.base / "calls.jsonl"
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("CODEX_", "FAKE_CODEX_", "FAKE_WORKFLOW_"))}
        self.env.update(PATH=str(self.bin) + os.pathsep + self.env.get("PATH", ""),
                        XDG_STATE_HOME=str(self.base / "state"),
                        CODEX_HOME=str(self.base / "home"),
                        CODEX_COUNCIL_SESSION_KEY="integration-session",
                        CODEX_COUNCIL_MODEL_ROUTING="off",
                        FAKE_WORKFLOW_LOG=str(self.log))
        self.runs = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for directory in self.runs:
            if (directory / "supervisor.json").exists():
                with contextlib.suppress(Exception):
                    self.cli("--cancel", directory)
                with contextlib.suppress(Exception):
                    self.cli("--reap", directory)
        for directory, dirs, files in os.walk(self.base):
            for name in dirs:
                path = Path(directory) / name
                if not path.is_symlink():
                    path.chmod(0o700)
            for name in files:
                path = Path(directory) / name
                if not path.is_symlink() and path.is_file():
                    path.chmod(0o600)
        self.temp.cleanup()

    def cli(self, *args, timeout=60):
        return subprocess.run([sys.executable, str(RUNNER), *map(str, args),
                               "--skill-contract", str(codex_council.SKILL_CONTRACT_EPOCH)],
                              cwd=self.source, env=self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout)

    def stage(self, instruction="Inspect the current contribution.", *, roles=None,
              task="task-one", execution=None):
        directory = self.base / f"run-{len(self.runs)}"
        directory.mkdir(mode=0o700)
        self.runs.append(directory)
        roles = roles if roles is not None else [{
            "id": "contribution", "label": "Current contribution", "instruction": [instruction],
            "execution": execution or {"sandbox": "workspace-write"}}]
        write(directory / "roles.json", roles)
        write(directory / "request.json", {
            "schema": "codex-council/request@1", "task_id": task,
            "reason": "This experiment can distinguish the current explanations.",
            "source_root": str(self.source), "include": ["."]})
        (directory / "context.md").write_text("Current goal: establish what the captured input supports.\n")
        proc = self.cli("--prepare", directory)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return directory

    def launch(self, directory):
        """--start's supervised child in the foreground (its exit code and
        streams), the runner's only path to workers."""
        return council_testlib.run_supervised(str(directory), self.env, self.source)

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def execs(self):
        return [call for call in self.calls() if call["method"] == "exec"]

    def result(self, directory):
        return json.loads((directory / "result.json").read_text())

    def test_experiment_records_red_green_evidence_without_live_edits(self):
        directory = self.stage("EXPERIMENT_FIXTURE: reproduce, change the copy, rerun.")
        preflight = self.cli("--check-staging-dir", directory)
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        self.assertIn("context_bytes=", preflight.stdout)
        self.assertIn("scratch-command-writes-only", preflight.stdout)
        proc = self.launch(directory)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((self.source / "subject.py").read_text(), "value = 1\n")
        result = self.result(directory)
        self.assertTrue(result["required_contributions_complete"], result)
        self.assertEqual(result["applicability"]["state"], "unchanged")
        contribution = result["roles"]["contribution"]
        commands = contribution["attempts"][0]["commands"]
        self.assertEqual([item["reported_exit_code"] for item in commands], [1, 0])
        self.assertEqual(contribution["interpretation"]["source"], "collaborator")
        self.assertEqual(contribution["attempts"][0]["reported_usage"]["output_tokens"], 10)
        self.assertTrue((directory / "evidence/contribution/candidate.diff").exists())
        self.assertIn("result.json", proc.stdout)
        self.assertEqual(self.execs()[0]["cwd"], str(directory / "workspaces/contribution"))
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", self.execs()[0]["argv"])

    def test_empty_source_directory_exists_in_scratch_and_its_deletion_is_stale(self):
        (self.source / "output").mkdir()
        directory = self.stage()
        copied = directory / "workspaces/contribution"
        command = [sys.executable, "-c",
                   "from pathlib import Path; Path('output/result.txt').write_text('fixture')"]
        self.assertEqual(subprocess.run(command, cwd=copied).returncode, 0)
        self.assertEqual(json.loads(self.cli("--freshness", directory).stdout)["state"], "unchanged")
        (self.source / "output").rmdir()
        stale = self.cli("--freshness", directory)
        self.assertEqual(stale.returncode, 1, stale.stdout)
        self.assertIn("output/", json.loads(stale.stdout)["changed_paths"])

    def test_failed_preparation_never_falls_through_to_a_launch(self):
        directory = self.base / "run-unprepared"
        directory.mkdir(mode=0o700)
        self.runs.append(directory)
        write(directory / "roles.json", [{"id": "contribution", "label": "Current contribution",
                                          "instruction": ["Inspect the current question."]}])
        write(directory / "request.json", {"schema": "codex-council/request@1",
                                           "task_id": "task-one", "source_root": str(self.source),
                                           "include": ["."], "reason": ""})
        (directory / "context.md").write_text("Current captured task.\n")
        self.assertEqual(self.cli("--prepare", directory).returncode, 2)
        for mode in ("--discover", "--check-staging-dir", "--start"):
            proc = self.cli(mode, directory)
            self.assertEqual(proc.returncode, 2, (mode, proc.stdout, proc.stderr))
            self.assertIn("no completed capture", proc.stderr)
        self.assertFalse((directory / "supervisor.lock").exists())
        self.assertFalse(self.execs())

    def test_there_is_no_direct_launch(self):
        directory = self.stage()
        proc = self.cli("--roles-file", directory / "roles.json",
                        "--context-file", directory / "context.md")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("there is no direct launch", proc.stderr)
        self.assertFalse(self.execs())

    def test_a_prepared_run_executes_at_most_once(self):
        directory = self.stage()
        first = self.launch(directory)
        self.assertEqual(first.returncode, 0, first.stderr)
        before = (directory / "result.json").read_bytes()
        for name in ("supervisor.lock", "supervisor.json"):
            (directory / name).unlink()  # even with --start's own claim reset
        second = self.launch(directory)
        self.assertEqual(second.returncode, 2, second.stderr)
        self.assertIn("already executed", second.stderr)
        self.assertEqual(len(self.execs()), 1)
        self.assertEqual((directory / "result.json").read_bytes(), before)

    def test_default_permissions_are_read_only_and_external_tools_disabled(self):
        directory = self.stage(roles=[{"id": "view", "label": "Perspective",
                                      "instruction": ["Investigate the current question."]}])
        proc = self.launch(directory)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = self.execs()[0]["argv"]
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        # An execpolicy allow rule would run its command outside the sandbox.
        self.assertIn("--ignore-rules", argv)
        self.assertIn('approval_policy="never"', argv)
        self.assertIn("mcp_servers.fixture-server.enabled=false", argv)
        self.assertFalse(any('"' in arg for arg in argv if arg.startswith("mcp_servers.")))
        self.assertIn('web_search="disabled"', argv)
        self.assertIn('features.apps=false', argv)

    def test_unaddressable_mcp_name_blocks_before_inference(self):
        directory = self.stage()
        self.env["FAKE_WORKFLOW_MCP"] = "fixture.server"
        proc = self.cli("--check-staging-dir", directory)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("cannot be disabled with a Codex -c override", proc.stderr)
        self.assertFalse(self.execs())

    def test_managed_requirements_block_before_inference(self):
        directory = self.stage()
        self.env["FAKE_WORKFLOW_REQUIREMENTS"] = json.dumps({"allowedSandboxModes": ["read-only"]})
        proc = self.cli("--check-staging-dir", directory)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("managed requirements forbid the requested policy", proc.stderr)
        self.assertFalse(self.execs())

    def test_cancelled_preflight_tears_down_its_policy_app_server(self):
        directory = self.stage()
        pidfile = self.base / "policy-server.pid"
        self.env["FAKE_WORKFLOW_CONFIG_PAUSE"] = str(pidfile)
        process = subprocess.Popen(
            [sys.executable, str(RUNNER), "--check-staging-dir", str(directory)],
            cwd=self.source, env=self.env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        server = None
        try:
            deadline = time.monotonic() + 15
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(pidfile.exists(), "policy probe never started")
            server = int(pidfile.read_text())
            process.terminate()
            _, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 128 + signal.SIGTERM, stderr)
            self.assertIn("--check-staging-dir interrupted by SIGTERM", stderr)
            deadline = time.monotonic() + 5
            while self._alive(server) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(self._alive(server), "policy app-server outlived preflight")
        finally:
            if server is not None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(server, signal.SIGKILL)
            if process.poll() is None:
                process.kill()
                process.wait()

    @staticmethod
    def _alive(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def test_policy_mismatch_fails_before_inference_without_fallback(self):
        directory = self.stage()
        self.env["FAKE_WORKFLOW_POLICY_REFUSE"] = "1"
        proc = self.cli("--check-staging-dir", directory)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("requested execution policy", proc.stderr)
        self.assertFalse(self.execs())
        proc = self.launch(directory)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertFalse(self.execs())

    def test_transient_failures_after_tool_work_are_not_replayed_fresh_or_resumed(self):
        for resume in (False, True):
            for status, tag in ((503, "5xx"), (429, "rate-limit")):
                with self.subTest(resume=resume, status=status):
                    task = f"retry-proof-{resume}-{status}"
                    if resume:
                        seed = self.stage(task=task)
                        self.assertEqual(self.launch(seed).returncode, 0)
                    count = len(self.execs())
                    directory = self.stage(
                        f"SIDE_EFFECT_{status}: exercise failure recovery.", task=task,
                        execution={"sandbox": "workspace-write",
                                   "continuity": "resume" if resume else "fresh"})
                    proc = self.launch(directory)
                    self.assertEqual(proc.returncode, 1, proc.stderr)
                    calls = self.execs()[count:]
                    self.assertEqual(len(calls), 1)
                    self.assertEqual("resume" in calls[0]["argv"], resume)
                    self.assertIn(f"[replay-blocked:{tag}]", proc.stdout)
                    effects = directory / "workspaces/contribution/effects.txt"
                    self.assertEqual(effects.read_text(), "effect\n")
                    self.assertFalse(self.result(directory)["required_contributions_complete"])

    def test_stale_looking_failure_after_tool_work_keeps_thread_and_never_restarts(self):
        first = self.stage()
        self.assertEqual(self.launch(first).returncode, 0)
        thread = self.execs()[-1]["thread"]
        count = len(self.execs())
        directory = self.stage("SIDE_EFFECT_STALE: exercise resume recovery.",
                               execution={"sandbox": "workspace-write", "continuity": "resume"})
        proc = self.launch(directory)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("[replay-blocked:stale]", proc.stdout)
        self.assertEqual(len(self.execs()[count:]), 1)
        self.assertEqual((directory / "workspaces/contribution/effects.txt").read_text(), "effect\n")
        next_run = self.stage(execution={"continuity": "resume"})
        self.assertEqual(self.launch(next_run).returncode, 0)
        self.assertEqual(self.execs()[-1]["thread"], thread)

    def test_writable_role_is_never_replayed_on_an_absent_tool_item(self):
        directory = self.stage("HIDDEN_TOOL_503: exercise failure recovery.")
        proc = self.launch(directory)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(len(self.execs()), 1)
        self.assertIn("[replay-blocked:5xx]", proc.stdout)
        effects = directory / "workspaces/contribution/effects.txt"
        self.assertEqual(effects.read_text(), "effect\n")

    def test_transient_failure_before_tools_retries_with_separate_evidence(self):
        directory = self.stage("TRANSIENT_BEFORE_TOOLS: inspect.",
                               execution={"sandbox": "read-only"})
        proc = self.launch(directory)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.execs()), 2)
        result = self.result(directory)["roles"]["contribution"]
        self.assertEqual(len(result["attempts"]), 2)
        self.assertEqual([a["transport_exit"] for a in result["attempts"]], [1, 0])

    def test_stale_source_is_detected_without_rewriting_captured_inputs(self):
        directory = self.stage()
        before = (directory / "baseline/subject.py").read_bytes()
        (self.source / "subject.py").write_text("value = 99\n")
        proc = self.launch(directory)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((directory / "baseline/subject.py").read_bytes(), before)
        self.assertEqual(self.result(directory)["applicability"]["state"], "stale")
        status = self.cli("--freshness", directory)
        self.assertEqual(status.returncode, 1, status.stderr)
        self.assertEqual(json.loads(status.stdout)["changed_paths"], ["subject.py"])

    def test_fresh_resume_and_new_task_do_not_confuse_scratch_with_identity(self):
        first = self.stage(execution={"continuity": "fresh"})
        self.assertEqual(self.launch(first).returncode, 0)
        old = self.execs()[-1]["thread"]
        second = self.stage(execution={"continuity": "resume"})
        self.assertEqual(self.launch(second).returncode, 0)
        self.assertEqual(self.execs()[-1]["thread"], old)
        resumed = self.execs()[-1]["argv"]
        self.assertLess(resumed.index("--ignore-rules"), resumed.index("resume"))
        self.assertNotEqual(self.execs()[-1]["cwd"], self.execs()[0]["cwd"])
        third = self.stage(execution={"continuity": "fresh"})
        self.assertEqual(self.launch(third).returncode, 0)
        self.assertNotEqual(self.execs()[-1]["thread"], old)
        fourth = self.stage(task="new-task", execution={"continuity": "resume"})
        self.assertEqual(self.launch(fourth).returncode, 0)
        self.assertNotIn("resume", self.execs()[-1]["argv"])

    def test_required_coverage_is_separate_from_some_role_responding(self):
        roles = [{"id": "good", "label": "A", "instruction": ["Contribute."],
                  "execution": {"required": True}},
                 {"id": "bad", "label": "B", "instruction": ["AUTH_FAILURE"],
                  "execution": {"required": True}}]
        directory = self.stage(roles=roles)
        proc = self.launch(directory)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("1/2 roles responded", proc.stdout)
        self.assertFalse(self.result(directory)["required_contributions_complete"])
        roles[1]["execution"]["required"] = False
        next_run = self.stage(roles=roles)
        self.assertEqual(self.launch(next_run).returncode, 0)
        self.assertTrue(self.result(next_run)["required_contributions_complete"])

    def test_clean_exit_without_a_completed_turn_is_not_complete_coverage(self):
        directory = self.stage("UNFINISHED_TURN: start the required check.")
        proc = self.launch(directory)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        result = self.result(directory)
        contribution = result["roles"]["contribution"]
        self.assertFalse(contribution["attempts"][-1]["turn_completed_observed"])
        self.assertIn("turn-completion-not-observed", contribution["coverage_gaps"])
        self.assertFalse(result["required_contributions_complete"])
        self.assertIn("contribution:required-contribution-incomplete", result["coverage_gaps"])
        # The partial reply and raw evidence are preserved for inspection.
        self.assertEqual(contribution["interpretation"]["text"],
                         "I am about to run the required check.")

    def test_discovery_and_execution_share_each_actual_workspace(self):
        self.env["CODEX_COUNCIL_MODEL_ROUTING"] = "auto"
        directory = self.stage()
        proc = self.cli("--discover", directory)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        catalog = json.loads((directory / "catalogs/contribution/model-snapshot.json").read_text())
        roles = json.loads((directory / "roles.json").read_text())
        roles[0].update(model="fixture-collaborator", effort="brisk",
                        selection={"mode": "routed", "snapshot_id": catalog["snapshot_id"],
                                   "reason": "The advertised bounded checks fit this task."})
        write(directory / "roles.json", roles)
        proc = self.launch(directory)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        workspace = str(directory / "workspaces/contribution")
        self.assertEqual(catalog["context"]["project_root"], workspace)
        self.assertTrue(all(call["cwd"] == workspace for call in self.calls()))
        self.assertIn("fixture-collaborator", self.execs()[0]["argv"])
        self.assertTrue((directory / "evidence/contribution/launch-model-snapshot.json").exists())

    def test_recorded_permission_mutation_is_refused_before_policy_probe_or_worker(self):
        directory = self.stage(execution={"sandbox": "read-only"})
        plan = json.loads((directory / "run.json").read_text())
        plan["roles"]["contribution"]["execution"].update(
            sandbox="bypass", network=True, authorization="changed manifest only")
        write(directory / "run.json", plan)
        proc = self.launch(directory)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("permissions changed", proc.stderr)
        self.assertFalse(self.calls())

    def test_context_mutation_is_refused_before_any_worker(self):
        directory = self.stage()
        (directory / "context.md").write_text("Different requirements.")
        proc = self.launch(directory)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("context changed after capture", proc.stderr)
        self.assertFalse(self.execs())

    def test_detached_cancellation_keeps_raw_evidence_and_partial_contract(self):
        directory = self.stage("HANG_FIXTURE: execute a bounded probe.")
        proc = self.cli("--start", directory)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        deadline = time.monotonic() + 20
        while not self.execs() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.execs(), (directory / "err.log").read_text())
        proc = self.cli("--cancel", directory)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(council_liveness.lock_state(str(directory)), "free")
        result = self.result(directory)
        self.assertEqual(result["execution_state"], "interrupted")
        self.assertFalse(result["required_contributions_complete"])
        logs = list((directory / "evidence/contribution").glob("*.stdout.jsonl"))
        self.assertEqual(len(logs), 1)
        self.assertTrue(logs[0].read_text())
        self.assertEqual(self.cli("--start", directory).returncode, 2)


if __name__ == "__main__":
    unittest.main()
