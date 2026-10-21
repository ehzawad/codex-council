"""Workspace, capabilities, freshness, evidence and real-process adapter tests.

No paid inference or network. The fake CLI checks integration/argv, NOT the real
Codex sandbox. tests/test_live_workflow.py is the opt-in actual-sandbox gate.
"""

import copy
import dataclasses
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/codex-council/skills/codex-council/scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import council_evidence as evidence  # noqa: E402
import council_policy as policy  # noqa: E402
import council_workflow as workflow  # noqa: E402
import council_workspace as workspace  # noqa: E402
from fake_workflow_codex import apply_codex_override  # noqa: E402


@dataclasses.dataclass(frozen=True)
class RoleFixture:
    id: str = "experiment"
    label: str = "Current contribution"
    instruction: str = "Reproduce the suspected boundary failure."
    execution: dict | None = None
    workdir: str | None = None
    evidence_dir: str | None = None
    execution_overrides: tuple = ()
    task_id: str | None = None
    input_snapshot: str | None = None


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


def apply_overrides(config, values):
    """Apply -c settings the way Codex does, not as Python TOML documents."""
    result = copy.deepcopy(config)
    for setting in values:
        apply_codex_override(result, setting)
    return result


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._cleanup)
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / "source"
        self.run = self.base / "run"
        self.source.mkdir(mode=0o700)
        self.run.mkdir(mode=0o700)
        self.file = self.source / "subject.py"
        self.file.write_text("value = 1\n")
        self.baseline = self.run / "baseline"

    def _cleanup(self):
        for root, dirs, files in os.walk(self.temp.name):
            for name in dirs:
                path = Path(root) / name
                if not path.is_symlink():
                    path.chmod(0o700)
            for name in files:
                path = Path(root) / name
                if not path.is_symlink() and path.is_file():
                    path.chmod(0o600)
        self.temp.cleanup()

    def capture(self, **kwargs):
        return workspace.capture(self.source, self.baseline, include=["."], **kwargs)

    def git(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                   GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                   GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
        return subprocess.run(["git", "-C", str(self.source), *args],
                              env=env, capture_output=True, check=True).stdout

    def init_git(self):
        self.git("init", "-q")
        self.git("add", "subject.py")
        self.git("commit", "-qm", "baseline")


class CaptureTests(WorkspaceCase):
    def test_copy_is_independent_and_baseline_is_read_only(self):
        snap = self.capture()
        target = self.run / "scratch"
        workspace.copy_workspace(self.baseline, target, snap)
        (target / "subject.py").write_text("value = 2\n")
        self.assertEqual(self.file.read_text(), "value = 1\n")
        self.assertEqual((self.baseline / "subject.py").read_text(), "value = 1\n")
        self.assertNotEqual(self.file.stat().st_ino, (target / "subject.py").stat().st_ino)
        self.assertEqual(stat.S_IMODE((self.baseline / "subject.py").stat().st_mode), 0o400)

    def test_git_uses_dirty_bytes_and_relevant_untracked_files(self):
        self.init_git()
        self.file.write_text("value = 2\n")
        self.git("add", "subject.py")
        self.file.write_text("value = 3\n")
        (self.source / "regression.py").write_text("assert value == 3\n")
        snap = workspace.capture(self.source, self.baseline)
        self.assertEqual((self.baseline / "subject.py").read_text(), "value = 3\n")
        self.assertIn("regression.py", snap["files"])
        self.assertIsNotNone(snap["git"]["head"])
        self.assertFalse((self.baseline / ".git").exists())
        self.assertEqual(self.git("show", ":subject.py"), b"value = 2\n")

    def test_git_deletion_is_preserved(self):
        self.init_git()
        self.file.unlink()
        snap = workspace.capture(self.source, self.baseline)
        self.assertNotIn("subject.py", snap["files"])

    def test_ignored_files_require_explicit_scope(self):
        self.init_git()
        (self.source / ".gitignore").write_text("ignored.txt\n")
        (self.source / "ignored.txt").write_text("fixture")
        snap = workspace.capture(self.source, self.baseline)
        self.assertNotIn("ignored.txt", snap["files"])
        explicit = workspace.capture(self.source, self.run / "explicit", include=["ignored.txt"])
        self.assertIn("ignored.txt", explicit["files"])

    def test_non_git_requires_explicit_scope_without_initializing_git(self):
        with self.assertRaisesRegex(workspace.WorkspaceError, "explicit include"):
            workspace.capture(self.source, self.baseline)
        snap = self.capture()
        self.assertIsNone(snap["git"])
        self.assertFalse((self.source / ".git").exists())
        self.assertFalse((self.baseline / ".git").exists())

    def test_empty_explicit_scope_supports_context_only_tasks(self):
        snap = workspace.capture(self.source, self.baseline, include=[])
        self.assertEqual(snap["files"], {})

    def test_credentials_and_agent_configuration_are_omitted(self):
        (self.source / ".env").write_text("SECRET=test-fixture")
        (self.source / ".codex").mkdir()
        (self.source / ".codex/config.toml").write_text('model="untrusted"')
        (self.source / ".env.example").write_text("TOKEN=")
        snap = self.capture()
        self.assertIn(".env", snap["omitted"])
        self.assertIn(".codex", snap["omitted"])
        self.assertIn(".env.example", snap["files"])
        self.assertFalse((self.baseline / ".env").exists())

    def test_source_symlinks_are_refused_and_never_followed(self):
        outside = self.base / "outside"
        outside.write_text("original")
        (self.source / "escape").symlink_to(outside)
        with self.assertRaises((workspace.WorkspaceError, OSError)):
            self.capture()
        self.assertEqual(outside.read_text(), "original")

    def test_symlinked_parent_in_explicit_include_is_refused(self):
        outside = self.base / "outside-dir"
        outside.mkdir()
        (outside / "value.txt").write_text("original")
        (self.source / "jump").symlink_to(outside, target_is_directory=True)
        with self.assertRaises((workspace.WorkspaceError, OSError)):
            workspace.capture(self.source, self.baseline, include=["jump/value.txt"])

    def test_hardlinked_input_becomes_an_independent_copy(self):
        outside = self.base / "outside"
        outside.write_text("original")
        os.link(outside, self.source / "hardlink")
        snap = self.capture()
        target = self.run / "scratch"
        workspace.copy_workspace(self.baseline, target, snap)
        (target / "hardlink").write_text("changed")
        self.assertEqual(outside.read_text(), "original")

    def test_fifo_is_refused_without_blocking(self):
        os.mkfifo(self.source / "pipe")
        started = time.monotonic()
        with self.assertRaises((workspace.WorkspaceError, OSError)):
            self.capture()
        self.assertLess(time.monotonic() - started, 2)

    def test_capture_detects_source_changes_during_copy(self):
        real = workspace._hash_file
        changed = False

        def changing(root, name, destination=None):
            nonlocal changed
            result = real(root, name, destination)
            if destination is not None and not changed:
                changed = True
                self.file.write_text("value = 999\n")
            return result

        with patch.object(workspace, "_hash_file", side_effect=changing):
            with self.assertRaisesRegex(workspace.WorkspaceError, "source changed"):
                self.capture()

    def test_nested_run_directory_is_refused(self):
        with self.assertRaisesRegex(workspace.WorkspaceError, "outside"):
            workspace.capture(self.source, self.source / "run" / "baseline", include=["."])

    def test_executable_bit_is_preserved(self):
        self.file.chmod(0o755)
        snap = self.capture()
        target = self.run / "scratch"
        workspace.copy_workspace(self.baseline, target, snap)
        self.assertEqual(stat.S_IMODE((target / "subject.py").stat().st_mode), 0o700)

    def test_case_collisions_are_rejected_when_filesystem_supports_them(self):
        a, b = self.source / "Case.txt", self.source / "case.txt"
        a.write_text("a")
        b.write_text("b")
        if a.stat().st_ino == b.stat().st_ino:
            self.skipTest("case-insensitive filesystem")
        with self.assertRaisesRegex(workspace.WorkspaceError, "collision"):
            self.capture()

    def test_empty_directories_are_captured_copied_and_compared(self):
        (self.source / "output").mkdir()
        (self.source / "nested/leaf").mkdir(parents=True)
        (self.source / "secrets").mkdir()
        (self.source / "secrets/.env").write_text("TOKEN=x")
        snap = self.capture()
        self.assertEqual(snap["dirs"], ["nested/leaf", "output", "secrets"])
        target = self.run / "scratch"
        workspace.copy_workspace(self.baseline, target, snap)
        for name in snap["dirs"]:
            self.assertTrue((target / name).is_dir(), name)
            self.assertTrue((self.baseline / name).is_dir(), name)
        self.assertFalse((target / "secrets/.env").exists())
        self.assertEqual(workspace.freshness(snap)["state"], "unchanged")
        (self.source / "output").rmdir()
        fresh = workspace.freshness(snap)
        self.assertEqual(fresh["state"], "stale")
        self.assertIn("output/", fresh["changed_paths"])
        (target / "nested/leaf").rmdir()
        with self.assertRaisesRegex(workspace.WorkspaceError, "empty directories"):
            workspace.verify_tree(target, snap["files"], snap["dirs"])

    def test_modified_baseline_is_not_used(self):
        snap = self.capture()
        target = self.baseline / "subject.py"
        target.chmod(0o600)
        target.write_text("changed")
        with self.assertRaisesRegex(workspace.WorkspaceError, "input changed"):
            workspace.copy_workspace(self.baseline, self.run / "scratch", snap)

    def test_added_baseline_control_directory_is_detected(self):
        snap = self.capture()
        self.baseline.chmod(0o700)
        (self.baseline / ".codex").mkdir()
        with self.assertRaisesRegex(workspace.WorkspaceError, "control"):
            workspace.verify_tree(self.baseline, snap["files"], snap["dirs"])

    def test_freshness_tracks_content_additions_deletions_and_index(self):
        self.init_git()
        snap = workspace.capture(self.source, self.baseline)
        self.assertEqual(workspace.freshness(snap)["state"], "unchanged")
        self.file.write_text("new value\n")
        self.assertIn("subject.py", workspace.freshness(snap)["changed_paths"])
        self.file.write_text("value = 1\n")
        (self.source / "new.txt").write_text("new")
        self.assertIn("new.txt", workspace.freshness(snap)["changed_paths"])
        (self.source / "new.txt").unlink()
        self.git("rm", "--cached", "subject.py")
        self.assertTrue(workspace.freshness(snap)["git_metadata_changed"])

    def test_invalid_paths(self):
        for value in ("../escape", "/absolute", "a/../b", "a\\b", "a//b", "", "./a"):
            with self.subTest(value=value), self.assertRaises(workspace.WorkspaceError):
                workspace.relative_path(value)

    def test_strict_json_and_private_reader(self):
        path = self.run / "value.json"
        path.write_text('{"a": 1, "a": 2}')
        path.chmod(0o600)
        with self.assertRaisesRegex(workspace.WorkspaceError, "duplicate"):
            workspace.read_json(path)
        write_json(path, {"a": 1})
        path.chmod(0o644)
        with self.assertRaises(workspace.WorkspaceError):
            workspace.read_json(path)
        self.assertEqual(workspace.read_json(path, private=False), {"a": 1})


class PolicyTests(unittest.TestCase):
    def test_default_is_restricted_fresh_and_required(self):
        p = policy.parse_policy()
        self.assertEqual((p.sandbox, p.network, p.external_tools, p.continuity, p.required),
                         ("read-only", False, "deny", "fresh", True))

    def test_escalation_requires_authorization(self):
        for value in ({"sandbox": "bypass"}, {"external_tools": "native"},
                      {"sandbox": "workspace-write", "network": True}):
            with self.subTest(value=value), self.assertRaisesRegex(workspace.WorkspaceError,
                                                                   "authorization"):
                policy.parse_policy(value)
        p = policy.parse_policy({"sandbox": "workspace-write", "network": True,
                                 "authorization": "User requested dependency download"})
        self.assertTrue(p.network)

    def test_unknown_and_wrong_type_values_are_refused(self):
        for value in ({"network": 1}, {"required": "yes"}, {"sandbox": "auto"},
                      {"continuity": "last"}, {"external_tools": "all"}, {"other": True}, []):
            with self.subTest(value=value), self.assertRaises(workspace.WorkspaceError):
                policy.parse_policy(value)

    def test_mcp_keys_are_bare_codex_dotted_paths(self):
        p = policy.parse_policy()
        settings = policy.config_overrides(p, ("node_repl", "fixture-2"))
        self.assertIn("mcp_servers.node_repl.enabled=false", settings)
        self.assertFalse(any('"' in s for s in settings if s.startswith("mcp_servers.")))
        config = apply_overrides({"mcp_servers": {"node_repl": {}, "fixture-2": {}}}, settings)
        self.assertEqual(set(config["mcp_servers"]), {"node_repl", "fixture-2"})
        self.assertIs(config["mcp_servers"]["node_repl"]["enabled"], False)
        self.assertIs(config["features"]["apps"], False)

    def test_every_sandboxed_policy_ignores_execpolicy_rules(self):
        for sandbox in ("read-only", "workspace-write"):
            with self.subTest(sandbox=sandbox):
                options = policy.command_options(policy.parse_policy({"sandbox": sandbox}))
                self.assertEqual(options[:3], ["--sandbox", sandbox, "--ignore-rules"])

    def test_quoted_mcp_key_would_create_a_different_server(self):
        # Why quoting is wrong: Codex keeps the quotes as part of the name.
        config = apply_overrides({"mcp_servers": {"node_repl": {}}},
                                 ['mcp_servers."node_repl".enabled=false'])
        self.assertEqual(set(config["mcp_servers"]), {"node_repl", '"node_repl"'})
        self.assertNotIn("enabled", config["mcp_servers"]["node_repl"])

    def test_unaddressable_mcp_names_fail_closed(self):
        p = policy.parse_policy()
        for name in ("server.name", 'quote"name', "a:b", "x@y", "a/b", "", "sp ace", "é"):
            with self.subTest(name=name), self.assertRaisesRegex(
                    workspace.WorkspaceError, "cannot be disabled"):
                policy.config_overrides(p, (name,))

    def test_policy_read_failure_keeps_the_sanitized_category(self):
        import council_discovery

        def failing(*_args, **_kwargs):
            raise council_discovery._DiscoveryFailure("server_exited:initialize", "other")

        with patch("shutil.which", return_value="/bin/codex"), \
                patch.object(council_discovery, "_app_server_session", failing):
            with self.assertRaisesRegex(workspace.WorkspaceError,
                                        r"\(server_exited:initialize:other\)"):
                policy._read_config("/scratch", ())

    def test_metadata_inspection_checks_effective_settings_twice(self):
        seen = []
        config = {"mcp_servers": {"fixture": {"enabled": True, "url": "SECRET-SENTINEL"}}}

        def read(root, settings):
            seen.append((root, settings))
            return apply_overrides(config, settings), None

        result = policy.inspect_policy("/scratch", policy.parse_policy(), reader=read)
        self.assertEqual(len(seen), 2)
        self.assertNotIn("SECRET-SENTINEL", json.dumps(result))
        self.assertIn("mcp_servers.fixture.enabled=false", result["overrides"])
        self.assertEqual(result["attestation"], "configuration-observed-not-exec-attested")

    def test_managed_override_and_unknown_mcp_fail_closed(self):
        p = policy.parse_policy({"sandbox": "workspace-write"})

        def managed(root, settings):
            result = apply_overrides({"mcp_servers": {}}, settings)
            result["sandbox_mode"] = "danger-full-access"
            return result, None

        with self.assertRaisesRegex(workspace.WorkspaceError, "sandbox_mode"):
            policy.inspect_policy("/scratch", p, reader=managed)
        with self.assertRaisesRegex(workspace.WorkspaceError, "MCP inventory"):
            policy.inspect_policy("/scratch", p, reader=lambda root, settings: ({}, None))

    def test_managed_requirements_that_forbid_the_request_fail_closed(self):
        p = policy.parse_policy({"sandbox": "workspace-write"})
        refused = ({"allowedSandboxModes": ["read-only"]},
                   {"allowedApprovalPolicies": ["on-request"]},
                   {"allowedWebSearchModes": ["live"]},
                   {"featureRequirements": {"memories": True}},
                   {"featureRequirements": {"tool_suggest": True}},
                   {"allowedSandboxModes": "workspace-write"}, ["unexpected"])
        for requirements in refused:
            with self.subTest(requirements=requirements), self.assertRaisesRegex(
                    workspace.WorkspaceError, "managed requirements"):
                policy.inspect_policy("/scratch", p, reader=lambda root, settings: (
                    apply_overrides({"mcp_servers": {}}, settings), requirements))
        allowed = {"allowedSandboxModes": ["read-only", "workspace-write"],
                   "allowedApprovalPolicies": ["never"], "featureRequirements": {"apps": False}}
        policy.inspect_policy("/scratch", p, reader=lambda root, settings: (
            apply_overrides({"mcp_servers": {}}, settings), allowed))

    def test_notify_memories_and_installers_are_disabled_and_observed(self):
        p = policy.parse_policy()
        settings = policy.config_overrides(p)
        for setting in ("notify=[]", "features.memories=false", "features.tool_suggest=false",
                        "features.skill_mcp_dependency_install=false",
                        "features.remote_plugin=false", "features.workspace_dependencies=false",
                        "features.image_generation=false"):
            self.assertIn(setting, settings)
        host = {"mcp_servers": {}, "notify": ["host-program", "--flag"],
                "features": {"memories": True}}
        config = apply_overrides(host, settings)
        policy.validate_observed(config, p, ())
        config["notify"] = ["host-program"]
        with self.assertRaisesRegex(workspace.WorkspaceError, "notify"):
            policy.validate_observed(config, p, ())
        native = policy.parse_policy({"external_tools": "native", "authorization": "user asked"})
        self.assertIn("features.memories=false", policy.config_overrides(native))

    def test_unknown_future_mcp_server_between_reads_is_refused(self):
        calls = 0

        def read(root, settings):
            nonlocal calls
            calls += 1
            config = apply_overrides({"mcp_servers": {}}, settings)
            if calls == 2:
                config["mcp_servers"]["new"] = {"enabled": True}
            return config, None

        with self.assertRaisesRegex(workspace.WorkspaceError, "inventory changed"):
            policy.inspect_policy("/scratch", policy.parse_policy(), reader=read)

    def test_restricts_tmp_and_additional_writable_roots(self):
        p = policy.parse_policy({"sandbox": "workspace-write"})
        config = apply_overrides({"mcp_servers": {}}, policy.config_overrides(p))
        policy.validate_observed(config, p, ())
        config["sandbox_workspace_write"]["writable_roots"] = ["/live"]
        with self.assertRaisesRegex(workspace.WorkspaceError, "writable_roots"):
            policy.validate_observed(config, p, ())


class WorkflowTests(WorkspaceCase):
    def stage(self, *, roles=None, body="Current objective and acceptance criteria.\n"):
        write_json(self.run / "request.json", {
            "schema": workflow.REQUEST_SCHEMA, "task_id": "current-investigation",
            "reason": "Resolve a consequential uncertainty", "source_root": str(self.source),
            "include": ["."],
        })
        (self.run / "context.md").write_text(body)
        roles = [RoleFixture()] if roles is None else roles
        return roles, body

    def test_prepare_binds_context_task_and_independent_workspaces(self):
        roles, body = self.stage(roles=[RoleFixture(id="a"), RoleFixture(id="b")])
        plan = workflow.prepare(str(self.run), roles, body)
        self.assertEqual(plan["task_id"], "current-investigation")
        self.assertEqual(plan["context_bytes"], len(body.encode()))
        self.assertEqual(plan["roles"]["a"]["execution"]["continuity"], "fresh")
        a = self.run / plan["roles"]["a"]["workspace"] / "subject.py"
        b = self.run / plan["roles"]["b"]["workspace"] / "subject.py"
        a.write_text("modified a")
        self.assertEqual(b.read_text(), "value = 1\n")
        self.assertEqual(self.file.read_text(), "value = 1\n")

    def test_context_lens_or_permission_changes_require_new_capture(self):
        roles, body = self.stage()
        workflow.prepare(str(self.run), roles, body)
        for altered, text in ((roles, "new objective"),
                              ([dataclasses.replace(roles[0], instruction="different")], body),
                              ([dataclasses.replace(roles[0], execution={"sandbox": "workspace-write"})], body)):
            with self.assertRaises(workspace.WorkspaceError):
                workflow.load_plan(self.run, altered, text)

    def test_prepared_workspaces_must_not_be_reused_after_an_experiment(self):
        roles, body = self.stage()
        plan = workflow.prepare(str(self.run), roles, body)
        (self.run / plan["roles"][roles[0].id]["workspace"] / "subject.py").write_text("changed")
        with self.assertRaisesRegex(workspace.WorkspaceError, "input changed"):
            workflow.load_plan(self.run, roles, body, verify_inputs=True)

    def test_double_prepare_refuses_without_touching_inputs(self):
        roles, body = self.stage()
        workflow.prepare(str(self.run), roles, body)
        before = (self.run / "run.json").read_bytes()
        with self.assertRaises(workspace.WorkspaceError):
            workflow.prepare(str(self.run), roles, body)
        self.assertEqual(before, (self.run / "run.json").read_bytes())

    def test_failed_capture_cannot_be_treated_as_legacy_run(self):
        roles, body = self.stage()
        (self.run / "prepare.lock").touch(mode=0o600)
        with self.assertRaisesRegex(workspace.WorkspaceError, "capture did not finish"):
            workflow.load_plan(self.run, roles, body)

    def test_tampered_workspace_binding_is_refused(self):
        roles, body = self.stage()
        plan = workflow.prepare(str(self.run), roles, body)
        plan["roles"][roles[0].id]["workspace"] = str(self.source)
        write_json(self.run / "run.json", plan)
        with self.assertRaisesRegex(workspace.WorkspaceError, "deterministic private path"):
            workflow.load_plan(self.run, roles, body)

    def test_recorded_execution_cannot_change_while_author_contract_hash_stays(self):
        roles, body = self.stage()
        plan = workflow.prepare(str(self.run), roles, body)
        plan["roles"][roles[0].id]["execution"] = policy.policy_dict({
            "sandbox": "bypass", "network": True,
            "authorization": "a changed record, not the authored policy"})
        write_json(self.run / "run.json", plan)
        with patch.object(workflow, "inspect_policy") as inspect:
            with self.assertRaisesRegex(workspace.WorkspaceError, "permissions changed"):
                workflow.bind_roles(str(self.run), roles, body, at_launch=True)
        inspect.assert_not_called()

    def test_task_and_source_scope_stay_bound_to_the_request(self):
        roles, body = self.stage()
        original = workflow.prepare(str(self.run), roles, body)
        for field, value in (("task_id", "different-task"),
                             ("source_root", str(self.run)),
                             ("include", []), ("exclude", ["*"])):
            plan = copy.deepcopy(original)
            target = plan if field == "task_id" else plan["snapshot"]
            target[field] = value
            write_json(self.run / "run.json", plan)
            with self.subTest(field=field):
                with self.assertRaisesRegex(workspace.WorkspaceError, "binding differs"):
                    workflow.load_plan(self.run, roles, body)

    def test_native_external_tool_permission_is_not_an_agent_label(self):
        roles, body = self.stage(roles=[RoleFixture(label="Security reviewer",
                                                   execution={"sandbox": "bypass"})])
        with self.assertRaisesRegex(workspace.WorkspaceError, "authorization"):
            workflow.prepare(str(self.run), roles, body)

    def test_bound_role_contains_execution_cwd_and_durable_task_identity(self):
        roles, body = self.stage()
        workflow.prepare(str(self.run), roles, body)
        observed = []

        def inspect(root, p):
            observed.append(root)
            return {"overrides": list(policy.config_overrides(p)), "requested": dataclasses.asdict(p)}

        with patch.object(workflow, "inspect_policy", side_effect=inspect):
            bound, plan = workflow.bind_roles(str(self.run), roles, body, at_launch=True)
        self.assertEqual(bound[0].workdir, str(self.run / "workspaces/experiment"))
        self.assertEqual(observed, [bound[0].workdir])
        self.assertEqual(bound[0].task_id, plan["task_id"])
        self.assertTrue((self.run / "evidence/experiment/execution-policy.json").exists())


class EvidenceTests(WorkspaceCase):
    def test_records_raw_bytes_and_separates_tool_evidence_from_claims(self):
        directory = self.run / "evidence"
        directory.mkdir(mode=0o700)
        recorder = evidence.AttemptEvidence(directory, ["codex", "exec"], "goal")
        events = [
            {"type": "item.completed", "item": {"type": "command_execution",
              "command": "python -m unittest", "exit_code": 0, "aggregated_output": "OK\n"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Everything verified"}},
            {"type": "turn.completed"},
        ]
        raw = b"\n".join(json.dumps(event).encode() for event in events) + b"\n"
        for offset in range(0, len(raw), 7):
            recorder.feed("stdout", raw[offset:offset+7])
        recorder.feed("stderr", b"diagnostic")
        result = recorder.finish(returncode=0, state="exited", unsafe_to_replay=True)
        self.assertEqual((directory / result["stdout_file"]).read_bytes(), raw)
        self.assertEqual(result["sha256"]["stdout"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(result["commands"][0]["reported_exit_code"], 0)
        self.assertEqual(result["commands"][0]["source"], "codex-jsonl")
        self.assertNotIn("Everything verified", json.dumps(result))
        self.assertTrue(result["evidence_complete"])

    def test_partial_raw_writes_are_completed_before_claiming_complete_evidence(self):
        directory = self.run / "evidence"
        directory.mkdir(mode=0o700)
        recorder = evidence.AttemptEvidence(directory, ["codex"], "goal")
        stream = recorder.streams["stdout"]
        original_write = stream.write
        raw = b'{"type":"turn.completed"}\n'
        with patch.object(stream, "write", side_effect=lambda data: original_write(data[:3])):
            recorder.feed("stdout", raw)
        result = recorder.finish(returncode=0, state="exited")
        self.assertEqual((directory / result["stdout_file"]).read_bytes(), raw)
        self.assertTrue(result["evidence_complete"])

    def test_raw_stream_no_progress_is_an_explicit_coverage_gap(self):
        directory = self.run / "evidence"
        directory.mkdir(mode=0o700)
        recorder = evidence.AttemptEvidence(directory, ["codex"], "goal")
        stream = recorder.streams["stdout"]
        with patch.object(stream, "write", return_value=0):
            recorder.feed("stdout", b'{"type":"turn.completed"}\n')
        result = recorder.finish(returncode=0, state="exited")
        self.assertFalse(result["evidence_complete"])
        self.assertIn("stdout-write-failed", result["coverage_gaps"])

    def test_oversized_summary_records_keep_raw_logs_and_mark_gap(self):
        directory = self.run / "evidence"
        directory.mkdir(mode=0o700)
        recorder = evidence.AttemptEvidence(directory, ["codex"], "goal")
        raw = b"x" * 200 + b"\n" + b'{"type":"turn.completed"}\n'
        with patch.object(evidence, "MAX_SUMMARY_LINE", 64):
            recorder.feed("stdout", raw)
        result = recorder.finish(returncode=0, state="exited")
        self.assertFalse(result["evidence_complete"])
        self.assertTrue(result["turn_completed_observed"])
        self.assertEqual((directory / result["stdout_file"]).read_bytes(), raw)

    def test_interrupted_evidence_is_retained_and_not_complete_success(self):
        directory = self.run / "evidence"
        directory.mkdir(mode=0o700)
        recorder = evidence.AttemptEvidence(directory, ["codex"], "goal")
        recorder.feed("stdout", b'{"type":"turn.started"}\n')
        result = recorder.finish(returncode=None, state="interrupted")
        self.assertEqual(result["state"], "interrupted")
        self.assertFalse(result["turn_completed_observed"])
        self.assertIsNone(result["transport_exit"])

    def test_generated_text_and_binary_artifacts_are_inventory_not_auto_applied(self):
        snap = self.capture()
        target, directory = self.run / "scratch", self.run / "evidence"
        directory.mkdir(mode=0o700)
        workspace.copy_workspace(self.baseline, target, snap)
        (target / "subject.py").write_text("value = 2\n")
        (target / "result.bin").write_bytes(b"\x00\x01")
        result = evidence.collect_artifacts(self.baseline, target, snap, directory)
        self.assertEqual({row["path"] for row in result["files"]}, {"subject.py", "result.bin"})
        self.assertIn("+value = 2", (directory / "candidate.diff").read_text())
        self.assertEqual(self.file.read_text(), "value = 1\n")

    def test_candidate_diff_preserves_embedded_unicode_line_separators(self):
        old = 'value = "first\u2028second\x85third"\n'
        new = 'value = "first\u2028changed\x85third"\n'
        self.file.write_text(old, encoding="utf-8")
        snap = self.capture()
        target, directory = self.run / "scratch", self.run / "evidence"
        directory.mkdir(mode=0o700)
        workspace.copy_workspace(self.baseline, target, snap)
        (target / "subject.py").write_text(new, encoding="utf-8")
        evidence.collect_artifacts(self.baseline, target, snap, directory)
        apply_dir = self.run / "apply"
        apply_dir.mkdir(mode=0o700)
        (apply_dir / "subject.py").write_text(old, encoding="utf-8")
        subprocess.run(["git", "apply", str(directory / "candidate.diff")],
                       cwd=apply_dir, check=True, capture_output=True)
        self.assertEqual((apply_dir / "subject.py").read_text(encoding="utf-8"), new)
        self.assertEqual(self.file.read_text(encoding="utf-8"), old)

    def test_invalid_attempt_metadata_blocks_completeness_not_the_report(self):
        roles = [RoleFixture()]
        write_json(self.run / "request.json", {
            "schema": workflow.REQUEST_SCHEMA, "task_id": "metadata-test",
            "reason": "Check evidence integrity", "source_root": str(self.source),
            "include": ["."]})
        plan = workflow.prepare(str(self.run), roles, "Current evidence")
        (self.run / "context.md").write_text("Current evidence")
        directory = self.run / plan["roles"][roles[0].id]["evidence"]
        write_json(directory / "attempt-corrupt.json", [])
        result = evidence.write_result(self.run, plan, [])
        self.assertFalse(result["required_contributions_complete"])
        self.assertIn("experiment:attempt-metadata-unreadable", result["coverage_gaps"])
        self.assertTrue((self.run / "result.json").exists())

    def test_artifact_symlink_cannot_read_host_file(self):
        snap = self.capture()
        target, directory = self.run / "scratch", self.run / "evidence"
        directory.mkdir(mode=0o700)
        workspace.copy_workspace(self.baseline, target, snap)
        (target / "escape").symlink_to(self.file)
        with self.assertRaises((workspace.WorkspaceError, OSError)):
            evidence.collect_artifacts(self.baseline, target, snap, directory)


if __name__ == "__main__":
    unittest.main()
