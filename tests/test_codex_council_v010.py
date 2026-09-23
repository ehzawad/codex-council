"""v0.10.0 runner surfaces: per-role reply files, --follow, model/effort keys.

Unit tests import codex_council directly; end-to-end tests drive the REAL
script as a subprocess with a FAKE `codex` on PATH (no network, no real
Codex) and an isolated XDG_STATE_HOME, like tests/test_codex_council_cli.py.

Lives outside the plugin subtree so end-user installs don't bundle it.
Run from repo root:
    python3 -m unittest discover -s tests -p 'test_*.py'
"""

import asyncio
import contextlib
import hashlib
import io
import json
import os
import re
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

SCRIPTS_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__),
    "..",
    "plugins", "codex-council", "skills", "codex-council", "scripts",
))
sys.path.insert(0, SCRIPTS_DIR)

import codex_council  # noqa: E402

SCRIPT = os.path.join(SCRIPTS_DIR, "codex_council.py")
FIXED_PROJECT_ROOT = "/fixed/project/root"

HANG_SENTINEL = "PLEASE_HANG_SILENTLY"
FAIL_SENTINEL = "PLEASE_FAIL"

# Fake codex: records its argv (so tests can see -m / -c placement), then
# hangs, fails, or replies "fake reply for <role marker>".
FAKE_CODEX = textwrap.dedent(
    f"""\
    #!/usr/bin/env python3
    import json, os, sys, uuid
    prompt = sys.stdin.read()
    argv_dir = os.environ.get("FAKE_CODEX_ARGV_DIR")
    if argv_dir:
        with open(os.path.join(argv_dir, uuid.uuid4().hex + ".json"), "w") as f:
            json.dump(sys.argv[1:], f)
    if {HANG_SENTINEL!r} in prompt:
        import time
        time.sleep(300)
        sys.exit(3)
    if {FAIL_SENTINEL!r} in prompt:
        sys.stderr.write("fake codex: simulated role failure\\n")
        sys.exit(3)
    tid = "thread-" + uuid.uuid4().hex[:12]
    sys.stdout.write(json.dumps({{"type": "thread.started", "thread_id": tid}}) + "\\n")
    sys.stdout.write(json.dumps({{"type": "item.completed", "item": {{
        "type": "agent_message", "text": "fake reply from codex"}}}}) + "\\n")
    sys.stdout.write(json.dumps({{"type": "turn.completed"}}) + "\\n")
    """
)


def _instruction(text="Review"):
    return f"{text}; if nothing material, say so clearly. Thoroughness beats speed."


def _role_json(rid="alpha", label="A", instruction=None, **extra):
    entry = {"id": rid, "label": label,
             "instruction": [instruction or _instruction()]}
    entry.update(extra)
    return entry


def _make_role(rid="architect", label="Architect", model=None, effort=None):
    return codex_council.Role(rid, label, _instruction(), model, effort)


def _assert_usage_exit(test, callable_, *, expect_in_stderr):
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        with test.assertRaises(SystemExit) as ctx:
            callable_()
    test.assertEqual(ctx.exception.code, 2)
    test.assertIn(expect_in_stderr, buf.getvalue())
    return buf.getvalue()


def _private_tmpdir(test):
    d = tempfile.TemporaryDirectory()
    test.addCleanup(d.cleanup)
    os.chmod(d.name, 0o700)
    return d.name


# ---------- contract epoch / brief ----------

class ContractEpochTests(unittest.TestCase):
    def test_epoch_is_2(self):
        self.assertEqual(codex_council.SKILL_CONTRACT_EPOCH, 2)

    def test_help_mentions_follow_and_v010(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit):
                codex_council._parse_args(["--help"])
        text = out.getvalue()
        self.assertIn("--follow", text)
        self.assertIn("v0.10.0", text)
        self.assertIn("replies/", text)


class CollaborationBriefTests(unittest.TestCase):
    # Phrase pins and instruction bookending live in
    # test_codex_council.ComposePromptTests; this only guards the tone.
    def test_brief_has_no_all_caps_emphasis(self):
        self.assertNotRegex(codex_council.COLLABORATION_BRIEF, r"[A-Z]{4,}")


# ---------- optional model / effort role keys ----------

