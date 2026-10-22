"""Opt-in service tiers, using the existing metadata and worker Codex fake.

The workflow tests exercise capture, per-role discovery, both resolution stages,
effective config, real fake-worker subprocesses, replies and result.json. They
call the workflow directly so they need no host supervisor/ps privileges.
"""

import asyncio
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import council_testlib
import codex_council as runner
import council_discovery as discovery
import council_evidence as evidence
import council_workflow as workflow
import fake_codex

NATIVE = fake_codex.NATIVE_MODEL
OTHER = "future-vega-2033"
TIERS = [{"id": "priority", "name": "Fast", "description": "More usage."}]

setUpModule = council_testlib.install_fake_codex
tearDownModule = council_testlib.remove_fake_codex


def role_json(rid="worker", **extra):
    return {"id": rid, "label": rid.title(), "instruction": ["Review the input."],
            **extra}


def parsed(**extra):
    return runner._parse_roles_json(json.dumps([role_json(**extra)]))[0]


def scenario():
    entries = fake_codex.default_catalog()
    for entry in entries:
        entry["serviceTiers"] = copy.deepcopy(TIERS)
    result = fake_codex.default_scenario(entries)
    result["policy_config"] = {"mcp_servers": {}, "model": NATIVE,
                               "service_tier": "default"}
    return result


class ServiceTierGrammarTests(unittest.TestCase):
    def test_tier_only_preserves_native_model_selection(self):
        role = parsed(service_tier="fast")
        self.assertEqual(role.service_tier, "fast")
        self.assertIsNone(role.selection)
        self.assertIsNone(role.model)
        self.assertIsNone(role.effort)

    def test_user_mode_can_pin_only_a_tier(self):
        role = parsed(service_tier="fast", selection={"mode": "user"})
        self.assertEqual(role.selection.mode, "user")
        self.assertEqual(role.service_tier, "fast")

    def test_tier_is_independent_of_routed_model_provenance(self):
        for mode in ("routed", "native_effort"):
            extra = {"model": OTHER} if mode == "routed" else {}
            role = parsed(**extra, effort="brisk", service_tier="fast",
                          selection={"mode": mode,
                                     "snapshot_id": council_testlib.SNAPSHOT_ID,
                                     "reason": "Advertised effort fits the task."})
            self.assertEqual(role.selection.mode, mode)
            self.assertEqual(role.service_tier, "fast")

    def test_tier_values_use_the_existing_safe_open_vocabulary(self):
        self.assertEqual(parsed(service_tier="future:tier-v2").service_tier,
                         "future:tier-v2")
        for value in (None, True, 7, "", "-fast", "fast\n", 'fast"', "fast mode"):
            with self.subTest(value=value):
                council_testlib.assert_usage_exit(
                    self, lambda: parsed(service_tier=value),
                    expect_in_stderr="service_tier")


class ServiceTierDiscoveryTests(unittest.TestCase):
    def normalize(self, **extra):
        return discovery._normalize_model_entry(fake_codex.model_entry(
            NATIVE, serviceTiers=copy.deepcopy(TIERS), **extra))

    def test_records_ids_and_shows_them_without_treating_names_as_ids(self):
        entry, problem = self.normalize()
        self.assertIsNone(problem)
        self.assertEqual(entry["service_tiers"], ["priority"])
        snap = council_testlib.snapshot(catalog=council_testlib.catalog([
            fake_codex.model_entry(NATIVE, serviceTiers=TIERS)]))
        self.assertIsNone(discovery._snapshot_shape_problem(snap))
        self.assertIn("service tiers: priority", "\n".join(
            discovery._discovery_summary(snap)))

    def test_missing_metadata_preserves_legacy_snapshots_and_other_selection(self):
        raw = fake_codex.model_entry(NATIVE)
        del raw["serviceTiers"]
        entry, problem = discovery._normalize_model_entry(raw)
        self.assertIsNone(problem)
        self.assertEqual(entry["service_tiers"], [])
        snap = council_testlib.snapshot()
        for item in snap["catalog"]["models"]:
            item.pop("service_tiers", None)
        self.assertIsNone(discovery._snapshot_shape_problem(snap))

    def test_malformed_or_duplicate_tier_ids_are_rejected(self):
        for value in (None, {}, ["priority"], [{"id": ""}], [{"id": 1}],
                      TIERS + TIERS):
            with self.subTest(value=value):
                raw = fake_codex.model_entry(NATIVE, serviceTiers=value)
                self.assertEqual(discovery._normalize_model_entry(raw),
                                 (None, "serviceTiers"))
        snap = council_testlib.snapshot()
        for value in (None, "priority", [None], [""], ["priority", "priority"]):
            with self.subTest(snapshot=value):
                snap["catalog"]["models"][0]["service_tiers"] = value
                self.assertEqual(discovery._snapshot_shape_problem(snap),
                                 "catalog.models")


class ServiceTierWorkflowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.mkdir("source")
        self.state = self.mkdir("state")
        self.argv_dir = self.mkdir("argv")
        self.home = self.mkdir("codex-home")
        self.config = self.home / "config.toml"
        self.config.write_text('service_tier = "default"\n')
        self.scenario_path = self.root / "scenario.json"
        self.method_log = self.root / "methods.log"
        self.env = council_testlib.clean_env(
            PATH=council_testlib.fake_bin_dir() + os.pathsep + os.environ.get("PATH", ""),
            CODEX_HOME=str(self.home), XDG_STATE_HOME=str(self.state),
            FAKE_CODEX_SCENARIO=str(self.scenario_path),
            FAKE_CODEX_ARGV_DIR=str(self.argv_dir),
            FAKE_CODEX_METHOD_LOG=str(self.method_log))
        self.addCleanup(self.assert_config_unchanged)
        self.addCleanup(lambda: council_testlib.assert_only_discovery_methods(
            self, fake_codex.read_lines(self.method_log)))
        env = patch.dict(os.environ, self.env, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.current = scenario()
        self.set_scenario(self.current)

    def mkdir(self, name):
        path = self.root / name
        path.mkdir(mode=0o700)
        return path

    def assert_config_unchanged(self):
        self.assertEqual(self.config.read_text(), 'service_tier = "default"\n')

    def set_scenario(self, value):
        self.current = value
        fake_codex.write_scenario(self.scenario_path, value)

    def stage(self, entries=None, *, name="run", discover=True):
        directory = self.mkdir(name)
        entries = entries or [role_json()]
        self.write_roles(directory, entries)
        (directory / "context.md").write_text("Review the current input.\n")
        council_testlib.prepare_run(self, str(directory), self.env, str(self.source),
                                   source_root=str(self.source), task_id="tier-task")
        if discover:
            proc = self.cli("--discover", directory)
            self.assertEqual(proc.returncode, 0, proc.stderr)
        return directory

    def cli(self, mode, directory):
        return subprocess.run([sys.executable, council_testlib.SCRIPT, mode, str(directory),
                               "--skill-contract", council_testlib.EPOCH],
                              capture_output=True, text=True, env=self.env,
                              cwd=self.source, stdin=subprocess.DEVNULL, timeout=30)

    def write_roles(self, directory, entries):
        (directory / "roles.json").write_text(json.dumps(entries))

    def roles(self, directory):
        return runner._parse_roles_json((directory / "roles.json").read_text())

    def resolve(self, directory, *, launch=False, mode="auto"):
        return workflow.resolve_selections(self.roles(directory), str(directory), mode,
                                           launch, (directory / "context.md").read_text())

    def launch_workers(self, directory):
        roles, _, plan = self.resolve(directory, launch=True)
        replies = runner._prepare_replies_dir(str(directory))
        with patch.object(runner, "STATE_DIR", str(self.state / "codex-council")), \
                patch.object(runner, "_logical_project_root", return_value=str(self.source)), \
                contextlib.redirect_stderr(io.StringIO()):
            results = asyncio.run(runner.run_council(
                roles, (directory / "context.md").read_text(), 1,
                replies_dir=replies))
        self.assertTrue(all(r.ok for r in results), [r.error for r in results])
        (directory / "out.md").write_text(runner._format_report(results, 0.0))
        evidence.write_result(directory, plan, results)
        return roles, results

    def argvs(self):
        return [json.loads(path.read_text()) for path in sorted(self.argv_dir.glob("*.json"))]

    def test_no_opt_in_keeps_native_config_and_does_not_require_discovery(self):
        directory = self.stage(discover=False)
        roles, _ = self.launch_workers(directory)
        self.assertIsNone(roles[0].decision.dispatch_service_tier)
        self.assertNotIn("service_tier", council_testlib.config_values(self.argvs()[0]))
        self.assertFalse(self.method_log.exists())
        result = json.loads((directory / "result.json").read_text())
        self.assertIsNone(result["roles"]["worker"]["sent_service_tier"])

    def test_tier_can_change_after_capture_and_is_sent_and_reported(self):
        directory = self.stage()
        plan_before = (directory / "run.json").read_bytes()
        self.write_roles(directory, [role_json(service_tier="fast")])
        preflight = self.cli("--check-staging-dir", directory)
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        self.assertIn("service tier fast", preflight.stdout)
        roles, _ = self.launch_workers(directory)
        self.assertEqual((directory / "run.json").read_bytes(), plan_before)
        self.assertEqual(roles[0].decision.dispatch_service_tier, "fast")
        argv = self.argvs()[0]
        council_testlib.assert_no_model_overrides(self, argv)
        self.assertEqual(council_testlib.config_values(argv)["service_tier"], "fast")
        self.assertIn("service_tier=fast", (directory / "replies/worker.md").read_text())
        self.assertIn("sent fast", (directory / "out.md").read_text())
        result = json.loads((directory / "result.json").read_text())
        self.assertEqual(result["roles"]["worker"]["sent_service_tier"], "fast")
        self.assertIn("not serving-tier attestation",
                      result["roles"]["worker"]["service_tier_provenance"])

    def test_fast_maps_only_to_priority_and_generic_ids_stay_exact(self):
        for idx, (tier, advertised, accepted) in enumerate((
                ("fast", "priority", True), ("priority", "priority", True),
                ("future-tier", "future-tier", True), ("fast", "fast", False),
                ("Fast", "priority", False), ("bogus-tier", "priority", False))):
            with self.subTest(tier=tier, advertised=advertised):
                current = scenario()
                current["methods"]["model/list"]["pages"][""]["data"][0]["serviceTiers"] = [
                    {"id": advertised, "name": "Fast", "description": "A tier."}]
                self.set_scenario(current)
                directory = self.stage([role_json(service_tier=tier)], name=f"run-{idx}")
                proc = self.cli("--check-staging-dir", directory)
                self.assertEqual(proc.returncode, 0 if accepted else 2, proc.stderr)
                if not accepted:
                    self.assertIn("not advertised", proc.stderr)
        self.assertEqual(self.argvs(), [])

    def test_user_mode_with_only_a_tier_reports_a_real_sent_value(self):
        directory = self.stage([role_json(service_tier="fast", selection={"mode": "user"})])
        proc = self.cli("--check-staging-dir", directory)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("override ()", proc.stdout)
        self.launch_workers(directory)
        report = (directory / "out.md").read_text()
        self.assertIn("explicit: service tier fast", report)
        self.assertIn("sent service tier fast", report)
        council_testlib.assert_no_model_overrides(self, self.argvs()[0])

    def test_unproven_native_model_and_missing_snapshot_refuse_before_workers(self):
        directory = self.stage([role_json(service_tier="fast")], discover=False)
        proc = self.cli("--check-staging-dir", directory)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("service tier", proc.stderr)
        unknown = scenario()
        unknown["methods"]["config/read"]["result"]["config"]["model"] = None
        self.set_scenario(unknown)
        directory = self.stage([role_json(service_tier="fast")], name="unknown")
        proc = self.cli("--check-staging-dir", directory)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("model cannot be established", proc.stderr)
        self.assertEqual(self.argvs(), [])

    def test_routed_and_native_effort_modes_keep_the_explicit_tier(self):
        for mode in ("routed", "native_effort"):
            with self.subTest(mode=mode):
                directory = self.stage(name=mode)
                snap = json.loads((directory / "catalogs/worker/model-snapshot.json").read_text())
                extra = {"model": OTHER} if mode == "routed" else {}
                self.write_roles(directory, [role_json(
                    **extra, effort="brisk", service_tier="fast",
                    selection={"mode": mode, "snapshot_id": snap["snapshot_id"],
                               "reason": "The advertised effort fits."})])
                roles, _, _ = self.resolve(directory, launch=True)
                self.assertEqual(roles[0].decision.provenance, mode)
                self.assertEqual(roles[0].decision.dispatch_service_tier, "fast")
                self.assertEqual(roles[0].decision.dispatch_model,
                                 OTHER if mode == "routed" else NATIVE)

    def test_routing_off_does_not_discard_an_explicit_tier(self):
        directory = self.stage([role_json(service_tier="fast")])
        roles, launch, _ = self.resolve(directory, launch=True, mode="off")
        self.assertIsNotNone(launch)
        self.assertIsNone(roles[0].decision.dispatch_model)
        self.assertEqual(roles[0].decision.dispatch_service_tier, "fast")

    def test_routing_fallback_checks_the_native_model_instead_of_the_requested_one(self):
        directory = self.stage()
        snap = json.loads((directory / "catalogs/worker/model-snapshot.json").read_text())
        self.write_roles(directory, [role_json(
            model=OTHER, effort="brisk", service_tier="fast",
            selection={"mode": "routed", "snapshot_id": snap["snapshot_id"],
                       "reason": "The advertised effort fits."})])
        current = scenario()
        for entry in current["methods"]["model/list"]["pages"][""]["data"]:
            if entry["model"] == OTHER:
                entry["hidden"] = True
        self.set_scenario(current)
        roles, _, _ = self.resolve(directory, launch=True)
        self.assertEqual(roles[0].decision.provenance, "fallback")
        self.assertIsNone(roles[0].decision.dispatch_model)
        self.assertEqual(roles[0].decision.dispatch_service_tier, "fast")
        for entry in current["methods"]["model/list"]["pages"][""]["data"]:
            if entry["model"] == NATIVE:
                entry["serviceTiers"] = []
        self.set_scenario(current)
        text = council_testlib.assert_usage_exit(
            self, lambda: self.resolve(directory, launch=True),
            expect_in_stderr="not advertised")
        self.assertIn(NATIVE, text)

    def test_each_role_uses_its_own_planning_snapshot(self):
        directory = self.stage([role_json("alpha", service_tier="fast"),
                                role_json("beta", service_tier="fast")])
        path = directory / "catalogs/beta/model-snapshot.json"
        snap = json.loads(path.read_text())
        for entry in snap["catalog"]["models"]:
            entry["service_tiers"] = []
        path.write_text(json.dumps(snap))
        text = council_testlib.assert_usage_exit(
            self, lambda: self.resolve(directory), expect_in_stderr="not advertised")
        self.assertIn("role 'beta'", text)

    def test_explicit_model_without_tier_still_forwards_an_unlisted_pin(self):
        directory = self.stage([role_json(model="custom/model",
                                         selection={"mode": "user"})], discover=False)
        roles, _, _ = self.resolve(directory, launch=True)
        self.assertEqual(roles[0].decision.dispatch_model, "custom/model")
        self.assertIsNone(roles[0].decision.dispatch_service_tier)
        self.write_roles(directory, [role_json(model="custom/model", service_tier="fast",
                                               selection={"mode": "user"})])
        council_testlib.assert_usage_exit(
            self, lambda: self.resolve(directory), expect_in_stderr="service tier")

    def test_launch_revalidation_refuses_removed_tier_and_failed_discovery(self):
        directory = self.stage([role_json(model=OTHER, service_tier="fast",
                                         selection={"mode": "user"})])
        self.assertEqual(self.cli("--check-staging-dir", directory).returncode, 0)
        for change in ("removed", "failed"):
            with self.subTest(change=change):
                current = scenario()
                if change == "removed":
                    for entry in current["methods"]["model/list"]["pages"][""]["data"]:
                        entry["serviceTiers"] = []
                else:
                    current["methods"]["model/list"] = {
                        "error": {"code": -32601, "message": "No catalog."}}
                self.set_scenario(current)
                council_testlib.assert_usage_exit(
                    self, lambda: self.resolve(directory, launch=True),
                    expect_in_stderr="service tier")
        self.assertEqual(self.argvs(), [])

    def test_changed_native_model_is_checked_for_tier_only_requests(self):
        directory = self.stage([role_json(service_tier="fast")])
        current = scenario()
        current["methods"]["config/read"]["result"]["config"]["model"] = OTHER
        current["policy_config"]["model"] = OTHER
        for entry in current["methods"]["model/list"]["pages"][""]["data"]:
            if entry["model"] == OTHER:
                entry["serviceTiers"] = []
        self.set_scenario(current)
        text = council_testlib.assert_usage_exit(
            self, lambda: self.resolve(directory, launch=True),
            expect_in_stderr="not advertised")
        self.assertIn(OTHER, text)

    def test_catalog_from_another_provider_is_not_tier_evidence(self):
        current = scenario()
        current["methods"]["config/read"]["result"]["config"]["model_provider"] = "acme"
        self.set_scenario(current)
        directory = self.stage([role_json(model=OTHER, service_tier="fast",
                                         selection={"mode": "user"})])
        proc = self.cli("--check-staging-dir", directory)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("verified catalog", proc.stderr)

    def test_managed_overrides_to_model_or_tier_fail_effective_config_check(self):
        directory = self.stage([role_json(model=OTHER, service_tier="fast",
                                         selection={"mode": "user"})])
        self.assertEqual(self.cli("--check-staging-dir", directory).returncode, 0)
        for forced in ({"service_tier": "default"}, {"model": NATIVE}):
            with self.subTest(forced=forced):
                current = scenario()
                current["policy_forced_config"] = forced
                self.set_scenario(current)
                proc = self.cli("--check-staging-dir", directory)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("service tier", proc.stderr)
                with self.assertRaisesRegex(ValueError, "service tier"):
                    self.resolve(directory, launch=True)
        self.assertEqual(self.argvs(), [])

    def test_equivalent_priority_in_effective_config_is_accepted(self):
        current = scenario()
        current["policy_forced_config"] = {"service_tier": "priority"}
        self.set_scenario(current)
        directory = self.stage([role_json(service_tier="fast")])
        roles, _, _ = self.resolve(directory, launch=True)
        self.assertEqual(roles[0].decision.dispatch_service_tier, "fast")

    def test_fresh_resume_and_omission_are_per_invocation(self):
        first = self.stage([role_json(service_tier="fast", execution={"continuity": "resume"})])
        self.launch_workers(first)
        second = self.stage([role_json(service_tier="fast", execution={"continuity": "resume"})],
                            name="second")
        self.launch_workers(second)
        third = self.stage([role_json(execution={"continuity": "resume"})],
                           name="third", discover=False)
        self.launch_workers(third)
        fresh, resumed, native = self.argvs()
        self.assertNotIn("resume", fresh)
        self.assertIn("resume", resumed)
        self.assertIn("resume", native)
        self.assertEqual(resumed[resumed.index("resume") + 1],
                         native[native.index("resume") + 1])
        for argv in (fresh, resumed):
            self.assertEqual(council_testlib.config_values(argv)["service_tier"], "fast")
        self.assertLess(resumed.index('service_tier="fast"'), resumed.index("resume"))
        self.assertNotIn("service_tier", council_testlib.config_values(native))

    def test_a_post_check_omission_notice_remains_visible(self):
        directory = self.stage([role_json(service_tier="fast")])
        current = scenario()
        notice = (f"Configured service tier `fast` is not advertised as supported for "
                  f"model `{NATIVE}` and will be omitted from requests.")
        current["exec_item_errors"] = [notice]
        self.set_scenario(current)
        _, results = self.launch_workers(directory)
        self.assertIn(notice, results[0].warning)
        self.assertIn("will be omitted from requests", (directory / "out.md").read_text())


if __name__ == "__main__":
    unittest.main()
