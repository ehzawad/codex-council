"""Mechanical documentation and packaging checks, plus a few literal-prose
assertions for instructions the skill must keep."""
import json
import re
import shlex
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/codex-council"
SKILL = PLUGIN / "skills/codex-council/SKILL.md"
REFERENCES = SKILL.parent / "references"
SCRIPTS = SKILL.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
HAS_RUNNER = (SCRIPTS / "codex_council.py").exists()
if HAS_RUNNER:
    import codex_council  # noqa: E402


@unittest.skipUnless(HAS_RUNNER, "requires full checkout")
class DocumentationContractTests(unittest.TestCase):
    def test_skill_has_room_to_evolve_and_does_not_pin_the_host_model(self):
        text = SKILL.read_text()
        self.assertLess(len(text), 10000)
        self.assertLess(len(text.splitlines()), 180)
        frontmatter = text.split("---", 2)[1]
        self.assertFalse(re.search(r"(?m)^(model|effort|context):", frontmatter))
        self.assertNotIn("disable-model-invocation: true", frontmatter)

    def test_every_skill_python_command_parses_with_the_real_parser(self):
        commands = []
        for block in re.findall(r"```bash\n(.*?)```", SKILL.read_text(), re.S):
            for line in block.splitlines():
                if line.startswith("python3 "):
                    argv = shlex.split(line)[2:]
                    parsed = codex_council._parse_args(argv)
                    self.assertEqual(parsed.skill_contract, codex_council.SKILL_CONTRACT_EPOCH)
                    commands.append(argv[0])
        self.assertEqual(commands, ["--prepare", "--discover", "--check-staging-dir",
                                    "--start", "--freshness"])

    def test_new_modes_are_exclusive_and_accept_the_current_epoch(self):
        import contextlib
        import io
        for command in ("--prepare", "--freshness"):
            parsed = codex_council._parse_args([command, "/private/run", "--skill-contract",
                                               str(codex_council.SKILL_CONTRACT_EPOCH)])
            self.assertEqual(getattr(parsed, command[2:]), "/private/run")
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                codex_council._parse_args([command, "/run", "--start", "/other"])
            self.assertEqual(caught.exception.code, 2)

    def test_role_example_uses_the_actual_parser_without_magic_prose(self):
        blocks = re.findall(r"```json\n(.*?)```", (REFERENCES / "invocation-contract.md").read_text(), re.S)
        panels = [json.loads(block) for block in blocks if isinstance(json.loads(block), list)]
        self.assertEqual(len(panels), 1)
        roles = codex_council._parse_roles_json(json.dumps(panels[0]))
        self.assertEqual(roles[0].execution["sandbox"], "workspace-write")

    def test_relative_markdown_links_point_at_existing_files(self):
        documents = [SKILL, ROOT / "README.md", ROOT / "DESIGN.md", *REFERENCES.glob("*.md")]
        for document in documents:
            for target in re.findall(r"\]\(([^)]+)\)", document.read_text()):
                if "://" in target or target.startswith("#"):
                    continue
                with self.subTest(document=document.name, target=target):
                    self.assertTrue((document.parent / target.split("#", 1)[0]).exists())

    def test_marketplace_installs_only_the_plugin_subtree(self):
        marketplace = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
        self.assertEqual(marketplace["plugins"][0]["source"], "./plugins/codex-council")
        self.assertFalse((PLUGIN / "tests").exists())
        self.assertFalse(any(PLUGIN.rglob("*.pdf")))
        self.assertFalse(any(PLUGIN.rglob("*.png")))

    def test_license_is_publishable(self):
        meta = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text())
        self.assertEqual(meta["license"], "MIT")
        self.assertIn("Permission is hereby granted", (ROOT / "LICENSE").read_text())

    def test_allowed_tools_pre_approve_every_runner_command_as_written(self):
        """Every runner command the skill body gives starts with a declared
        allow-rule's text before its trailing ' *', and nothing broader is
        declared. Actual host authorization requires a Claude Code check."""
        text = SKILL.read_text()
        frontmatter = text.split("---", 2)[1]
        rules = re.findall(r"(?m)^  - Bash\((.*) \*\)$", frontmatter)
        runner = ('python3 "${CLAUDE_PLUGIN_ROOT}/skills/codex-council/'
                  'scripts/codex_council.py"')
        self.assertEqual(rules, [runner])
        self.assertEqual(len(re.findall(r"(?m)^  - ", frontmatter)), len(rules))
        body = text.split("---", 2)[2]
        commands = re.findall(r"(?m)^(python3 .*)$", body)
        commands += re.findall(r"`(python3 [^`]*)`", body)
        self.assertGreaterEqual(len(commands), 6)
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(command.startswith(runner + " "))
                argv = shlex.split(command)[2:]
                parsed = codex_council._parse_args(argv)
                self.assertEqual(parsed.skill_contract, codex_council.SKILL_CONTRACT_EPOCH)

    def test_skill_says_pre_approval_ends_with_its_turn_and_names_write(self):
        text = " ".join(SKILL.read_text().split())
        self.assertIn("lasts only for the turn that invokes it", text)
        for step in ("`--status`", "`--freshness`", "`--cancel`", "any new council"):
            self.assertIn(step, text.split("lasts only for the turn")[1][:400])
        self.assertIn("invoke this skill again first", text)
        self.assertIn("needs Write permission in the run directory, which this skill "
                      "does not grant", text)
        self.assertIn("`dontAsk`", text)
        frontmatter = SKILL.read_text().split("---", 2)[1]
        self.assertNotIn("Write", frontmatter)
        readme = " ".join((ROOT / "README.md").read_text().split())
        self.assertIn("does not carry into later turns", readme)
        self.assertIn("The skill does not pre-approve Write.", readme)

    def test_skill_allocates_each_run_with_new_run_and_prunes_only_on_request(self):
        text = SKILL.read_text()
        self.assertNotIn("mktemp", text)
        flat = " ".join(text.split())
        new_run = flat.index('codex_council.py" --new-run --skill-contract')
        self.assertLess(new_run, flat.index("--prepare 'ABS_RUNDIR'"))
        self.assertIn("use its printed absolute path literally", flat)
        self.assertIn("Run `--prune-runs` only on user request; add `--delete` only when "
                      "they ask to delete", flat)

    def test_reconciliation_quotes_or_checks_collaborator_observations(self):
        text = " ".join(SKILL.read_text().split())
        self.assertIn("quote the reply or the attempt log, or check it against "
                      "`result.json`; do not paraphrase it from memory", text)

    def test_ci_job_names_follow_the_matrix_labels(self):
        ci = (ROOT / ".github/workflows/ci.yml").read_text()
        self.assertIn("    name: test (${{ matrix.label }})\n", ci)
        labels = re.findall(r"label: '([^']+)'", ci)
        self.assertEqual(labels, ["3.12", "3.13", "3.14", "3.15", "macos, 3.13"])
        rows = re.findall(r"\{os: (\S+), python: '([^']+)', label: '([^']+)'\}", ci)
        self.assertEqual(len(rows), len(labels))
        for os_name, python, label in rows:
            expected = python if os_name == "ubuntu-latest" else f"macos, {python}"
            self.assertEqual(label, expected)

    def test_bare_command_defaults_do_not_grant_bypass(self):
        for command in (codex_council._fresh_cmd("/workspace"),
                        codex_council._resume_cmd("/workspace", "thread-id")):
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
            self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
            self.assertIn('approval_policy="never"', command)

    def test_behavioral_cases_cover_non_invocation_and_context_drift(self):
        cases = json.loads((ROOT / "tests/fixtures/invocation_cases.json").read_text())
        ids = {case["id"] for case in cases["cases"]}
        self.assertLessEqual({"simple-no-council", "after-ten-turns", "changed-during-run",
                             "after-compaction", "context-only", "no-redundant-invocation",
                             "explicit-first-turn"}, ids)
        self.assertEqual(len(ids), len(cases["cases"]))
        self.assertTrue(all(case["steps"] and case["expected"] for case in cases["cases"]))


if __name__ == "__main__":
    unittest.main()