class RoleOverrideParsingTests(unittest.TestCase):
    def _parse(self, **extra):
        return codex_council._parse_roles_json(json.dumps([_role_json(**extra)]))

    def test_omitted_keys_inherit(self):
        role = self._parse()[0]
        self.assertIsNone(role.model)
        self.assertIsNone(role.effort)

    def test_valid_model_and_effort_are_kept(self):
        for model in ("gpt-6-luna", "gpt-5.6-codex", "org/model:tag_1"):
            with self.subTest(model=model):
                role = self._parse(model=model, effort="low")[0]
                self.assertEqual(role.model, model)
                self.assertEqual(role.effort, "low")

    def test_unlisted_but_well_shaped_effort_is_accepted(self):
        # No hardcoded value list: codex validates the value itself.
        self.assertEqual(self._parse(effort="ultra")[0].effort, "ultra")
        self.assertEqual(self._parse(effort="futurelevel")[0].effort, "futurelevel")

    def test_malformed_model_rejected_with_rewrite_recovery(self):
        for bad in ("", "-m", " gpt", "gpt 6", "gpt-6\n", "gpt x", None, 6,
                    ["gpt-6"], ".hidden"):
            with self.subTest(model=bad):
                err = _assert_usage_exit(
                    self, lambda bad=bad: self._parse(model=bad),
                    expect_in_stderr="optional field 'model'",
                )
                self.assertIn("rewrite the entire file", err)
                self.assertIn("omit the key to inherit", err)

    def test_malformed_effort_rejected(self):
        for bad in ("", "High", "x-high", "low\n", "low ", 'low"', None, 3):
            with self.subTest(effort=bad):
                _assert_usage_exit(
                    self, lambda bad=bad: self._parse(effort=bad),
                    expect_in_stderr="optional field 'effort'",
                )

    def test_unknown_keys_still_rejected_and_message_names_optional_keys(self):
        err = _assert_usage_exit(
            self, lambda: self._parse(reasoning="high"),
            expect_in_stderr="unknown field(s) 'reasoning'",
        )
        self.assertIn("optionally 'model' and 'effort'", err)


class CommandOverrideTests(unittest.TestCase):
    def test_no_overrides_keep_the_exact_old_commands(self):
        self.assertEqual(
            codex_council._fresh_cmd("/r"),
            ["codex", "exec", "-C", "/r",
             "--dangerously-bypass-approvals-and-sandbox",
             "--json", "--skip-git-repo-check", "-"],
        )
        self.assertEqual(
            codex_council._resume_cmd("/r", "sid"),
            ["codex", "exec", "-C", "/r", "resume", "sid",
             "--dangerously-bypass-approvals-and-sandbox",
             "--skip-git-repo-check", "--json", "-"],
        )

    def test_fresh_places_overrides_on_parent_exec(self):
        cmd = codex_council._fresh_cmd("/r", "gpt-6-luna", "low")
        self.assertEqual(
            cmd[:8],
            ["codex", "exec", "-C", "/r", "-m", "gpt-6-luna",
             "-c", 'model_reasoning_effort="low"'],
        )
        self.assertEqual(cmd[-1], "-")

    def test_resume_places_overrides_before_resume_keyword(self):
        cmd = codex_council._resume_cmd("/r", "sid", "gpt-6-astra", "max")
        self.assertLess(cmd.index("-m"), cmd.index("resume"))
        self.assertLess(cmd.index("-c"), cmd.index("resume"))
        self.assertEqual(cmd[cmd.index("-m") + 1], "gpt-6-astra")
        self.assertEqual(cmd[cmd.index("-c") + 1], 'model_reasoning_effort="max"')
        self.assertEqual(cmd[cmd.index("resume") + 1], "sid")

    def test_model_only_or_effort_only(self):
        self.assertNotIn("-c", codex_council._fresh_cmd("/r", "gpt-6-sol", None))
        self.assertNotIn("-m", codex_council._fresh_cmd("/r", None, "high"))


class RunRoleOnceOverrideTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for patcher in (
            patch.object(codex_council, "_project_root",
                         return_value=FIXED_PROJECT_ROOT),
            patch.object(codex_council, "STATE_DIR", self.tmp.name),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_fresh_then_resume_both_carry_overrides(self):
        seen = []

        async def fake_subproc(cmd, prompt, role_id=None):
            seen.append(cmd)
            jsonl = "\n".join([
                json.dumps({"type": "thread.started", "thread_id": "sid-1"}),
                json.dumps({"type": "item.completed",
                            "item": {"type": "agent_message", "text": "ok"}}),
            ])
            return codex_council.CodexRun(returncode=0, stdout=jsonl, stderr="")

        role = _make_role(model="gpt-6-luna", effort="low")
        with patch.object(codex_council, "_run_codex_subprocess",
                          side_effect=fake_subproc), \
             contextlib.redirect_stderr(io.StringIO()):
            first = await codex_council._run_role_once(role, "p", 1)
            second = await codex_council._run_role_once(role, "p", 1)
        self.assertTrue(first.ok and second.ok)
        self.assertNotIn("resume", seen[0])
        self.assertIn("resume", seen[1])
        for cmd in seen:
            self.assertEqual(cmd[cmd.index("-m") + 1], "gpt-6-luna")
            self.assertIn('model_reasoning_effort="low"', cmd)


class ItemErrorWarningTests(unittest.IsolatedAsyncioTestCase):
    MISMATCH = ("This session was recorded with model `gpt-6-sol` but is "
                "resuming with `gpt-6-luna`.")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for patcher in (
            patch.object(codex_council, "_project_root",
                         return_value=FIXED_PROJECT_ROOT),
            patch.object(codex_council, "STATE_DIR", self.tmp.name),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _jsonl(self, with_item_error):
        events = [{"type": "thread.started", "thread_id": "sid-1"}]
        if with_item_error:
            events.append({"type": "item.completed",
                           "item": {"type": "error", "message": self.MISMATCH}})
        events.append({"type": "item.completed",
                       "item": {"type": "agent_message", "text": "OK2"}})
        return "\n".join(json.dumps(e) for e in events)

    def test_extract_item_errors(self):
        self.assertEqual(
            codex_council.extract_item_errors(self._jsonl(True)),
            [self.MISMATCH])
        self.assertEqual(codex_council.extract_item_errors(self._jsonl(False)),
                         [])

    async def test_item_error_on_successful_resume_becomes_warning(self):
        outputs = [self._jsonl(False), self._jsonl(True)]

        async def fake_subproc(cmd, prompt, role_id=None):
            return codex_council.CodexRun(
                returncode=0, stdout=outputs.pop(0), stderr="")

        role = _make_role(model="gpt-6-luna")
        with patch.object(codex_council, "_run_codex_subprocess",
                          side_effect=fake_subproc), \
             contextlib.redirect_stderr(io.StringIO()):
            first = await codex_council._run_role_once(role, "p", 1)
            second = await codex_council._run_role_once(role, "p", 1)
        self.assertTrue(first.ok and second.ok)
        self.assertIsNone(first.warning)
        self.assertEqual(second.text, "OK2")
        self.assertIn("codex reported: " + self.MISMATCH, second.warning)


# ---------- report / reply-file rendering ----------

class ReportOverridesTests(unittest.TestCase):
    def _r(self, role, ok=True, **kw):
        return codex_council.RoleResult(
            role=role, ok=ok, elapsed_seconds=1.5,
            text=kw.pop("text", "reply" if ok else None), **kw,
        )

    def test_summary_shows_model_and_effort_only_when_set(self):
        out = codex_council._format_report([
            self._r(_make_role("a", "A", "gpt-6-luna", "low")),
            self._r(_make_role("b", "B", None, "high")),
            self._r(_make_role("c", "C")),
        ], 2.0)
        self.assertIn("- **A** [a]: ok (model: gpt-6-luna, effort: low) — 1.5s", out)
        self.assertIn("- **B** [b]: ok (effort: high) — 1.5s", out)
        self.assertIn("- **C** [c]: ok — 1.5s", out)

    def test_report_is_header_summary_plus_the_shared_sections(self):
        results = [
            self._r(_make_role("a", "A"), warning="w1"),
            self._r(_make_role("b", "B"), ok=False, error="boom\nline2"),
        ]
        report = codex_council._format_report(results, 1.0)
        for r in results:
            section = "\n".join(codex_council._format_role_section(r)).rstrip()
            self.assertIn(section, report)

    def test_reply_file_is_header_plus_exact_section(self):
        r = self._r(_make_role("a", "Lab el", "gpt-6-sol", "max"),
                    ok=False, error="boom", attempts=2, warning="careful")
        content = codex_council._format_reply_file(r)
        header, _, rest = content.partition("\n\n")
        self.assertEqual(
            header,
            "<!-- codex-council reply id=a status=FAILED elapsed=1.5s "
            "attempts=2 model=gpt-6-sol effort=max warning=yes -->",
        )
        self.assertEqual(
            rest,
            "\n".join(codex_council._format_role_section(r)).rstrip() + "\n",
        )
        self.assertIn("_Failed: boom_", rest)
        self.assertIn("Lab\\u2028el", rest)  # label escaped like out.md


# ---------- reply files: directory + atomic write ----------

class RepliesDirTests(unittest.TestCase):
    def setUp(self):
        self.run_dir = _private_tmpdir(self)

    def _prepare(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            path = codex_council._prepare_replies_dir(self.run_dir)
        return path, buf.getvalue()

    def test_creates_private_dir(self):
        path, err = self._prepare()
        self.assertEqual(path, os.path.join(self.run_dir, "replies"))
        st = os.lstat(path)
        self.assertTrue(stat.S_ISDIR(st.st_mode))
        self.assertEqual(stat.S_IMODE(st.st_mode) & 0o077, 0)
        self.assertEqual(err, "")

    def test_existing_private_dir_is_reused(self):
        os.mkdir(os.path.join(self.run_dir, "replies"), 0o700)
        path, err = self._prepare()
        self.assertIsNotNone(path)
        self.assertEqual(err, "")

    def test_symlink_is_refused_with_one_warning(self):
        target = _private_tmpdir(self)
        os.symlink(target, os.path.join(self.run_dir, "replies"))
        path, err = self._prepare()
        self.assertIsNone(path)
        self.assertIn("reply files disabled", err)
        self.assertIn("symlink", err)
        self.assertEqual(len(err.strip().splitlines()), 1)

    def test_group_accessible_dir_is_refused(self):
        p = os.path.join(self.run_dir, "replies")
        os.mkdir(p, 0o700)
        os.chmod(p, 0o750)
        path, err = self._prepare()
        self.assertIsNone(path)
        self.assertIn("not private", err)

    def test_regular_file_is_refused(self):
        with open(os.path.join(self.run_dir, "replies"), "w"):
            pass
        path, err = self._prepare()
        self.assertIsNone(path)
        self.assertIn("not a directory", err)

    def test_linebreak_in_path_disables_reply_files(self):
        weird = os.path.join(self.run_dir, "a\nb")
        os.mkdir(weird, 0o700)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            self.assertIsNone(codex_council._prepare_replies_dir(weird))
        self.assertIn("line break", buf.getvalue())


class WriteReplyFileTests(unittest.TestCase):
    def setUp(self):
        self.replies = os.path.join(_private_tmpdir(self), "replies")
        os.mkdir(self.replies, 0o700)

    def _result(self, rid="architect", text="hello"):
        return codex_council.RoleResult(
            role=_make_role(rid, "Architect"), ok=True, text=text,
            elapsed_seconds=3.0,
        )

    def test_atomic_private_file_and_no_temp_leftovers(self):
        r = self._result()
        path = codex_council._write_reply_file(self.replies, r)
        self.assertEqual(path, os.path.join(self.replies, "architect.md"))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), codex_council._format_reply_file(r))
        self.assertEqual(os.listdir(self.replies), ["architect.md"])

    def test_rewrite_replaces_previous_content(self):
        codex_council._write_reply_file(self.replies, self._result(text="one"))
        path = codex_council._write_reply_file(self.replies, self._result(text="two"))
        with open(path, encoding="utf-8") as f:
            self.assertIn("two", f.read())
        self.assertEqual(os.listdir(self.replies), ["architect.md"])

    def test_long_id_uses_state_hash_component(self):
        rid = "x" * 40
        path = codex_council._write_reply_file(self.replies, self._result(rid))
        digest = hashlib.sha256(rid.encode("utf-8")).hexdigest()
        self.assertEqual(os.path.basename(path), f"role-sha256-{digest}.md")

    def test_unencodable_text_is_replaced_not_fatal(self):
        path = codex_council._write_reply_file(
            self.replies, self._result(text="bad \udc80 surrogate"))
        self.assertIsNotNone(path)

    def test_failure_returns_none_with_diag_and_never_raises(self):
        gone = os.path.join(self.replies, "missing-subdir")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            self.assertIsNone(
                codex_council._write_reply_file(gone, self._result()))
        self.assertIn("reply file not written", buf.getvalue())

    def test_failed_replace_removes_temp_file(self):
        buf = io.StringIO()
        with patch.object(codex_council.os, "replace",
                          side_effect=OSError("boom")), \
             contextlib.redirect_stderr(buf):
            self.assertIsNone(
                codex_council._write_reply_file(self.replies, self._result()))
        self.assertEqual(os.listdir(self.replies), [])


class RunCouncilReplyFilesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.replies = os.path.join(_private_tmpdir(self), "replies")
        os.mkdir(self.replies, 0o700)
        for patcher in (
            patch.object(codex_council, "_project_root",
                         return_value=FIXED_PROJECT_ROOT),
            patch.object(codex_council, "STATE_DIR", self.tmp.name),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def _run(self, roles, fake_role, replies_dir):
        lines = []
        checks = []

        def fake_diag(message):
            lines.append(message)
            m = re.search(r" reply=(\S+)$", message)
            if m:
                # The file must already be complete when its line is emitted.
                with open(m.group(1), encoding="utf-8") as f:
                    checks.append((message, f.read()))

        with patch.object(codex_council, "_run_role_attempts",
                          side_effect=fake_role), \
             patch.object(codex_council, "_diag", side_effect=fake_diag):
            results = await codex_council.run_council(
                roles, "body", max_parallel=2, replies_dir=replies_dir)
        return results, lines, checks

    async def test_each_settled_role_writes_file_before_its_line(self):
        async def fake_role(role, prompt):
            if role.id == "security":
                await asyncio.sleep(0.05)
                return codex_council.RoleResult(
                    role=role, ok=False, error="boom", elapsed_seconds=0.2)
            return codex_council.RoleResult(
                role=role, ok=True, text="fast reply", elapsed_seconds=0.1)

        roles = [_make_role("architect"), _make_role("security", "Security")]
        results, lines, checks = await self._run(roles, fake_role, self.replies)
        self.assertEqual(len(checks), 2)
        self.assertRegex(
            checks[0][0],
            r"^\[codex-council\] 1/2 architect: ok \(0\.1s\) reply="
            + re.escape(os.path.join(self.replies, "architect.md")) + "$",
        )
        self.assertRegex(checks[1][0],
                         r"^\[codex-council\] 2/2 security: FAILED \(0\.2s\) reply=")
        self.assertIn("fast reply", checks[0][1])
        self.assertIn("_Failed: boom_", checks[1][1])
        # out.md sections and reply files render identically.
        report = codex_council._format_report(results, 1.0)
        for (_, content), r in zip(checks, results):
            body = content.partition("\n\n")[2].rstrip()
            self.assertIn(body, report)

    async def test_crashed_role_gets_a_reply_file_too(self):
        async def fake_role(role, prompt):
            raise RuntimeError("kaboom")

        results, lines, checks = await self._run(
            [_make_role("architect")], fake_role, self.replies)
        self.assertEqual(len(checks), 1)
        self.assertRegex(
            checks[0][0],
            r"^\[codex-council\] 1/1 architect: crashed \(RuntimeError\) reply=",
        )
        self.assertIn("[orchestrator-exception] RuntimeError: kaboom", checks[0][1])
        self.assertIn("[orchestrator-exception] RuntimeError: kaboom",
                      results[0].error)

    async def test_no_replies_dir_means_no_files_and_old_line_format(self):
        async def fake_role(role, prompt):
            return codex_council.RoleResult(
                role=role, ok=True, text="x", elapsed_seconds=0.1)

        results, lines, checks = await self._run(
            [_make_role("architect")], fake_role, None)
        self.assertEqual(checks, [])
        self.assertIn("[codex-council] 1/1 architect: ok (0.1s)", lines)
        self.assertEqual(os.listdir(self.replies), [])

    async def test_write_failure_keeps_result_and_drops_suffix(self):
        async def fake_role(role, prompt):
            return codex_council.RoleResult(
                role=role, ok=True, text="x", elapsed_seconds=0.1)

        os.rmdir(self.replies)
        results, lines, checks = await self._run(
            [_make_role("architect")], fake_role, self.replies)
        self.assertTrue(results[0].ok)
        self.assertIn("[codex-council] 1/1 architect: ok (0.1s)", lines)
        self.assertTrue(any("reply file not written" in ln for ln in lines))


# ---------- --follow ----------

class FollowArgTests(unittest.TestCase):
    def test_follow_is_exclusive_with_launch_and_preflight_flags(self):
        for other in (["--roles-file", "r.json"], ["--context-file", "c.md"],
                      ["--check-staging-dir", "d"]):
            with self.subTest(other=other):
                _assert_usage_exit(
                    self,
                    lambda other=other: codex_council._parse_args(
                        ["--follow", "/x", *other]),
                    expect_in_stderr="--follow cannot be combined with",
                )

    def test_follow_accepts_skill_contract(self):
        args = codex_council._parse_args(["--follow", "/x", "--skill-contract", "2"])
        self.assertEqual(args.follow, "/x")

    def test_empty_follow_rejected(self):
        _assert_usage_exit(self, lambda: codex_council._parse_args(["--follow", ""]),
                           expect_in_stderr="--follow must be non-empty")


DONE_LINE = ("[codex-council] CODEX_COUNCIL_DONE ok=1 total=1 elapsed=1.0s "
             "exit=0 version=0.10.0")
DISPATCH_LINE = ("[codex-council] dispatching 1 roles with max parallel 6 "
                 "(architect); version=0.10.0.")


class FollowInProcessTests(unittest.TestCase):
    """Drive _follow() directly with shortened poll/start windows."""

    def setUp(self):
        self.run_dir = _private_tmpdir(self)
        self.log = os.path.join(self.run_dir, "err.log")
        for patcher in (
            patch.object(codex_council, "FOLLOW_POLL_SECS", 0.02),
            patch.object(codex_council, "FOLLOW_START_SECS", 0.3),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _write(self, text, mode="a"):
        with open(self.log, mode, encoding="utf-8") as f:
            f.write(text)

    def _follow(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = codex_council._follow(self.run_dir)
        return code, out.getvalue().splitlines()

    def test_relays_only_prefixed_lines_and_exits_0_on_sentinel(self):
        reply = os.path.join(self.run_dir, "replies", "architect.md")
        self._write("\n".join([
            DISPATCH_LINE,
            "[codex-council] architect: started (fresh) attempt=1/2 watchdog=1800s",
            "some unrelated stderr text",
            "x [codex-council] CODEX_COUNCIL_DONE ok=9 total=9 elapsed=0s exit=0 version=1",
            "[codex-council:architect] retriable error on attempt 1/2; sleeping 5s.",
            f"[codex-council] 1/1 architect: ok (1.0s) reply={reply}",
            DONE_LINE,
            "[codex-council] after the sentinel",
        ]) + "\n")
        code, lines = self._follow()
        self.assertEqual(code, 0)
        self.assertEqual(lines[0], DISPATCH_LINE)
        self.assertEqual(lines[-1], DONE_LINE)
        self.assertNotIn("some unrelated stderr text", lines)
        self.assertFalse(any("ok=9" in ln for ln in lines))
        self.assertNotIn("[codex-council] after the sentinel", lines)
        self.assertEqual(len(lines), 5)

    def test_drops_completion_lines_naming_paths_outside_replies(self):
        # Roles run unsandboxed as the same user and can append to err.log;
        # a forged reply= must not point Claude at an arbitrary file.
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

    def test_interruption_line_is_terminal(self):
        self._write(DISPATCH_LINE + "\n\n[codex-council] interrupted by SIGTERM\n")
        code, lines = self._follow()
        self.assertEqual(code, 0)
        self.assertEqual(lines[-1], "[codex-council] interrupted by SIGTERM")

    def test_line_arriving_in_pieces_is_emitted_once_complete(self):
        self._write(DISPATCH_LINE + "\n")

        def writer():
            time.sleep(0.1)
            self._write(DONE_LINE[:20])
            time.sleep(0.1)
            self._write(DONE_LINE[20:] + "\n")

        t = threading.Thread(target=writer)
        t.start()
        code, lines = self._follow()
        t.join()
        self.assertEqual(code, 0)
        self.assertEqual(lines, [DISPATCH_LINE, DONE_LINE])

    def test_missing_err_log_exits_3(self):
        code, lines = self._follow()
        self.assertEqual(code, codex_council.FOLLOW_EXIT_NO_ACTIVITY)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith(
            "[codex-council-follow] no council activity:"))
        self.assertIn("did not appear", lines[0])

    def test_err_log_without_dispatch_exits_3(self):
        self._write("codex-council input staging error:\n- bad\n")
        code, lines = self._follow()
        self.assertEqual(code, 3)
        self.assertIn("no dispatch line", lines[-1])

    def test_err_log_appearing_late_is_followed(self):
        def writer():
            time.sleep(0.1)
            self._write(DISPATCH_LINE + "\n" + DONE_LINE + "\n")

        t = threading.Thread(target=writer)
        t.start()
        code, lines = self._follow()
        t.join()
        self.assertEqual(code, 0)
        self.assertEqual(lines, [DISPATCH_LINE, DONE_LINE])

    def test_silent_dispatched_council_is_presumed_gone(self):
        self._write(DISPATCH_LINE + "\n")
        old = time.time() - codex_council.FOLLOW_SILENCE_SECS - 10
        os.utime(self.log, (old, old))
        code, lines = self._follow()
        self.assertEqual(code, codex_council.FOLLOW_EXIT_RUNNER_GONE)
        self.assertEqual(lines[0], DISPATCH_LINE)
        self.assertTrue(lines[-1].startswith(
            "[codex-council-follow] runner presumed gone"))

    def test_runner_aborted_line_is_terminal(self):
        aborted = ("[codex-council] runner aborted exit=1: stdout "
                   "unavailable; the report was not delivered")
        self._write(DISPATCH_LINE + "\n" + aborted + "\n[codex-council] x\n")
        code, lines = self._follow()
        self.assertEqual(code, 0)
        self.assertEqual(lines, [DISPATCH_LINE, aborted])

    def test_system_suspend_restarts_the_silence_count(self):
        # err.log is not yet silent long enough; then the wall clock jumps
        # an hour while the monotonic clock does not (a laptop suspend).
        # Without suspend detection that jump alone would trip exit 4.
        self._write(DISPATCH_LINE + "\n")
        recent = time.time() - codex_council.FOLLOW_SILENCE_SECS + 30
        os.utime(self.log, (recent, recent))
        real_time = time.time
        calls = {"n": 0}

        def jumped_time():
            calls["n"] += 1
            return real_time() + (3600 if calls["n"] > 1 else 0)

        def writer():
            time.sleep(0.3)
            with open(self.log, "a", encoding="utf-8") as f:
                f.write(DONE_LINE + "\n")

        t = threading.Thread(target=writer)
        t.start()
        with patch.object(codex_council.time, "time", jumped_time):
            code, lines = self._follow()
        t.join()
        self.assertEqual(code, 0, lines)
        self.assertEqual(lines, [DISPATCH_LINE, DONE_LINE])

    def test_traceback_is_advisory_not_terminal(self):
        self._write(DISPATCH_LINE + "\nTraceback (most recent call last):\n"
                    "  File x\nTraceback (most recent call last):\n"
                    + DONE_LINE + "\n")
        code, lines = self._follow()
        self.assertEqual(code, 0)
        notes = [ln for ln in lines if "Python traceback" in ln]
        self.assertEqual(len(notes), 1)
        self.assertEqual(lines[-1], DONE_LINE)

    def test_replaced_err_log_is_reread_from_start(self):
        self._write("[codex-council] old run line\n")

        def relaunch():
            time.sleep(0.1)
            tmp = self.log + ".new"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(DISPATCH_LINE + "\n" + DONE_LINE + "\n")
            os.replace(tmp, self.log)

        t = threading.Thread(target=relaunch)
        t.start()
        code, lines = self._follow()
        t.join()
        self.assertEqual(code, 0)
        self.assertEqual(lines, ["[codex-council] old run line",
                                 DISPATCH_LINE, DONE_LINE])

    def test_non_private_or_symlink_dir_is_usage_error(self):
        os.chmod(self.run_dir, 0o755)
        self.addCleanup(os.chmod, self.run_dir, 0o700)
        _assert_usage_exit(self, lambda: codex_council._follow(self.run_dir),
                           expect_in_stderr="--follow: ")
        link = self.run_dir + "-link"
        os.chmod(self.run_dir, 0o700)
        os.symlink(self.run_dir, link)
        self.addCleanup(os.remove, link)
        _assert_usage_exit(self, lambda: codex_council._follow(link),
                           expect_in_stderr="is a symlink")

    def test_non_regular_err_log_is_usage_error(self):
        os.mkfifo(self.log)
        _assert_usage_exit(self, lambda: codex_council._follow(self.run_dir),
                           expect_in_stderr="not a regular file")

    def test_follow_never_writes(self):
        self._write(DISPATCH_LINE + "\n" + DONE_LINE + "\n")
        before = sorted(os.listdir(self.run_dir))
        with open(self.log, "rb") as f:
            content = f.read()
        self._follow()
        self.assertEqual(sorted(os.listdir(self.run_dir)), before)
        with open(self.log, "rb") as f:
            self.assertEqual(f.read(), content)


# ---------- end to end (fake codex on PATH) ----------

class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.bindir = tempfile.TemporaryDirectory()
        self.addCleanup(self.bindir.cleanup)
        self.statedir = tempfile.TemporaryDirectory()
        self.addCleanup(self.statedir.cleanup)
        self.run_dir = _private_tmpdir(self)
        self.argv_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.argv_dir.cleanup)
        fake = os.path.join(self.bindir.name, "codex")
        with open(fake, "w", encoding="utf-8") as f:
            f.write(FAKE_CODEX)
        os.chmod(fake, 0o755)
        self.env = dict(os.environ)
        self.env["PATH"] = self.bindir.name + os.pathsep + self.env.get("PATH", "")
        self.env["XDG_STATE_HOME"] = self.statedir.name
        self.env["CODEX_HOME"] = self.statedir.name
        self.env["FAKE_CODEX_ARGV_DIR"] = self.argv_dir.name
        for name in ("CODEX_COUNCIL_SESSION_KEY", "CODEX_COUNCIL_MAX_PARALLEL",
                     "CODEX_COUNCIL_STALL_SECS"):
            self.env.pop(name, None)

    def _stage(self, roles):
        with open(os.path.join(self.run_dir, "roles.json"), "w",
                  encoding="utf-8") as f:
            json.dump(roles, f)
        with open(os.path.join(self.run_dir, "context.md"), "w",
                  encoding="utf-8") as f:
            f.write("please review\n")

    def _launch_args(self):
        return [sys.executable, SCRIPT,
                "--roles-file", os.path.join(self.run_dir, "roles.json"),
                "--context-file", os.path.join(self.run_dir, "context.md"),
                "--skill-contract", "2"]

    def _launch_redirected(self):
        """Launch like SKILL.md does: stdout > out.md, stderr > err.log."""
        out = open(os.path.join(self.run_dir, "out.md"), "wb")
        err = open(os.path.join(self.run_dir, "err.log"), "wb")
        self.addCleanup(out.close)
        self.addCleanup(err.close)
        proc = subprocess.Popen(
            self._launch_args(), stdin=subprocess.DEVNULL, stdout=out,
            stderr=err, env=self.env, cwd=self.run_dir)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return proc

    def _follow_proc(self):
        return subprocess.Popen(
            [sys.executable, SCRIPT, "--follow", self.run_dir,
             "--skill-contract", "2"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=self.env)

    def test_staged_run_writes_reply_files_and_follow_streams_to_done(self):
        self._stage([
            _role_json("architect", "Architect", model="gpt-6-luna", effort="low"),
            _role_json("security", "Security",
                       instruction=_instruction(f"Review {FAIL_SENTINEL}")),
        ])
        launch = self._launch_redirected()
        follower = self._follow_proc()
        stdout, stderr = follower.communicate(timeout=60)
        self.assertEqual(follower.returncode, 0, stderr)
        self.assertEqual(launch.wait(timeout=60), 0)
        lines = stdout.splitlines()
        self.assertTrue(lines[0].startswith("[codex-council] dispatching 2 roles"))
        self.assertRegex(lines[-1], codex_council.FOLLOW_DONE_PATTERN)
        replies = os.path.join(self.run_dir, "replies")
        self.assertEqual(stat.S_IMODE(os.lstat(replies).st_mode), 0o700)
        for rid, status in (("architect", "ok"), ("security", "FAILED")):
            path = os.path.join(replies, f"{rid}.md")
            self.assertTrue(any(
                re.fullmatch(
                    rf"\[codex-council\] \d/2 {rid}: {status} \([\d.]+s\) "
                    rf"reply={re.escape(path)}", ln)
                for ln in lines), lines)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(os.path.join(replies, "architect.md"), encoding="utf-8") as f:
            reply = f.read()
        self.assertTrue(reply.startswith(
            "<!-- codex-council reply id=architect status=ok "))
        self.assertIn("model=gpt-6-luna effort=low", reply)
        self.assertIn("fake reply from codex", reply)
        with open(os.path.join(self.run_dir, "out.md"), encoding="utf-8") as f:
            report = f.read()
        self.assertIn("[architect]: ok (model: gpt-6-luna, effort: low)", report)
        self.assertIn(reply.partition("\n\n")[2].rstrip(), report)
        # The real command line carried the overrides on the parent exec.
        argvs = []
        for name in os.listdir(self.argv_dir.name):
            with open(os.path.join(self.argv_dir.name, name)) as f:
                argvs.append(json.load(f))
        with_model = [a for a in argvs if "-m" in a]
        self.assertEqual(len(with_model), 1)
        self.assertEqual(
            with_model[0][:7],
            ["exec", "-C", with_model[0][2], "-m", "gpt-6-luna",
             "-c", 'model_reasoning_effort="low"'])
        without = [a for a in argvs if "-m" not in a]
        self.assertTrue(without and all("-c" not in a for a in without))

    def test_dead_stdout_logs_runner_aborted_and_follow_exits(self):
        self._stage([_role_json("architect", "Architect")])
        err = open(os.path.join(self.run_dir, "err.log"), "wb")
        self.addCleanup(err.close)
        read_end, write_end = os.pipe()
        os.close(read_end)  # nobody will ever read the report
        try:
            proc = subprocess.Popen(
                self._launch_args(), stdin=subprocess.DEVNULL,
                stdout=write_end, stderr=err, env=self.env, cwd=self.run_dir)
        finally:
            os.close(write_end)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.assertEqual(proc.wait(timeout=60), 1)
        follower = self._follow_proc()
        stdout, stderr = follower.communicate(timeout=60)
        self.assertEqual(follower.returncode, 0, stderr)
        lines = stdout.splitlines()
        self.assertRegex(lines[-1], codex_council.FOLLOW_ABORTED_PATTERN)
        self.assertIn("stdout unavailable", lines[-1])
        self.assertFalse(any(codex_council.FOLLOW_DONE_PATTERN.match(ln)
                             for ln in lines))

    def test_reply_files_survive_sigterm_and_follow_exits_on_interruption(self):
        self._stage([
            _role_json("fast", "Fast"),
            _role_json("slow", "Slow",
                       instruction=_instruction(f"Review {HANG_SENTINEL}")),
        ])
        launch = self._launch_redirected()
        err_log = os.path.join(self.run_dir, "err.log")
        fast = os.path.join(self.run_dir, "replies", "fast.md")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            with open(err_log, encoding="utf-8") as f:
                if f"reply={fast}" in f.read():
                    break
            time.sleep(0.05)
        else:
            self.fail("fast role never reported its reply file")
        time.sleep(0.2)  # let the hanging role start
        launch.send_signal(signal.SIGTERM)
        self.assertEqual(launch.wait(timeout=60), 128 + signal.SIGTERM)
        # The finished role's reply is still on disk and complete.
        with open(fast, encoding="utf-8") as f:
            self.assertIn("fake reply from codex", f.read())
        self.assertFalse(os.path.exists(
            os.path.join(self.run_dir, "replies", "slow.md")))
        with open(err_log, encoding="utf-8") as f:
            log = f.read()
        self.assertNotIn("CODEX_COUNCIL_DONE", log)
        # A (re-armed) follower replays history and stops on the interruption.
        follower = self._follow_proc()
        stdout, stderr = follower.communicate(timeout=30)
        self.assertEqual(follower.returncode, 0, stderr)
        self.assertEqual(stdout.splitlines()[-1],
                         "[codex-council] interrupted by SIGTERM")

    def test_stdin_mode_uses_roles_file_directory(self):
        with open(os.path.join(self.run_dir, "roles.json"), "w",
                  encoding="utf-8") as f:
            json.dump([_role_json("architect", "Architect")], f)
        proc = subprocess.run(
            [sys.executable, SCRIPT, "--roles-file",
             os.path.join(self.run_dir, "roles.json")],
            input="please review\n", capture_output=True, text=True,
            env=self.env, cwd=self.run_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        path = os.path.join(self.run_dir, "replies", "architect.md")
        self.assertIn(f"reply={path}", proc.stderr)
        self.assertTrue(os.path.isfile(path))

    def test_unusable_replies_dir_never_fails_the_council(self):
        self._stage([_role_json("architect", "Architect")])
        with open(os.path.join(self.run_dir, "replies"), "w"):
            pass
        proc = subprocess.run(
            self._launch_args(), capture_output=True, text=True,
            env=self.env, cwd=self.run_dir, stdin=subprocess.DEVNULL)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("reply files disabled", proc.stderr)
        self.assertNotIn("reply=", proc.stderr)
        self.assertIn("fake reply from codex", proc.stdout)
        self.assertRegex(
            [ln for ln in proc.stderr.splitlines() if ln.strip()][-1],
            codex_council.FOLLOW_DONE_PATTERN)

    def test_follow_cli_on_typo_path_is_usage_error(self):
        proc = subprocess.run(
            [sys.executable, SCRIPT, "--follow",
             os.path.join(self.run_dir, "nope")],
            capture_output=True, text=True, env=self.env)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("does not exist", proc.stderr)
        self.assertEqual(proc.stdout, "")


if __name__ == "__main__":
    unittest.main()
