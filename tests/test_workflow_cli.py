"""The real council CLI/supervisor with a fake Codex protocol, never paid inference.

These tests prove capture, routing, argv, continuity, evidence and cancellation
integration. They do not claim the fake enforces a real OS sandbox.
"""
import contextlib
import dataclasses
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
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

    def cli(self, *args, timeout=60, cwd=None):
        return subprocess.run([sys.executable, str(RUNNER), *map(str, args),
                               "--skill-contract", str(codex_council.SKILL_CONTRACT_EPOCH)],
                              cwd=cwd or self.source, env=self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout)

    def stage(self, instruction="Inspect the current contribution.", *, roles=None,
              task="task-one", execution=None, include=(".",), exclude=()):
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
            "source_root": str(self.source), "include": include, "exclude": exclude})
        (directory / "context.md").write_text("Current goal: establish what the captured input supports.\n")
        proc = self.prepared = self.cli("--prepare", directory)
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

    def test_reported_status_reaches_result_and_reply_header(self):
        directory = self.stage("REPORTED_STATUS_FIXTURE: stop at the open decision.")
        proc = self.launch(directory)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        interpretation = self.result(directory)["roles"]["contribution"]["interpretation"]
        self.assertEqual(interpretation["reported_status"],
                         {"state": "blocked", "detail": "Which schema version wins?"})
        reply = next((directory / "replies").glob("*.md")).read_text()
        self.assertIn(" reported=blocked ", reply.splitlines()[0])

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

    def test_links_and_pipes_a_role_leaves_are_listed_not_followed(self):
        """A shim symlink or FIFO in scratch once cost the role its whole
        artifact manifest and required coverage."""
        directory = self.stage("SYMLINK_FIXTURE: edit beside a tool shim and a pipe.")
        proc = self.launch(directory)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = self.result(directory)
        self.assertTrue(result["required_contributions_complete"], result)
        contribution = result["roles"]["contribution"]
        self.assertEqual(contribution["coverage_gaps"], [])
        self.assertEqual({row["path"]: row["kind"]
                          for row in contribution["artifacts"]["files"]},
                         {"subject.py": "modified", "bin/git": "symlink",
                          "test.fifo": "special-file"})
        diff = directory / "evidence/contribution/candidate.diff"
        self.assertIn("+value = 2", diff.read_text())

    def test_live_bypass_binds_real_cwd_and_reports_selected_edits_in_a_mixed_run(self):
        self.source.chmod(0o755)
        (self.source / "delete.txt").write_text("captured deletion\n")
        (self.source / "selected").mkdir()
        (self.source / "selected/other.txt").write_text("captured original\n")
        (self.source / "unselected.txt").write_text("outside the captured scope\n")
        self.env["CODEX_COUNCIL_MODEL_ROUTING"] = "auto"
        roles = [
            {"id": "writer", "label": "Writer", "instruction": ["LIVE_SCOPE_FIXTURE"],
             "execution": {"workspace": "live", "sandbox": "bypass", "network": True,
                           "authorization": "User authorized working tree and unrestricted network"}},
            {"id": "reader", "label": "Reader", "instruction": ["Inspect the capture."]},
        ]
        directory = self.stage(roles=roles, include=["subject.py", "delete.txt", "selected"],
                               exclude=["selected/excluded.txt"])
        self.assertFalse((directory / "workspaces/writer").exists())
        # An edit after capture is part of the working tree observation even
        # when it precedes the worker.
        (self.source / "selected/other.txt").write_text("edited before launch\n")
        discovery = self.cli("--discover", directory)
        self.assertEqual(discovery.returncode, 0, discovery.stderr)
        catalog = json.loads((directory / "catalogs/writer/model-snapshot.json").read_text())
        self.assertEqual(catalog["context"]["project_root"], str(self.source))
        roles[0].update(model="fixture-collaborator", effort="brisk",
                        selection={"mode": "routed", "snapshot_id": catalog["snapshot_id"],
                                   "reason": "The catalog advertises the fixture."})
        write(directory / "roles.json", roles)
        preflight = self.cli("--check-staging-dir", directory)
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        self.assertIn(f"workspace=LIVE {self.source}; edits land in your working tree",
                      preflight.stdout)
        self.assertIn("write_scope=UNRESTRICTED", preflight.stdout)
        proc = self.launch(directory)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        calls = {call["cwd"]: call for call in self.execs()}
        writer = calls[str(self.source)]
        self.assertEqual(writer["root"], str(self.source))
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", writer["argv"])
        reader = calls[str(directory / "workspaces/reader")]
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", reader["argv"])
        # Live roles resolve the project natively: no marker override in the
        # discovery, the policy probe or the worker; the copy keeps it.
        config_calls = [call for call in self.calls()
                        if call["method"] == "config/read" and call["cwd"] == str(self.source)]
        self.assertTrue(config_calls)
        self.assertFalse(any("project_root_markers=[]" in c["argv"] for c in config_calls))
        self.assertNotIn("project_root_markers=[]", writer["argv"])
        self.assertIn("project_root_markers=[]", reader["argv"])
        result = self.result(directory)
        self.assertTrue(result["required_contributions_complete"], result)
        self.assertEqual((directory / "baseline/subject.py").read_text(), "value = 1\n")
        self.assertEqual((directory / "workspaces/reader/subject.py").read_text(), "value = 1\n")
        self.assertEqual((self.source / "subject.py").read_text(), "value = 2\n")
        self.assertEqual(self.source.stat().st_mode & 0o777, 0o755)
        self.assertEqual(result["roles"]["writer"]["artifacts"],
                         {"live_diff": "evidence/live.diff", "applied_to_live_tree": True,
                          "writers": ["writer"]})
        artifact = result["live"]
        self.assertEqual(artifact["writers"], ["writer"])
        self.assertEqual(artifact["changed_before_launch"], ["selected/other.txt"])
        self.assertTrue(artifact["applied_to_live_tree"])
        self.assertIn("other writers", artifact["application"])
        changed = {"delete.txt": "deleted", "subject.py": "modified",
                   "selected/new.txt": "added", "selected/other.txt": "modified",
                   "selected/link": "symlink", "selected/test.fifo": "special-file"}
        self.assertEqual({row["path"]: row["kind"] for row in artifact["files"]}, changed)
        patch = (directory / artifact["live_diff"]).read_text()
        self.assertIn("+value = 2", patch)
        self.assertIn("-captured deletion", patch)
        self.assertNotIn("out-of-scope fixture", patch)
        self.assertIn("candidate_diff", result["roles"]["reader"]["artifacts"])
        stale = self.cli("--freshness", directory)
        self.assertEqual(stale.returncode, 1, stale.stderr)
        today = json.loads(stale.stdout)
        self.assertEqual(today, result["applicability"])
        self.assertEqual(today["state"], "stale")
        self.assertEqual(set(today["changed_paths"]), set(changed))

    def test_live_resume_uses_live_thread_and_does_not_replay_failure(self):
        copy = self.stage()
        self.assertEqual(self.launch(copy).returncode, 0)
        copy_thread = self.execs()[-1]["thread"]
        execution = {"workspace": "live", "sandbox": "workspace-write", "continuity": "resume",
                     "authorization": "User authorized working-tree edits"}
        live = self.stage(execution=execution)
        self.assertEqual(self.launch(live).returncode, 0)
        worker = self.execs()[-1]
        self.assertNotEqual(worker["thread"], copy_thread)
        self.assertNotIn("resume", worker["argv"])
        self.assertIn("--ignore-rules", worker["argv"])
        self.assertEqual(self.result(live)["applicability"]["state"], "unchanged")
        self.assertEqual(self.cli("--freshness", live).returncode, 0)
        later = self.stage("SIDE_EFFECT_STALE", execution=execution)
        count = len(self.execs())
        proc = self.launch(later)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("[replay-blocked:stale]", proc.stdout)
        resumed, = self.execs()[count:]
        self.assertEqual(resumed["thread"], worker["thread"])
        self.assertEqual(resumed["root"], str(self.source))
        self.assertLess(resumed["argv"].index("--ignore-rules"), resumed["argv"].index("resume"))
        self.assertEqual((self.source / "effects.txt").read_text(), "effect\n")
        self.assertTrue(self.result(later)["roles"]["contribution"]["artifacts"]["applied_to_live_tree"])

    def git(self, *args, cwd=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                   GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                   GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
        return subprocess.run(["git", "-C", str(cwd or self.source), *args], env=env,
                              capture_output=True, text=True, check=True).stdout.strip()

    def test_discover_sends_the_probes_overrides_mcp_disables_included(self):
        """--discover, the policy probe, launch discovery and the worker send
        one -c list per role; a deny role's configured MCP server is disabled
        in its discovery session too."""
        directory = self.stage(execution={"sandbox": "workspace-write"})
        discovery = self.cli("--discover", directory)
        self.assertEqual(discovery.returncode, 0, discovery.stderr)
        def settings(argv):
            return [argv[i + 1] for i, arg in enumerate(argv) if arg == "-c"]

        overrides = next(settings(call["argv"]) for call in self.calls()
                         if call["method"] == "model/list")
        self.assertIn("mcp_servers.fixture-server.enabled=false", overrides)
        self.assertIn("agents.enabled=false", overrides)
        # The probe's first read enumerates the MCP servers; every later
        # session sends the full list.
        sent = {tuple(settings(call["argv"])) for call in self.calls()}
        self.assertEqual(sent, {tuple(overrides), tuple(
            setting for setting in overrides if not setting.startswith("mcp_servers."))})
        self.log.unlink()
        self.assertEqual(self.launch(directory).returncode, 0)
        self.assertEqual(settings(self.execs()[0]["argv"]), overrides)

    def test_worktree_role_commits_on_its_branch_and_leaves_the_tree_alone(self):
        (self.source / "deleted.txt").write_text("tracked\n")
        self.git("init", "-q")
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")
        base = self.git("rev-parse", "HEAD")
        (self.source / "subject.py").write_text("value = 2\n")  # dirty, captured
        (self.source / "deleted.txt").unlink()
        (self.source / "untracked.txt").write_text("untracked\n")
        self.env["CODEX_COUNCIL_MODEL_ROUTING"] = "auto"
        roles = [{"id": "builder", "label": "Builder", "instruction": ["WORKTREE_COMMIT_FIXTURE"],
                  "execution": {"workspace": "worktree", "sandbox": "bypass", "network": True,
                                "external_tools": "native",
                                "authorization": "User granted full access on a branch"}}]
        directory = self.stage(roles=roles, include=None)
        work = directory / "workspaces/builder"
        branch = f"codex-council/{directory.name}/builder"
        self.assertIn(f"[codex-council] worktree builder: {work} branch={branch} base={base}",
                      self.prepared.stdout)
        self.assertEqual((work / "subject.py").read_text(), "value = 2\n")
        self.assertFalse((work / "deleted.txt").exists())
        discovery = self.cli("--discover", directory)
        self.assertEqual(discovery.returncode, 0, discovery.stderr)
        catalog = json.loads((directory / "catalogs/builder/model-snapshot.json").read_text())
        self.assertEqual(catalog["context"]["project_root"], str(work))
        roles[0].update(model="fixture-collaborator", effort="brisk",
                        selection={"mode": "routed", "snapshot_id": catalog["snapshot_id"],
                                   "reason": "The catalog advertises the fixture."})
        write(directory / "roles.json", roles)
        preflight = self.cli("--check-staging-dir", directory)
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        self.assertIn(f"workspace=worktree {work} branch={branch}", preflight.stdout)
        self.assertIn("worktree roles: Codex loads the project's own AGENTS.md", preflight.stdout)
        proc = self.launch(directory)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("left their changes on their own branches", proc.stdout)
        worker, = self.execs()
        self.assertEqual(worker["root"], str(work))
        # Full access leaves Codex's own harness as configured: identical
        # overrides in discovery, the policy probe and the worker.
        self.assertTrue(any(call["method"] == "config/read" for call in self.calls()))
        for call in self.calls():
            self.assertEqual(call["cwd"], str(work))
            self.assertNotIn("project_root_markers=[]", call["argv"])
            self.assertNotIn("features.memories=false", call["argv"])
            self.assertIn('sandbox_mode="danger-full-access"', call["argv"])
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", worker["argv"])
        result = self.result(directory)
        self.assertTrue(result["required_contributions_complete"], result)
        artifacts = result["roles"]["builder"]["artifacts"]
        info = artifacts["worktree"]
        head = self.git("rev-parse", "HEAD", cwd=work)
        self.assertEqual((info["branch"], info["base"], info["head"]), (branch, base, head))
        self.assertEqual(info["commits"], [{"sha": head, "subject": "Worker change"}])
        diff = (directory / "evidence/builder" / artifacts["candidate_diff"]).read_text()
        self.assertIn("-value = 2\n+value = 3", diff)
        self.assertIn("+uncommitted", diff)
        self.assertEqual(self.git("rev-parse", branch), head)
        self.assertEqual((self.source / "subject.py").read_text(), "value = 2\n")
        self.assertEqual(self.git("rev-parse", "HEAD"), base)
        self.assertEqual(self.cli("--freshness", directory).returncode, 0)

    def test_prepare_refuses_a_worktree_role_without_a_committed_repository(self):
        directory = self.base / "run-refused"
        directory.mkdir(mode=0o700)
        self.runs.append(directory)
        write(directory / "roles.json", [{
            "id": "builder", "label": "Builder", "instruction": ["Build."],
            "execution": {"workspace": "worktree", "sandbox": "workspace-write",
                          "authorization": "User authorized a branch"}}])
        write(directory / "request.json", {
            "schema": "codex-council/request@1", "task_id": "task-one",
            "reason": "Build on a branch.", "source_root": str(self.source),
            "include": ["."]})
        (directory / "context.md").write_text("Current goal.\n")
        for setup, reason in ((None, "needs source_root inside a Git work tree"),
                              (("init", "-q"), "HEAD is unborn")):
            if setup:
                self.git(*setup)
            proc = self.cli("--prepare", directory)
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertIn(reason, proc.stderr)
            self.assertIn("use execution.workspace copy or live", proc.stderr)
        self.assertFalse((directory / "prepare.lock").exists())

    def test_two_live_writers_produce_one_shared_diff(self):
        execution = {"workspace": "live", "sandbox": "workspace-write",
                     "authorization": "User authorized concurrent working-tree edits"}
        roles = [{"id": role_id, "label": role_id, "execution": execution,
                  "instruction": [f"LIVE_WRITE_FILE:{role_id}.txt"]} for role_id in ("a", "b")]
        directory = self.stage(roles=roles)
        proc = self.launch(directory)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual({call["root"] for call in self.execs()}, {str(self.source)})
        result = self.result(directory)
        live = result["live"]
        self.assertEqual(live["writers"], ["a", "b"])
        self.assertEqual(live["changed_before_launch"], [])
        self.assertEqual({row["path"]: row["kind"] for row in live["files"]},
                         {"a.txt": "added", "b.txt": "added"})
        diff = (directory / "evidence/live.diff").read_text()
        self.assertIn("+written by a.txt", diff)
        self.assertIn("+written by b.txt", diff)
        for role_id in ("a", "b"):
            self.assertEqual(result["roles"][role_id]["artifacts"]["live_diff"],
                             "evidence/live.diff")
            self.assertFalse((directory / f"evidence/{role_id}/live.diff").exists())

    def test_prompts_and_state_keys_follow_the_workspace(self):
        role = codex_council.Role("r", "R", "Do it.", workdir="/w", task_id="t",
                                  input_snapshot="s", execution={"sandbox": "read-only"})
        copy = codex_council._compose_prompt(role, "body")
        self.assertIn("Your execution workspace is /w. Use its relative paths. Historical "
                      "absolute paths may identify the live project: do not use them as "
                      "substitutes for captured inputs. Changes belong only in this "
                      "experimental workspace. Do not edit another role's workspace or "
                      "council control files. Never apply or publish changes to the live "
                      "project. When empirical work", copy)  # 1.4.0 text
        self.assertNotIn("full access", copy)
        live = codex_council._compose_prompt(dataclasses.replace(
            role, execution={"workspace": "live", "sandbox": "workspace-write"}), "body")
        self.assertIn("the live working tree at /w", live)
        self.assertIn("Other writers (people or roles) may edit this tree at the same time", live)
        # The shared tree is other live roles' workspace too.
        self.assertIn("Outside this tree, do not edit other roles' workspaces or council "
                      "control files.", live)
        self.assertNotIn("Do not edit another role's workspace", live)
        full = {"workspace": "worktree", "sandbox": "bypass", "network": True,
                "external_tools": "native"}
        worktree = codex_council._compose_prompt(dataclasses.replace(
            role, execution=full,
            worktree={"branch": "codex-council/run/r", "base": "abc123"}), "body")
        self.assertIn("a Git worktree of the user's repository at /w on branch "
                      "codex-council/run/r, based on abc123 plus the user's captured "
                      "uncommitted changes", worktree)
        self.assertIn("Edit and commit freely on that branch; never push, touch other "
                      "branches, or edit the user's working tree", worktree)
        self.assertIn("You have full access, so run any commands and use the network, web "
                      "search and your own tools and subagents as the task needs; this "
                      "replaces the subagent limit above.", worktree)
        self.assertIn("verify your own result", worktree)
        self.assertIn("when it needs a design decision the context does not settle, stop "
                      "and report it as blocked instead of guessing.", worktree)
        self.assertNotIn("report it as blocked instead", copy)
        # workspace-write cannot write the worktree's .git: no commit invitation.
        sandboxed = codex_council._compose_prompt(dataclasses.replace(
            role, execution={**full, "sandbox": "workspace-write"},
            worktree={"branch": "codex-council/run/r", "base": "abc123"}), "body")
        self.assertNotIn("commit freely", sandboxed)
        self.assertIn("Edit freely there but do not commit: this sandbox cannot write the "
                      "repository's Git metadata", sandboxed)
        self.assertIn("Never push, touch other branches, or edit the user's working tree",
                      sandboxed)
        self.assertNotIn("full access", sandboxed)
        # Bypass with external tools denied: no web search or subagents promised.
        denied = codex_council._compose_prompt(dataclasses.replace(
            role, execution={"workspace": "live", "sandbox": "bypass", "network": True,
                             "external_tools": "deny"}), "body")
        self.assertNotIn("full access", denied)
        self.assertNotIn("web search and your own tools", denied)
        self.assertIn("Your commands run without a sandbox, so run any commands and use the "
                      "network as the task needs; web search, MCP tools and subagents stay "
                      "disabled for this role. Work autonomously", denied)
        keys = {}
        for mode in ("copy", "live", "worktree"):
            plan = {"snapshot": {"source_root": "/src"}, "task_id": "t",
                    "roles": {"r": {"execution": {"workspace": mode}}}}
            with unittest.mock.patch.dict(codex_council._WORKFLOW, plan=plan), \
                    unittest.mock.patch.dict(os.environ, CODEX_COUNCIL_SESSION_KEY="k"):
                keys[mode] = codex_council._project_key("r").split("-")
        self.assertEqual(keys["copy"][2:3], [keys["live"][3]])  # the session suffix
        self.assertEqual(keys["live"][2], "live")
        self.assertEqual(keys["worktree"][2], "worktree")
        self.assertEqual(keys["copy"][:2], keys["live"][:2])

    def test_a_1_4_0_shaped_copy_run_serves_freshness_status_and_prune(self):
        allocated = self.cli("--new-run")
        self.assertEqual(allocated.returncode, 0, allocated.stderr)
        directory = Path(allocated.stdout.strip())
        self.runs.append(directory)
        write(directory / "roles.json", [{"id": "contribution", "label": "C",
                                          "instruction": ["Inspect."]}])
        write(directory / "request.json", {
            "schema": "codex-council/request@1", "task_id": "legacy",
            "reason": "Exercise a copy run.", "source_root": str(self.source),
            "include": ["."]})
        (directory / "context.md").write_text("Current goal.\n")
        self.assertEqual(self.cli("--prepare", directory).returncode, 0)
        binding = json.loads((directory / "run.json").read_text())["roles"]["contribution"]
        self.assertEqual(set(binding), {"workspace", "evidence", "catalog", "contract_sha256",
                                        "execution"})
        self.assertNotIn("workspace", binding["execution"])
        self.assertEqual(self.cli("--freshness", directory).returncode, 0)
        proc = self.cli("--start", directory)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        deadline = time.monotonic() + 30
        while (not (directory / "result.json").exists()
               or council_liveness.lock_state(str(directory)) != "free"):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)
        status = self.cli("--status", directory)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("runner: done (exit 0)", status.stdout)
        pruned = self.cli("--prune-runs", "--older-than", "0", "--delete")
        self.assertEqual(pruned.returncode, 0, pruned.stdout + pruned.stderr)
        self.assertIn(f"removed {directory}", pruned.stdout)
        self.assertFalse(directory.exists())

    def test_moved_source_freshness_is_unknown_not_a_contract_error(self):
        directory = self.stage()
        self.source.rename(self.base / "moved")
        proc = self.cli("--freshness", directory, cwd=self.base)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["state"], "unknown")

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

    def test_project_root_markers_must_be_observed_empty_before_inference(self):
        """The fake starts from Codex's default markers; the -c override must
        clear them, and a layer that keeps them or a Codex that does not
        report them fails closed."""
        self.assertEqual(self.cli("--check-staging-dir", self.stage()).returncode, 0)
        for markers in ('[".git"]', "unreported"):
            with self.subTest(markers=markers):
                self.env.pop("FAKE_WORKFLOW_ROOT_MARKERS", None)
                directory = self.stage()  # a launch uses its directory up
                self.env["FAKE_WORKFLOW_ROOT_MARKERS"] = markers
                proc = self.cli("--check-staging-dir", directory)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("requested execution policy: project_root_markers",
                              proc.stderr)
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

    def test_stopping_live_writer_keeps_edits_after_cancel_and_reap(self):
        subject = self.source / "subject.py"
        for hard_stop in (False, True):
            with self.subTest(hard_stop=hard_stop):
                subject.write_text("value = 1\n")
                directory = self.stage("LIVE_WRITE_AND_WAIT", execution={
                    "workspace": "live", "sandbox": "workspace-write",
                    "authorization": "User authorized working-tree edits"})
                proc = self.cli("--start", directory)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                deadline = time.monotonic() + 20
                while subject.read_text() != "value = 3\n" and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertEqual(subject.read_text(), "value = 3\n")
                if hard_stop:
                    supervisor = json.loads((directory / "supervisor.json").read_text())
                    os.kill(supervisor["pid"], signal.SIGKILL)
                    deadline = time.monotonic() + 5
                    while (council_liveness.lock_state(str(directory)) != "free"
                           and time.monotonic() < deadline):
                        time.sleep(0.05)
                    status = self.cli("--status", directory)
                    self.assertIn("live codex groups:", status.stdout)
                    proc = self.cli("--reap", directory)
                    self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                    self.assertIn("reaped contribution", proc.stdout)
                    self.assertFalse((directory / "result.json").exists())
                else:
                    proc = self.cli("--cancel", directory)
                    self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                    result = self.result(directory)
                    self.assertEqual(result["execution_state"], "interrupted")
                    self.assertFalse(result["required_contributions_complete"])
                    self.assertTrue(result["roles"]["contribution"]["artifacts"]["applied_to_live_tree"])
                self.assertEqual(subject.read_text(), "value = 3\n")
                self.assertEqual(self.cli("--freshness", directory).returncode, 1)


if __name__ == "__main__":
    unittest.main()
