"""Per-role reply files and per-role model/effort overrides.

Covers the replies/ directory and its atomic reply files, the completion
line's reply= path, the report and reply-file rendering of what each role
sent, the placement of -m / -c on the codex command line, Codex's
item-level advisories, and the end-to-end launch with --follow. The
`selection` contract (discovery, routing, [model-rejected]/[quota]) is
covered in tests/test_model_selection.py, and --follow's liveness checks,
--status, and --reap in tests/test_liveness.py.

Unit tests import codex_council directly; end-to-end tests drive the REAL
script as a subprocess with the fake `codex` from tests/fake_codex.py on
PATH (no network, no real Codex) and an isolated XDG_STATE_HOME. Model ids
are synthetic.

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
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import council_testlib  # noqa: E402
import codex_council  # noqa: E402
import council_common  # noqa: E402
import council_liveness  # noqa: E402
from council_selection import Selection  # noqa: E402
from council_testlib import (  # noqa: E402
    EPOCH,
    FIXED_PROJECT_ROOT,
    SCRIPT,
)
from fake_codex import EXEC_SENTINELS  # noqa: E402

HANG_SENTINEL = EXEC_SENTINELS["hang"]
FAIL_SENTINEL = EXEC_SENTINELS["fail"]

setUpModule = council_testlib.install_fake_codex
tearDownModule = council_testlib.remove_fake_codex


def _instruction(text="Review"):
    return f"{text}; if nothing material, say so clearly. Thoroughness beats speed."


def _role_json(rid="alpha", label="A", instruction=None, **extra):
    entry = {"id": rid, "label": label,
             "instruction": [instruction or _instruction()]}
    entry.update(extra)
    return entry


def _make_role(rid="architect", label="Architect", model=None, effort=None):
    """A Role; a model or effort makes it an explicit user pin."""
    selection = Selection("user") if model or effort else None
    return codex_council.Role(rid, label, _instruction(), model, effort,
                              selection)


def _private_tmpdir(test):
    d = tempfile.TemporaryDirectory()
    test.addCleanup(d.cleanup)
    os.chmod(d.name, 0o700)
    return d.name


# ---------- model / effort on the codex command line ----------

class CommandOverrideTests(unittest.TestCase):
    def test_fresh_places_overrides_on_parent_exec(self):
        cmd = codex_council._fresh_cmd("/r", "future-vega-2033", "brisk")
        self.assertEqual(
            cmd[:8],
            ["codex", "exec", "-C", "/r", "-m", "future-vega-2033",
             "-c", 'model_reasoning_effort="brisk"'],
        )
        self.assertEqual(cmd[-1], "-")

    def test_resume_places_overrides_before_resume_keyword(self):
        cmd = codex_council._resume_cmd(
            "/r", "sid", "future-orion-2032", "adaptive-v2")
        self.assertLess(cmd.index("-m"), cmd.index("resume"))
        self.assertLess(cmd.index("-c"), cmd.index("resume"))
        self.assertEqual(cmd[cmd.index("-m") + 1], "future-orion-2032")
        self.assertEqual(cmd[cmd.index("-c") + 1],
                         'model_reasoning_effort="adaptive-v2"')
        self.assertEqual(cmd[cmd.index("resume") + 1], "sid")

    def test_model_only_or_effort_only(self):
        self.assertNotIn(
            "-c", codex_council._fresh_cmd("/r", "future-vega-2033", None))
        self.assertNotIn("-m", codex_council._fresh_cmd("/r", None, "brisk"))


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

        role = _make_role(model="future-vega-2033", effort="brisk")
        with patch.object(codex_council, "_run_codex_subprocess",
                          side_effect=fake_subproc), \
             contextlib.redirect_stderr(io.StringIO()):
            first = await codex_council._run_role_once(role, "p", 1)
            second = await codex_council._run_role_once(role, "p", 1)
        self.assertTrue(first.ok and second.ok)
        self.assertNotIn("resume", seen[0])
        self.assertIn("resume", seen[1])
        for cmd in seen:
            self.assertEqual(cmd[cmd.index("-m") + 1], "future-vega-2033")
            self.assertIn('model_reasoning_effort="brisk"', cmd)


class ItemErrorWarningTests(unittest.IsolatedAsyncioTestCase):
    MISMATCH = ("This session was recorded with model `future-lyra-2030` "
                "but is resuming with `future-vega-2033`.")

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

        role = _make_role(model="future-vega-2033")
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

    def test_summary_shows_sent_model_and_effort_only_when_set(self):
        out = codex_council._format_report([
            self._r(_make_role("a", "A", "future-vega-2033", "brisk")),
            self._r(_make_role("b", "B", None, "deliberate")),
            self._r(_make_role("c", "C")),
        ], 2.0)
        self.assertIn("- **A** [a]: ok (explicit: model future-vega-2033, "
                      "effort brisk) — 1.5s", out)
        self.assertIn("- **B** [b]: ok (explicit: effort deliberate) — 1.5s",
                      out)
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
        r = self._r(_make_role("a", "Lab\u2028el", "future-orion-2032",
                               "deliberate"),
                    ok=False, error="boom", attempts=2, warning="careful")
        content = codex_council._format_reply_file(r)
        header, _, rest = content.partition("\n\n")
        self.assertEqual(
            header,
            "<!-- codex-council reply id=a status=FAILED elapsed=1.5s "
            "attempts=2 selection=user model=future-orion-2032 "
            "effort=deliberate warning=yes -->",
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
        with patch.object(council_common.os, "replace",
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
        results, _, checks = await self._run(roles, fake_role, self.replies)
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
        for _, content in checks:
            body = content.partition("\n\n")[2].rstrip()
            self.assertIn(body, report)

    async def test_stale_resume_warning_reaches_reply_file_and_report(self):
        """A saved thread Codex no longer has: the role reruns fresh, and its
        result, reply file, and report section all say continuity was
        lost."""
        codex_council.save_session("architect", "stale-sid")

        async def fake_subproc(cmd, prompt, role_id=None):
            if "resume" in cmd:
                return codex_council.CodexRun(
                    1, "", "Error: thread/resume failed: no rollout found "
                    "for thread id stale-sid (code -32600)")
            return codex_council.CodexRun(0, "\n".join([
                json.dumps({"type": "thread.started", "thread_id": "new-sid"}),
                json.dumps({"type": "item.completed", "item": {
                    "type": "agent_message", "text": "fresh reply"}}),
            ]), "")

        with patch.object(codex_council, "_run_codex_subprocess",
                          side_effect=fake_subproc), \
             patch.object(codex_council, "_diag"):
            results = await codex_council.run_council(
                [_make_role("architect")], "body", max_parallel=1,
                replies_dir=self.replies)
        warning = codex_council.STALE_RESUME_WARNING
        self.assertTrue(results[0].ok)
        self.assertEqual(results[0].warning, warning)
        self.assertEqual(codex_council.load_session("architect")[0], "new-sid")
        with open(os.path.join(self.replies, "architect.md"),
                  encoding="utf-8") as f:
            reply = f.read()
        self.assertIn(" warning=yes", reply.splitlines()[0])
        self.assertIn(f"_Warning: {warning}_\n\nfresh reply", reply)
        # out.md is this report.
        report = codex_council._format_report(results, 1.0)
        self.assertIn("[architect]: ok — WARNING — ", report)
        self.assertIn(f"_Warning: {warning}_\n\nfresh reply", report)

    async def test_crashed_role_gets_a_reply_file_too(self):
        async def fake_role(role, prompt):
            raise RuntimeError("kaboom")

        results, _, checks = await self._run(
            [_make_role("architect")], fake_role, self.replies)
        self.assertEqual(len(checks), 1)
        self.assertRegex(
            checks[0][0],
            r"^\[codex-council\] 1/1 architect: crashed \(RuntimeError\) reply=",
        )
        self.assertIn("[orchestrator-exception] RuntimeError: kaboom", checks[0][1])
        self.assertIn("[orchestrator-exception] RuntimeError: kaboom",
                      results[0].error)

    async def test_no_replies_dir_means_no_files_and_no_reply_suffix(self):
        async def fake_role(role, prompt):
            return codex_council.RoleResult(
                role=role, ok=True, text="x", elapsed_seconds=0.1)

        _, lines, checks = await self._run(
            [_make_role("architect")], fake_role, None)
        self.assertEqual(checks, [])
        self.assertIn("[codex-council] 1/1 architect: ok (0.1s)", lines)
        self.assertEqual(os.listdir(self.replies), [])

    async def test_write_failure_keeps_result_and_drops_suffix(self):
        async def fake_role(role, prompt):
            return codex_council.RoleResult(
                role=role, ok=True, text="x", elapsed_seconds=0.1)

        os.rmdir(self.replies)
        results, lines, _ = await self._run(
            [_make_role("architect")], fake_role, self.replies)
        self.assertTrue(results[0].ok)
        self.assertIn("[codex-council] 1/1 architect: ok (0.1s)", lines)
        self.assertTrue(any("reply file not written" in ln for ln in lines))


# ---------- end to end (fake codex on PATH) ----------

class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.statedir = tempfile.TemporaryDirectory()
        self.addCleanup(self.statedir.cleanup)
        self.run_dir = _private_tmpdir(self)
        self.argv_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.argv_dir.cleanup)
        self.env = council_testlib.clean_env(
            PATH=council_testlib.fake_bin_dir() + os.pathsep
            + os.environ.get("PATH", ""),
            XDG_STATE_HOME=self.statedir.name,
            CODEX_HOME=self.statedir.name,
            FAKE_CODEX_ARGV_DIR=self.argv_dir.name,
        )

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
                "--skill-contract", EPOCH]

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
             "--skill-contract", EPOCH],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=self.env)

    def test_staged_run_writes_reply_files_and_follow_streams_to_done(self):
        self._stage([
            _role_json("architect", "Architect", model="future-vega-2033",
                       effort="brisk", selection={"mode": "user"}),
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
        # No runtime-grounded selection: the launch ran no discovery.
        self.assertEqual(
            lines[1],
            "[codex-council] model selection: routing=auto; "
            "discovery=not-run (no runtime-grounded selections); native=1 "
            "user=1 routed=0 native_effort=0 fallback=0")
        self.assertRegex(lines[-1], council_liveness.FOLLOW_DONE_PATTERN)
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
        self.assertIn("selection=user model=future-vega-2033 effort=brisk",
                      reply)
        self.assertIn("fake reply from codex", reply)
        with open(os.path.join(self.run_dir, "out.md"), encoding="utf-8") as f:
            report = f.read()
        self.assertIn(
            "[architect]: ok (explicit: model future-vega-2033, effort brisk)",
            report)
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
            ["exec", "-C", with_model[0][2], "-m", "future-vega-2033",
             "-c", 'model_reasoning_effort="brisk"'])
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
        self.assertRegex(lines[-1], council_liveness.FOLLOW_ABORTED_PATTERN)
        self.assertIn("stdout unavailable", lines[-1])
        self.assertFalse(any(council_liveness.FOLLOW_DONE_PATTERN.match(ln)
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
            council_liveness.FOLLOW_DONE_PATTERN)

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
