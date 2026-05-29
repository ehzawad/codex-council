"""Unit tests for codex_council.py.

Runs without the Codex CLI installed. Covers helper behavior:
key/state per role, JSONL parsing, error classifiers, prompt
composition, command shape, the resume-thread-id mismatch footgun,
retry-on-retriable, fan-out aggregation.

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
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

SCRIPTS_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__),
    "..",
    "plugins", "codex-council", "skills", "codex-council", "scripts",
))
sys.path.insert(0, SCRIPTS_DIR)

import codex_council  # noqa: E402


FIXED_PROJECT_ROOT = "/fixed/project/root"
FIXED_PROJECT_HASH = hashlib.sha256(FIXED_PROJECT_ROOT.encode()).hexdigest()[:16]


def _env_without_session_key():
    """Current env minus explicit and auto council session-scope keys."""
    excluded = {
        codex_council.SESSION_KEY_ENV,
        codex_council.DISABLE_AUTO_SESSION_KEY_ENV,
        *codex_council.AUTO_SESSION_ENV_VARS,
    }
    return {k: v for k, v in os.environ.items() if k not in excluded}


def _assert_usage_exit(test, callable_, *, expect_in_stderr):
    """Run callable_; assert it raised SystemExit(2) with expect_in_stderr on stderr."""
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        with test.assertRaises(SystemExit) as ctx:
            callable_()
    test.assertEqual(ctx.exception.code, 2)
    test.assertIn(expect_in_stderr, buf.getvalue())


def _valid_instruction(prefix="x"):
    return (
        f"{prefix}; if nothing material, say so clearly. "
        "Thoroughness beats speed."
    )


def _make_role(rid="test-role", label="Test Role",
               instruction=None):
    """Construct a Role for tests. The script has no built-in catalog,
    so tests build Role instances directly."""
    if instruction is None:
        instruction = _valid_instruction("x")
    return codex_council.Role(rid, label, instruction)


def _role_json(rid="alpha", label="A", instruction=None):
    if instruction is None:
        instruction = _valid_instruction("review")
    return {"id": rid, "label": label, "instruction": instruction}


# ---------- role resolution (custom-only; no built-in catalog) ----------

class ResolveRolesTests(unittest.TestCase):
    """The script accepts roles only via --roles-file; no positional path."""

    def test_no_roles_raises_systemexit(self):
        _assert_usage_exit(
            self, lambda: codex_council._resolve_roles([]),
            expect_in_stderr="No roles requested",
        )

    def test_resolve_returns_roles_in_input_order(self):
        a = _make_role("alpha", "Alpha", _valid_instruction("do alpha"))
        b = _make_role("beta", "Beta", _valid_instruction("do beta"))
        c = _make_role("gamma", "Gamma", _valid_instruction("do gamma"))
        roles = codex_council._resolve_roles([a, b, c])
        self.assertEqual([r.id for r in roles], ["alpha", "beta", "gamma"])

    def test_resolve_deduplicates_by_id_keeping_first(self):
        """Defense in depth — _parse_roles_json already rejects dupes,
        but _resolve_roles should also be safe if called with dupes."""
        a1 = _make_role("alpha", "A1", _valid_instruction("first"))
        a2 = _make_role("alpha", "A2", _valid_instruction("second"))
        roles = codex_council._resolve_roles([a1, a2])
        self.assertEqual([r.id for r in roles], ["alpha"])
        self.assertEqual(roles[0].label, "A1")  # first wins

    def test_resolve_at_cap_is_allowed(self):
        roles_in = [
            _make_role(f"r{i}", f"R{i}", _valid_instruction("x"))
            for i in range(codex_council.MAX_PARALLEL)
        ]
        roles = codex_council._resolve_roles(roles_in)
        self.assertEqual(len(roles), codex_council.MAX_PARALLEL)

    def test_resolve_over_cap_raises(self):
        roles_in = [
            _make_role(f"r{i}", f"R{i}", _valid_instruction("x"))
            for i in range(codex_council.MAX_PARALLEL + 1)
        ]
        _assert_usage_exit(
            self, lambda: codex_council._resolve_roles(roles_in),
            expect_in_stderr="MAX_PARALLEL",
        )


# ---------- env vars / session key ----------

class SessionKeyTests(unittest.TestCase):
    def test_unset_without_auto_returns_empty(self):
        with patch.dict(os.environ, _env_without_session_key(), clear=True):
            self.assertEqual(codex_council._session_key(), "")

    def test_explicit_value_returned(self):
        env = _env_without_session_key()
        env[codex_council.SESSION_KEY_ENV] = "branch-x"
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(codex_council._session_key(), "branch-x")

    def test_whitespace_stripped(self):
        env = _env_without_session_key()
        env[codex_council.SESSION_KEY_ENV] = "  spaced  "
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(codex_council._session_key(), "spaced")

    def test_whitespace_only_is_empty(self):
        env = _env_without_session_key()
        env[codex_council.SESSION_KEY_ENV] = "   "
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(codex_council._session_key(), "")

    def test_auto_session_key_uses_terminal_session_when_no_explicit_key(self):
        env = _env_without_session_key()
        env["TERM_SESSION_ID"] = "term-123"
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(codex_council._session_key(), "TERM_SESSION_ID=term-123")

    def test_explicit_session_key_overrides_auto_session_key(self):
        env = _env_without_session_key()
        env[codex_council.SESSION_KEY_ENV] = "manual"
        env["TERM_SESSION_ID"] = "term-123"
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(codex_council._session_key(), "manual")

    def test_disable_auto_session_key_restores_project_wide_scope(self):
        env = _env_without_session_key()
        env[codex_council.DISABLE_AUTO_SESSION_KEY_ENV] = "1"
        env["TERM_SESSION_ID"] = "term-123"
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(codex_council._session_key(), "")


# ---------- project / state path ----------

class ProjectKeyTests(unittest.TestCase):
    def setUp(self):
        self.project_patcher = patch.object(
            codex_council, "_project_root", return_value=FIXED_PROJECT_ROOT
        )
        self.project_patcher.start()
        self.addCleanup(self.project_patcher.stop)

    def test_role_appears_in_key(self):
        with patch.dict(os.environ, _env_without_session_key(), clear=True):
            key = codex_council._project_key("architect")
        self.assertTrue(key.endswith("__architect"))

    def test_distinct_roles_produce_distinct_keys(self):
        with patch.dict(os.environ, _env_without_session_key(), clear=True):
            a = codex_council._project_key("architect")
            s = codex_council._project_key("security")
        self.assertNotEqual(a, s)

    def test_with_session_key_appends_suffix_before_role(self):
        with patch.dict(os.environ, {codex_council.SESSION_KEY_ENV: "task-1"}, clear=False):
            key = codex_council._project_key("architect")
        suffix = hashlib.sha256(b"task-1").hexdigest()[:16]
        self.assertEqual(key, f"{FIXED_PROJECT_HASH}-{suffix}__architect")

    def test_auto_session_key_appends_suffix_before_role(self):
        env = _env_without_session_key()
        env["TERM_SESSION_ID"] = "term-123"
        with patch.dict(os.environ, env, clear=True):
            key = codex_council._project_key("architect")
        suffix = hashlib.sha256(b"TERM_SESSION_ID=term-123").hexdigest()[:16]
        self.assertEqual(key, f"{FIXED_PROJECT_HASH}-{suffix}__architect")

    def test_no_session_key_returns_project_plus_role(self):
        with patch.dict(os.environ, _env_without_session_key(), clear=True):
            self.assertEqual(
                codex_council._project_key("tester"),
                f"{FIXED_PROJECT_HASH}__tester",
            )

    def test_distinct_session_keys_produce_distinct_keys(self):
        with patch.dict(os.environ, {codex_council.SESSION_KEY_ENV: "alpha"}, clear=False):
            a = codex_council._project_key("architect")
        with patch.dict(os.environ, {codex_council.SESSION_KEY_ENV: "beta"}, clear=False):
            b = codex_council._project_key("architect")
        self.assertNotEqual(a, b)


class StatePathTests(unittest.TestCase):
    def setUp(self):
        self.project_patcher = patch.object(
            codex_council, "_project_root", return_value=FIXED_PROJECT_ROOT
        )
        self.project_patcher.start()
        self.addCleanup(self.project_patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_patcher = patch.object(codex_council, "STATE_DIR", self.tmp.name)
        self.state_patcher.start()
        self.addCleanup(self.state_patcher.stop)

    def test_state_dir_is_plugin_scoped(self):
        # Verify STATE_DIR's source definition uses the codex-council
        # namespace — state stays cleanly scoped to this plugin.
        with open(codex_council.__file__) as f:
            src = f.read()
        self.assertIn('"codex-council"', src)

    def test_each_role_distinct_path(self):
        with patch.dict(os.environ, _env_without_session_key(), clear=True):
            a = codex_council._state_path("architect")
            s = codex_council._state_path("security")
        self.assertNotEqual(a, s)
        self.assertTrue(a.endswith("__architect.json"))
        self.assertTrue(s.endswith("__security.json"))


class StateIOTests(unittest.TestCase):
    def setUp(self):
        self.project_patcher = patch.object(
            codex_council, "_project_root", return_value=FIXED_PROJECT_ROOT
        )
        self.project_patcher.start()
        self.addCleanup(self.project_patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_patcher = patch.object(codex_council, "STATE_DIR", self.tmp.name)
        self.state_patcher.start()
        self.addCleanup(self.state_patcher.stop)

    def test_save_and_load_roundtrip(self):
        with patch.dict(os.environ, _env_without_session_key(), clear=True):
            codex_council.save_session("architect", "sid-xyz")
            sid, meta = codex_council.load_session("architect")
        self.assertEqual(sid, "sid-xyz")
        self.assertEqual(meta["role_id"], "architect")
        self.assertEqual(meta["project_path"], FIXED_PROJECT_ROOT)
        self.assertIn("updated_at", meta)
        self.assertNotIn("session_key", meta)

    def test_save_includes_session_key_when_set(self):
        with patch.dict(os.environ, {codex_council.SESSION_KEY_ENV: "alpha"}, clear=False):
            codex_council.save_session("architect", "s-alpha")
            _, meta = codex_council.load_session("architect")
        self.assertEqual(meta["session_key"], "alpha")

    def test_save_includes_auto_session_key_when_detected(self):
        env = _env_without_session_key()
        env["TERM_SESSION_ID"] = "term-123"
        with patch.dict(os.environ, env, clear=True):
            codex_council.save_session("architect", "s-auto")
            _, meta = codex_council.load_session("architect")
        self.assertEqual(meta["session_key"], "TERM_SESSION_ID=term-123")

    def test_load_missing_returns_none_pair(self):
        with patch.dict(os.environ, _env_without_session_key(), clear=True):
            sid, meta = codex_council.load_session("architect")
        self.assertIsNone(sid)
        self.assertIsNone(meta)

    def test_load_corrupt_returns_none_pair(self):
        with patch.dict(os.environ, _env_without_session_key(), clear=True):
            os.makedirs(self.tmp.name, exist_ok=True)
            with open(codex_council._state_path("architect"), "w") as f:
                f.write("{not json")
            sid, meta = codex_council.load_session("architect")
        self.assertIsNone(sid)
        self.assertIsNone(meta)

    def test_clear_session_removes_only_that_role(self):
        with patch.dict(os.environ, _env_without_session_key(), clear=True):
            codex_council.save_session("architect", "sid-a")
            codex_council.save_session("security", "sid-s")
            codex_council.clear_session("architect")
            a_sid, _ = codex_council.load_session("architect")
            s_sid, _ = codex_council.load_session("security")
        self.assertIsNone(a_sid)
        self.assertEqual(s_sid, "sid-s")

    def test_save_leaves_no_tempfiles(self):
        with patch.dict(os.environ, _env_without_session_key(), clear=True):
            codex_council.save_session("architect", "x")
        leftovers = [f for f in os.listdir(self.tmp.name) if f.startswith(".tmp.")]
        self.assertEqual(leftovers, [])

    def test_two_roles_isolated(self):
        with patch.dict(os.environ, _env_without_session_key(), clear=True):
            codex_council.save_session("architect", "sid-a")
            codex_council.save_session("security", "sid-s")
            a_sid, _ = codex_council.load_session("architect")
            s_sid, _ = codex_council.load_session("security")
        self.assertEqual(a_sid, "sid-a")
        self.assertEqual(s_sid, "sid-s")


# ---------- JSONL parsing ----------

class ExtractSessionIDTests(unittest.TestCase):
    def test_returns_first_thread_started_id(self):
        jsonl = "\n".join([
            '{"type": "thread.started", "thread_id": "uuid-1"}',
            '{"type": "turn.started"}',
        ])
        self.assertEqual(codex_council.extract_session_id(jsonl), "uuid-1")

    def test_returns_none_when_no_thread_started(self):
        self.assertIsNone(codex_council.extract_session_id('{"type":"turn.started"}'))

    def test_tolerates_garbage(self):
        jsonl = "junk\n\n{malformed\n" '{"type":"thread.started","thread_id":"x"}'
        self.assertEqual(codex_council.extract_session_id(jsonl), "x")

    def test_skips_non_object_json_and_invalid_thread_ids(self):
        jsonl = "\n".join([
            "[]",
            "null",
            '{"type":"thread.started","thread_id":123}',
            '{"type":"thread.started","thread_id":""}',
            '{"type":"thread.started","thread_id":"valid"}',
        ])
        self.assertEqual(codex_council.extract_session_id(jsonl), "valid")


class ExtractFinalMessageTests(unittest.TestCase):
    def test_returns_last_agent_message(self):
        jsonl = "\n".join([
            '{"type":"item.completed","item":{"type":"agent_message","text":"first"}}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"last"}}',
        ])
        self.assertEqual(codex_council.extract_final_message(jsonl), "last")

    def test_returns_none_with_no_agent_message(self):
        jsonl = '{"type":"item.completed","item":{"type":"command_execution"}}'
        self.assertIsNone(codex_council.extract_final_message(jsonl))

    def test_skips_non_object_events_items_and_text(self):
        jsonl = "\n".join([
            "[]",
            '{"type":"item.completed","item":null}',
            '{"type":"item.completed","item":[]}',
            '{"type":"item.completed","item":{"type":"agent_message","text":123}}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}',
        ])
        self.assertEqual(codex_council.extract_final_message(jsonl), "ok")


class ExtractErrorMessagesTests(unittest.TestCase):
    def test_extracts_error_message(self):
        jsonl = '{"type":"error","message":"401 unauthorized"}'
        self.assertEqual(codex_council.extract_error_messages(jsonl), ["401 unauthorized"])

    def test_extracts_turn_failed_error_message(self):
        jsonl = '{"type":"turn.failed","error":{"message":"HTTP 429 Too Many Requests"}}'
        self.assertEqual(
            codex_council.extract_error_messages(jsonl),
            ["HTTP 429 Too Many Requests"],
        )

    def test_extracts_nested_codex_error_message_and_dedupes(self):
        inner = json.dumps({
            "type": "error",
            "status": 400,
            "error": {"message": "The model is unsupported."},
        })
        jsonl = "\n".join([
            json.dumps({"type": "error", "message": inner}),
            json.dumps({"type": "turn.failed", "error": {"message": inner}}),
        ])
        self.assertEqual(
            codex_council.extract_error_messages(jsonl),
            [inner, "The model is unsupported."],
        )


# ---------- error classifiers ----------

class ClassifierTests(unittest.TestCase):
    def test_auth_markers_match(self):
        self.assertTrue(codex_council._is_auth_error("401 Unauthorized: incorrect api key sk-..."))
        self.assertTrue(codex_council._is_auth_error("Please run `codex login`"))

    def test_rate_limit_markers_match(self):
        self.assertTrue(codex_council._is_rate_limit_error("HTTP 429 too many requests"))
        self.assertTrue(codex_council._is_rate_limit_error("rate_limit_exceeded"))

    def test_5xx_markers_match(self):
        self.assertTrue(codex_council._is_transient_5xx_error("502 bad gateway"))
        self.assertTrue(codex_council._is_transient_5xx_error("Service unavailable, retry later"))

    def test_stale_markers_match(self):
        self.assertTrue(codex_council._is_stale_resume_error(
            "Error: thread/resume failed: no rollout found for thread id abc (code -32600)"
        ))
        self.assertTrue(codex_council._is_stale_resume_error("THREAD NOT FOUND"))

    def test_retriable_helper_covers_both(self):
        self.assertTrue(codex_council._is_retriable_error("429 too many"))
        self.assertTrue(codex_council._is_retriable_error("503 service unavailable"))
        self.assertFalse(codex_council._is_retriable_error("401 unauthorized"))

    def test_distinct_classes_dont_overlap(self):
        s = "no rollout found for thread id x"
        self.assertTrue(codex_council._is_stale_resume_error(s))
        self.assertFalse(codex_council._is_auth_error(s))
        self.assertFalse(codex_council._is_retriable_error(s))


# ---------- prompt composition ----------

class ComposePromptTests(unittest.TestCase):
    def test_bookends_with_role_instruction(self):
        role = _make_role("architect", "Architect",
                          _valid_instruction("Review as architect"))
        out = codex_council._compose_prompt(role, "BODY")
        self.assertTrue(out.startswith(role.instruction + "\n\n"))
        self.assertTrue(out.endswith("\n\n" + role.instruction))
        self.assertIn("BODY", out)

    def test_different_roles_produce_different_prompts(self):
        a = codex_council._compose_prompt(
            _make_role("architect", "Architect", _valid_instruction("review arch")),
            "x",
        )
        s = codex_council._compose_prompt(
            _make_role("security", "Security", _valid_instruction("review sec")),
            "x",
        )
        self.assertNotEqual(a, s)


# ---------- command shape ----------

class CommandShapeTests(unittest.TestCase):
    def test_resume_places_C_before_resume_keyword(self):
        cmd = codex_council._resume_cmd("/root", "sid-1")
        self.assertIn("-C", cmd)
        self.assertIn("resume", cmd)
        self.assertLess(cmd.index("-C"), cmd.index("resume"))

    def test_fresh_cmd_has_no_resume_keyword(self):
        cmd = codex_council._fresh_cmd("/root")
        self.assertNotIn("resume", cmd)
        self.assertIn("-C", cmd)

    def test_both_use_json_and_stdin_sentinel(self):
        for cmd in [codex_council._fresh_cmd("/r"), codex_council._resume_cmd("/r", "s")]:
            self.assertIn("--json", cmd)
            self.assertEqual(cmd[-1], "-")


# ---------- report formatting ----------

class FormatReportTests(unittest.TestCase):
    _LABELS = {
        "architect": "Architect",
        "security": "Security",
        "tester": "Test engineer",
    }

    def _r(self, role_id, ok, text=None, error=None, attempts=1, elapsed=1.0):
        return codex_council.RoleResult(
            role=_make_role(role_id, self._LABELS[role_id]),
            ok=ok, text=text, error=error,
            elapsed_seconds=elapsed, attempts=attempts,
        )

    def test_header_counts_ok_over_total(self):
        results = [self._r("architect", True, text="A"), self._r("security", False, error="boom")]
        out = codex_council._format_report(results, 5.5)
        self.assertIn("1/2 roles responded", out)
        self.assertIn("5.5s", out)

    def test_summary_section_lists_each_role(self):
        results = [self._r("architect", True, text="A", elapsed=1.2)]
        out = codex_council._format_report(results, 1.2)
        self.assertIn("**Architect**", out)
        self.assertIn("[architect]", out)

    def test_failed_role_uses_italic_failed_marker(self):
        out = codex_council._format_report([self._r("security", False, error="boom")], 0.1)
        self.assertIn("_Failed: boom_", out)

    def test_attempts_note_shown_only_when_retried(self):
        out_no = codex_council._format_report([self._r("architect", True, text="x", attempts=1)], 0.1)
        out_yes = codex_council._format_report([self._r("architect", True, text="x", attempts=2)], 0.1)
        self.assertNotIn("attempts:", out_no)
        self.assertIn("attempts: 2", out_yes)

    def test_role_order_preserved(self):
        results = [
            self._r("tester", True, text="T"),
            self._r("architect", True, text="A"),
        ]
        out = codex_council._format_report(results, 0.1)
        self.assertLess(out.index("Test engineer"), out.index("Architect"))


# ---------- async role runner ----------

def _fresh_jsonl(thread_id="new-sid", text="ok"):
    return "\n".join([
        json.dumps({"type": "thread.started", "thread_id": thread_id}),
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message", "text": text}}),
    ])


def _resume_jsonl_no_thread_event(text="resumed"):
    return json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": text},
    })


class RunRoleAsyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project_patcher = patch.object(
            codex_council, "_project_root", return_value=FIXED_PROJECT_ROOT
        )
        self.project_patcher.start()
        self.addCleanup(self.project_patcher.stop)
        self.state_patcher = patch.object(codex_council, "STATE_DIR", self.tmp.name)
        self.state_patcher.start()
        self.addCleanup(self.state_patcher.stop)
        # Stub out the env so nothing leaks in from the developer's shell.
        self.env_patcher = patch.dict(os.environ, _env_without_session_key(), clear=True)
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)

    async def test_fresh_success_saves_session(self):
        role = _make_role("architect", "Architect")
        async def fake_subproc(cmd, prompt):
            return 0, _fresh_jsonl("new-sid", "All good."), ""
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "All good.")
        self.assertEqual(result.thread_id, "new-sid")
        sid, _ = codex_council.load_session("architect")
        self.assertEqual(sid, "new-sid")

    async def test_codex_fails_returns_classified_error(self):
        role = _make_role("architect", "Architect")
        async def fake_subproc(cmd, prompt):
            return 1, "", "401 unauthorized: incorrect api key sk-..."
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("[auth]"))

    async def test_rate_limit_tagged_for_retry(self):
        role = _make_role("architect", "Architect")
        async def fake_subproc(cmd, prompt):
            return 1, "", "HTTP 429 too many requests"
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("[retriable:rate-limit]"))

    async def test_5xx_tagged_for_retry(self):
        role = _make_role("architect", "Architect")
        async def fake_subproc(cmd, prompt):
            return 1, "", "502 bad gateway"
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("[retriable:5xx]"))

    async def test_stdout_error_jsonl_classifies_auth(self):
        role = _make_role("architect", "Architect")
        stdout = json.dumps({"type": "error", "message": "401 unauthorized"})
        async def fake_subproc(cmd, prompt):
            return 1, stdout, ""
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("[auth]"))

    async def test_stdout_turn_failed_jsonl_classifies_rate_limit(self):
        role = _make_role("architect", "Architect")
        stdout = json.dumps({
            "type": "turn.failed",
            "error": {"message": "HTTP 429 Too Many Requests"},
        })
        async def fake_subproc(cmd, prompt):
            return 1, stdout, ""
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("[retriable:rate-limit]"))

    async def test_stdout_nested_json_error_is_reported(self):
        role = _make_role("architect", "Architect")
        inner = json.dumps({
            "type": "error",
            "status": 400,
            "error": {"message": "The model is unsupported."},
        })
        stdout = json.dumps({"type": "turn.failed", "error": {"message": inner}})
        async def fake_subproc(cmd, prompt):
            return 1, stdout, ""
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertFalse(result.ok)
        self.assertIn("The model is unsupported.", result.error)

    async def test_no_agent_message_returns_failure_without_saving(self):
        role = _make_role("architect", "Architect")
        async def fake_subproc(cmd, prompt):
            return 0, json.dumps({"type": "thread.started", "thread_id": "x"}), ""
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertFalse(result.ok)
        self.assertIn("no agent_message", result.error)
        sid, _ = codex_council.load_session("architect")
        self.assertIsNone(sid)

    async def test_stale_resume_restarts_fresh(self):
        role = _make_role("architect", "Architect")
        codex_council.save_session("architect", "stale-sid")
        calls = []
        async def fake_subproc(cmd, prompt):
            calls.append(cmd)
            if "resume" in cmd:
                return 1, "", "Error: thread/resume failed: no rollout found for thread id stale-sid (code -32600)"
            return 0, _fresh_jsonl("brand-new-sid", "fresh ok"), ""
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertTrue(result.ok)
        self.assertEqual(result.thread_id, "brand-new-sid")
        self.assertEqual(len(calls), 2, "expected resume → fresh fallthrough")
        sid, _ = codex_council.load_session("architect")
        self.assertEqual(sid, "brand-new-sid")

    async def test_stale_resume_with_429_in_thread_id_still_restarts_fresh(self):
        role = _make_role("architect", "Architect")
        codex_council.save_session("architect", "stale-429-sid")
        calls = []
        async def fake_subproc(cmd, prompt):
            calls.append(cmd)
            if "resume" in cmd:
                return (
                    1, "",
                    "Error: no rollout found for thread id stale-429-sid",
                )
            return 0, _fresh_jsonl("brand-new-sid", "fresh ok"), ""
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertTrue(result.ok)
        self.assertEqual(len(calls), 2)
        sid, _ = codex_council.load_session("architect")
        self.assertEqual(sid, "brand-new-sid")

    async def test_stale_resume_detected_from_stdout_jsonl(self):
        role = _make_role("architect", "Architect")
        codex_council.save_session("architect", "stale-sid")
        calls = []
        stdout = json.dumps({
            "type": "turn.failed",
            "error": {"message": "no rollout found for thread id stale-sid"},
        })
        async def fake_subproc(cmd, prompt):
            calls.append(cmd)
            if "resume" in cmd:
                return 1, stdout, ""
            return 0, _fresh_jsonl("brand-new-sid", "fresh ok"), ""
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertTrue(result.ok)
        self.assertEqual(len(calls), 2)

    async def test_resume_thread_id_mismatch_adopts_new_id_without_rerun(self):
        """Codex resume-with-bogus-id silently spawns a new thread.
        Red-council verdict: adopt the new id, don't burn tokens re-running.
        The mismatch also surfaces a warning so the report shows degraded
        continuity (the role lost its accumulated framing)."""
        role = _make_role("architect", "Architect")
        codex_council.save_session("architect", "expected-sid")
        calls = []
        async def fake_subproc(cmd, prompt):
            calls.append(cmd)
            return 0, _fresh_jsonl("DIFFERENT-sid", "happened anyway"), ""
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertTrue(result.ok)
        self.assertEqual(result.thread_id, "DIFFERENT-sid")
        self.assertEqual(len(calls), 1, "must NOT re-run; just adopt the new id")
        self.assertIsNotNone(result.warning)
        self.assertIn("DIFFERENT-sid", result.warning)
        self.assertIn("expected-sid", result.warning)
        sid, _ = codex_council.load_session("architect")
        self.assertEqual(sid, "DIFFERENT-sid")

    async def test_resume_with_no_thread_started_event_keeps_stored_id(self):
        """Codex may omit thread.started on resume; treat as a normal resume."""
        role = _make_role("architect", "Architect")
        codex_council.save_session("architect", "kept-sid")
        async def fake_subproc(cmd, prompt):
            return 0, _resume_jsonl_no_thread_event("resumed text"), ""
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertTrue(result.ok)
        self.assertEqual(result.thread_id, "kept-sid")
        self.assertEqual(result.text, "resumed text")

    async def test_fresh_path_msg_without_thread_started_is_still_ok(self):
        """Regression: a fresh codex call that emits agent_message but no
        thread.started must not drop the reply. Continuity is lost (we
        can't resume), but the user still gets the answer for this turn."""
        role = _make_role("architect", "Architect")
        # No saved session, so this goes the fresh path; stdout has no thread.started.
        async def fake_subproc(cmd, prompt):
            return 0, _resume_jsonl_no_thread_event("answer without id"), ""
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "answer without id")
        self.assertIsNone(result.thread_id)
        # And no garbage state was written for a thread we never identified.
        sid, _ = codex_council.load_session("architect")
        self.assertIsNone(sid)


class TerminateProcessGroupTests(unittest.IsolatedAsyncioTestCase):
    async def test_sigkill_sent_even_if_grace_sleep_is_cancelled(self):
        class Proc:
            returncode = 0

        async def cancelled_sleep(_):
            raise asyncio.CancelledError()

        with patch.object(codex_council.os, "killpg") as killpg:
            with patch.object(codex_council.asyncio, "sleep", side_effect=cancelled_sleep):
                with self.assertRaises(asyncio.CancelledError):
                    await codex_council._terminate_process_group(Proc(), pgid=12345)
        killpg.assert_any_call(12345, codex_council.signal.SIGTERM)
        killpg.assert_any_call(12345, codex_council.signal.SIGKILL)


class FormatReportWarningTests(unittest.TestCase):
    def test_warning_field_renders_in_role_section(self):
        role = _make_role("architect", "Architect")
        result = codex_council.RoleResult(
            role=role, ok=True, text="body", warning="thread continuity lost",
            elapsed_seconds=0.1,
        )
        out = codex_council._format_report([result], 0.1)
        self.assertIn("_Warning: thread continuity lost_", out)
        self.assertIn("WARNING", out)  # in summary line too

    def test_report_metadata_newlines_are_escaped(self):
        role = _make_role("architect", "Good\n## Forged")
        result = codex_council.RoleResult(
            role=role, ok=False, error="boom\n## Injected", elapsed_seconds=0.1,
        )
        out = codex_council._format_report([result], 0.1)
        self.assertNotIn("\n## Forged", out)
        self.assertNotIn("\n## Injected", out)
        self.assertIn("Good\\n## Forged", out)
        self.assertIn("boom\\n## Injected", out)


class RunRoleWithRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project_patcher = patch.object(
            codex_council, "_project_root", return_value=FIXED_PROJECT_ROOT
        )
        self.project_patcher.start()
        self.addCleanup(self.project_patcher.stop)
        self.state_patcher = patch.object(codex_council, "STATE_DIR", self.tmp.name)
        self.state_patcher.start()
        self.addCleanup(self.state_patcher.stop)
        self.env_patcher = patch.dict(os.environ, _env_without_session_key(), clear=True)
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)
        # Skip backoff sleep during tests.
        self.sleep_patcher = patch.object(codex_council.asyncio, "sleep", AsyncMock(return_value=None))
        self.sleep_patcher.start()
        self.addCleanup(self.sleep_patcher.stop)

    async def test_retriable_then_success(self):
        role = _make_role("architect", "Architect")
        attempts = [0]
        async def fake_once(r, prompt, attempt):
            attempts[0] += 1
            if attempt == 1:
                return codex_council.RoleResult(
                    role=r, ok=False, error="[retriable:5xx] 503 unavailable",
                    elapsed_seconds=0.1, attempts=attempt,
                )
            return codex_council.RoleResult(
                role=r, ok=True, text="finally", thread_id="sid",
                elapsed_seconds=0.2, attempts=attempt,
            )
        with patch.object(codex_council, "_run_role_once", side_effect=fake_once):
            result = await codex_council._run_role(role, "prompt")
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(attempts[0], 2)

    async def test_retriable_exhausted_returns_last_failure(self):
        role = _make_role("architect", "Architect")
        async def fake_once(r, prompt, attempt):
            return codex_council.RoleResult(
                role=r, ok=False, error="[retriable:rate-limit] 429",
                elapsed_seconds=0.1, attempts=attempt,
            )
        with patch.object(codex_council, "_run_role_once", side_effect=fake_once):
            result = await codex_council._run_role(role, "prompt")
        self.assertFalse(result.ok)
        self.assertEqual(result.attempts, codex_council.MAX_RETRY_ATTEMPTS)

    async def test_non_retriable_does_not_retry(self):
        role = _make_role("architect", "Architect")
        call_count = [0]
        async def fake_once(r, prompt, attempt):
            call_count[0] += 1
            return codex_council.RoleResult(
                role=r, ok=False, error="[auth] 401 unauthorized",
                elapsed_seconds=0.1, attempts=attempt,
            )
        with patch.object(codex_council, "_run_role_once", side_effect=fake_once):
            result = await codex_council._run_role(role, "prompt")
        self.assertFalse(result.ok)
        self.assertEqual(call_count[0], 1)


class RunTeamAsyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project_patcher = patch.object(
            codex_council, "_project_root", return_value=FIXED_PROJECT_ROOT
        )
        self.project_patcher.start()
        self.addCleanup(self.project_patcher.stop)
        self.state_patcher = patch.object(codex_council, "STATE_DIR", self.tmp.name)
        self.state_patcher.start()
        self.addCleanup(self.state_patcher.stop)
        self.env_patcher = patch.dict(os.environ, _env_without_session_key(), clear=True)
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)

    _LABELS = {
        "architect": "Architect",
        "security": "Security",
        "tester": "Test engineer",
    }

    def _roles(self, *ids):
        return [_make_role(i, self._LABELS.get(i, i)) for i in ids]

    async def test_parallel_fanout_preserves_order(self):
        async def fake_role(role, prompt):
            return codex_council.RoleResult(
                role=role, ok=True, text=f"reply-{role.id}",
                elapsed_seconds=0.1, attempts=1, thread_id=f"sid-{role.id}",
            )
        with patch.object(codex_council, "_run_role", side_effect=fake_role):
            results = await codex_council.run_council(
                self._roles("tester", "architect"), "body",
            )
        self.assertEqual([r.role.id for r in results], ["tester", "architect"])
        for r in results:
            self.assertTrue(r.ok)

    async def test_one_crash_does_not_lose_siblings(self):
        async def fake_role(role, prompt):
            if role.id == "security":
                raise RuntimeError("simulated crash")
            return codex_council.RoleResult(role=role, ok=True, text="ok", elapsed_seconds=0.1)
        with patch.object(codex_council, "_run_role", side_effect=fake_role):
            results = await codex_council.run_council(
                self._roles("architect", "security", "tester"), "body",
            )
        self.assertEqual(len(results), 3)
        crashed = [r for r in results if r.role.id == "security"][0]
        self.assertFalse(crashed.ok)
        self.assertIn("orchestrator-exception", crashed.error)
        self.assertIn("RuntimeError", crashed.error)
        siblings = [r for r in results if r.role.id != "security"]
        self.assertTrue(all(r.ok for r in siblings))

    async def test_custom_roles_run_through_fanout(self):
        """Ad-hoc Role objects flow through run_council (the only flow now)."""
        custom = codex_council.Role(
            "ml-fairness", "ML Fairness", "audit bias thoroughly."
        )

        async def fake_role(role, prompt):
            return codex_council.RoleResult(
                role=role, ok=True, text=f"reply-{role.id}",
                elapsed_seconds=0.1, attempts=1,
            )
        with patch.object(codex_council, "_run_role", side_effect=fake_role):
            results = await codex_council.run_council([custom], "body")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].role.id, "ml-fairness")
        self.assertTrue(results[0].ok)

    async def test_same_role_runs_are_serialized_by_state_lock(self):
        role = _make_role("architect", "Architect")
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        calls = []
        active = {"count": 0, "max": 0}

        async def fake_subproc(cmd, prompt):
            calls.append(cmd)
            active["count"] += 1
            active["max"] = max(active["max"], active["count"])
            try:
                if len(calls) == 1:
                    first_started.set()
                    await release_first.wait()
                if "resume" in cmd:
                    return 0, _resume_jsonl_no_thread_event("resumed"), ""
                return 0, _fresh_jsonl("sid-first", "fresh"), ""
            finally:
                active["count"] -= 1

        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            t1 = asyncio.create_task(codex_council._run_role(role, "prompt-1"))
            await first_started.wait()
            t2 = asyncio.create_task(codex_council._run_role(role, "prompt-2"))
            await asyncio.sleep(0.05)
            self.assertEqual(active["max"], 1)
            release_first.set()
            results = await asyncio.gather(t1, t2)

        self.assertTrue(all(r.ok for r in results))
        self.assertEqual(sum(1 for cmd in calls if "resume" in cmd), 1)
        self.assertEqual(sum(1 for cmd in calls if "resume" not in cmd), 1)

    async def test_different_roles_still_run_concurrently(self):
        roles = self._roles("architect", "security")
        both_started = asyncio.Event()
        release = asyncio.Event()
        active = {"count": 0, "max": 0}

        async def fake_subproc(cmd, prompt):
            active["count"] += 1
            active["max"] = max(active["max"], active["count"])
            if active["count"] == 2:
                both_started.set()
            try:
                await release.wait()
                return 0, _fresh_jsonl(f"sid-{len(prompt)}", "ok"), ""
            finally:
                active["count"] -= 1

        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            tasks = [asyncio.create_task(codex_council._run_role(r, "prompt")) for r in roles]
            await both_started.wait()
            self.assertEqual(active["max"], 2)
            release.set()
            results = await asyncio.gather(*tasks)

        self.assertTrue(all(r.ok for r in results))


class RunCouncilProgressTests(unittest.IsolatedAsyncioTestCase):
    """run_council emits a per-role completion line to stderr as each role
    settles (in completion order), while stdout stays the report. The final
    CODEX_COUNCIL_DONE line is NOT emitted here — main() owns it (covered by
    the E2E happy-path test)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project_patcher = patch.object(
            codex_council, "_project_root", return_value=FIXED_PROJECT_ROOT
        )
        self.project_patcher.start()
        self.addCleanup(self.project_patcher.stop)
        self.state_patcher = patch.object(codex_council, "STATE_DIR", self.tmp.name)
        self.state_patcher.start()
        self.addCleanup(self.state_patcher.stop)
        self.env_patcher = patch.dict(os.environ, _env_without_session_key(), clear=True)
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)

    _LABELS = {
        "architect": "Architect",
        "security": "Security",
        "tester": "Test engineer",
    }

    def _roles(self, *ids):
        return [_make_role(i, self._LABELS.get(i, i)) for i in ids]

    async def test_per_role_stderr_progress_and_order(self):
        async def fake_role(role, prompt):
            ok = role.id != "security"  # one not-ok to exercise FAILED line
            return codex_council.RoleResult(
                role=role, ok=ok,
                text="reply" if ok else None,
                error=None if ok else "boom",
                elapsed_seconds=0.1, attempts=1,
            )
        buf = io.StringIO()
        with patch.object(codex_council, "_run_role", side_effect=fake_role):
            with contextlib.redirect_stderr(buf):
                results = await codex_council.run_council(
                    self._roles("architect", "security", "tester"), "body",
                )

        # Returned list preserves ROLE order (not completion order).
        self.assertEqual(
            [r.role.id for r in results], ["architect", "security", "tester"]
        )

        err = buf.getvalue()
        # A per-role line for each role, ok or FAILED, with an elapsed paren.
        self.assertRegex(err, r"\[codex-council\] \d+/3 architect: ok \(")
        self.assertRegex(err, r"\[codex-council\] \d+/3 security: FAILED \(")
        self.assertRegex(err, r"\[codex-council\] \d+/3 tester: ok \(")
        # Exactly one progress line per role (3 total).
        progress_lines = [
            ln for ln in err.splitlines()
            if re.match(r"\[codex-council\] \d+/3 \S+: (ok|FAILED) \(", ln)
        ]
        self.assertEqual(len(progress_lines), 3)
        # Counters are exactly 1..N, each once — catches an "always 1/3" bug.
        counters = sorted(
            int(re.match(r"\[codex-council\] (\d+)/3", ln).group(1))
            for ln in progress_lines
        )
        self.assertEqual(counters, [1, 2, 3])
        # The final sentinel is main()'s job, never run_council's.
        self.assertNotIn("CODEX_COUNCIL_DONE", err)


# ---------- roles JSON parsing (--roles-file contents) ----------

class ParseRolesJsonTests(unittest.TestCase):
    def test_single_custom_role_happy_path(self):
        instruction = _valid_instruction("Audit for bias")
        raw = json.dumps([_role_json("ml-fairness", "ML Fairness", instruction)])
        roles = codex_council._parse_roles_json(raw)
        self.assertEqual(len(roles), 1)
        self.assertEqual(roles[0].id, "ml-fairness")
        self.assertEqual(roles[0].label, "ML Fairness")
        self.assertEqual(roles[0].instruction, instruction)

    def test_multiple_custom_roles_preserve_order(self):
        raw = json.dumps([
            _role_json("alpha", "A", _valid_instruction("do a")),
            _role_json("beta", "B", _valid_instruction("do b")),
            _role_json("gamma", "G", _valid_instruction("do g")),
        ])
        roles = codex_council._parse_roles_json(raw)
        self.assertEqual([r.id for r in roles], ["alpha", "beta", "gamma"])

    def test_longer_id_allowed_up_to_32(self):
        raw = json.dumps([_role_json("a" * 32, "L")])
        roles = codex_council._parse_roles_json(raw)
        self.assertEqual(roles[0].id, "a" * 32)

    def test_invalid_json_raises(self):
        _assert_usage_exit(
            self, lambda: codex_council._parse_roles_json("{not json"),
            expect_in_stderr="invalid JSON",
        )

    def test_non_list_top_level_raises(self):
        _assert_usage_exit(
            self,
            lambda: codex_council._parse_roles_json(json.dumps({"id": "x"})),
            expect_in_stderr="must be a JSON list",
        )

    def test_non_object_entry_raises(self):
        _assert_usage_exit(
            self,
            lambda: codex_council._parse_roles_json(json.dumps(["not-an-object"])),
            expect_in_stderr="must be an object",
        )

    def test_missing_id_field_raises(self):
        raw = json.dumps([{"label": "L", "instruction": _valid_instruction("x")}])
        _assert_usage_exit(
            self, lambda: codex_council._parse_roles_json(raw),
            expect_in_stderr="missing field 'id'",
        )

    def test_missing_label_field_raises(self):
        raw = json.dumps([{"id": "x", "instruction": _valid_instruction("x")}])
        _assert_usage_exit(
            self, lambda: codex_council._parse_roles_json(raw),
            expect_in_stderr="missing field 'label'",
        )

    def test_missing_instruction_field_raises(self):
        raw = json.dumps([{"id": "x", "label": "L"}])
        _assert_usage_exit(
            self, lambda: codex_council._parse_roles_json(raw),
            expect_in_stderr="missing field 'instruction'",
        )

    def test_empty_string_field_raises(self):
        raw = json.dumps([{"id": "x", "label": "", "instruction": _valid_instruction("x")}])
        _assert_usage_exit(
            self, lambda: codex_council._parse_roles_json(raw),
            expect_in_stderr="non-empty string",
        )

    def test_whitespace_only_field_raises(self):
        raw = json.dumps([{"id": "x", "label": "L", "instruction": "   "}])
        _assert_usage_exit(
            self, lambda: codex_council._parse_roles_json(raw),
            expect_in_stderr="non-empty string",
        )

    def test_label_newline_raises(self):
        raw = json.dumps([_role_json("x", "Good\nForged")])
        _assert_usage_exit(
            self, lambda: codex_council._parse_roles_json(raw),
            expect_in_stderr="label must not contain newlines",
        )

    def test_instruction_newline_raises(self):
        raw = json.dumps([_role_json("x", "L", "one\ntwo")])
        _assert_usage_exit(
            self, lambda: codex_council._parse_roles_json(raw),
            expect_in_stderr="single paragraph",
        )

    def test_instruction_unicode_line_separator_raises(self):
        raw = json.dumps([_role_json("x", "L", "one\u2028two")])
        _assert_usage_exit(
            self, lambda: codex_council._parse_roles_json(raw),
            expect_in_stderr="single paragraph",
        )

    def test_label_at_byte_cap_allowed(self):
        raw = json.dumps([_role_json("x", "a" * codex_council.ROLE_LABEL_MAX_BYTES)])
        roles = codex_council._parse_roles_json(raw)
        self.assertEqual(roles[0].label, "a" * codex_council.ROLE_LABEL_MAX_BYTES)

    def test_label_over_byte_cap_raises(self):
        raw = json.dumps([_role_json("x", "a" * (codex_council.ROLE_LABEL_MAX_BYTES + 1))])
        _assert_usage_exit(
            self, lambda: codex_council._parse_roles_json(raw),
            expect_in_stderr="label exceeds",
        )

    def test_instruction_over_byte_cap_raises(self):
        oversized = "a" * (codex_council.ROLE_INSTRUCTION_MAX_BYTES + 1)
        raw = json.dumps([_role_json("x", "L", oversized)])
        _assert_usage_exit(
            self, lambda: codex_council._parse_roles_json(raw),
            expect_in_stderr="instruction exceeds",
        )

    def test_instruction_size_counts_utf8_bytes(self):
        with patch.object(codex_council, "ROLE_INSTRUCTION_MAX_BYTES", 4):
            raw = json.dumps([_role_json("x", "L", "€€")])
            _assert_usage_exit(
                self, lambda: codex_council._parse_roles_json(raw),
                expect_in_stderr="instruction exceeds",
            )

    def test_instruction_requires_scope_phrase(self):
        raw = json.dumps([_role_json("x", "L", "Review only. Thoroughness beats speed.")])
        _assert_usage_exit(
            self, lambda: codex_council._parse_roles_json(raw),
            expect_in_stderr="nothing material",
        )

    def test_instruction_requires_cadence_sentence(self):
        raw = json.dumps([_role_json("x", "L", "Review; if nothing material, say so clearly.")])
        _assert_usage_exit(
            self, lambda: codex_council._parse_roles_json(raw),
            expect_in_stderr="Thoroughness beats speed.",
        )

    def test_bad_id_regex_uppercase_raises(self):
        raw = json.dumps([_role_json("BadID", "L")])
        _assert_usage_exit(
            self, lambda: codex_council._parse_roles_json(raw),
            expect_in_stderr="must match",
        )

    def test_bad_id_regex_with_dot_raises(self):
        raw = json.dumps([_role_json("ml.fairness", "L")])
        _assert_usage_exit(
            self, lambda: codex_council._parse_roles_json(raw),
            expect_in_stderr="must match",
        )

    def test_id_over_32_chars_raises(self):
        raw = json.dumps([_role_json("a" * 33, "L")])
        _assert_usage_exit(
            self, lambda: codex_council._parse_roles_json(raw),
            expect_in_stderr="exceeds 32",
        )

    def test_duplicate_id_in_payload_raises(self):
        raw = json.dumps([
            _role_json("alpha", "A", _valid_instruction("do a")),
            _role_json("alpha", "A2", _valid_instruction("again")),
        ])
        _assert_usage_exit(
            self, lambda: codex_council._parse_roles_json(raw),
            expect_in_stderr="duplicate id",
        )


class ResolveRolesJsonIntegrationTests(unittest.TestCase):
    """End-to-end of JSON parsing through resolution (custom roles only)."""

    def test_json_invocation_resolves(self):
        raw = json.dumps([
            {"id": "data-pipeline", "label": "Data",
             "instruction": _valid_instruction("review pipeline")},
            {"id": "ml-fairness", "label": "Fair",
             "instruction": _valid_instruction("audit bias")},
        ])
        custom = codex_council._parse_roles_json(raw)
        roles = codex_council._resolve_roles(custom)
        self.assertEqual([r.id for r in roles], ["data-pipeline", "ml-fairness"])

    def test_max_parallel_enforced_via_json(self):
        """MAX_PARALLEL + 1 custom roles must reject."""
        entries = [
            {"id": f"role-{i}", "label": f"R{i}",
             "instruction": _valid_instruction("x")}
            for i in range(codex_council.MAX_PARALLEL + 1)
        ]
        custom = codex_council._parse_roles_json(json.dumps(entries))
        _assert_usage_exit(
            self, lambda: codex_council._resolve_roles(custom),
            expect_in_stderr="MAX_PARALLEL",
        )


class ProjectRootCacheTests(unittest.TestCase):
    def setUp(self):
        codex_council._project_root.cache_clear()

    def tearDown(self):
        codex_council._project_root.cache_clear()

    def test_only_one_git_call_across_many_lookups(self):
        calls = {"count": 0}
        def fake_run(*args, **kwargs):
            calls["count"] += 1
            from subprocess import CompletedProcess
            return CompletedProcess(args=args[0], returncode=0, stdout="/x\n", stderr="")
        with patch.object(codex_council.subprocess, "run", side_effect=fake_run):
            for _ in range(5):
                codex_council._project_root()
        self.assertEqual(calls["count"], 1)


# ---------- --roles-file (the sole role-input channel) ----------

class ReadRolesFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_reads_file_contents_verbatim(self):
        path = os.path.join(self.tmp.name, "roles.json")
        payload = json.dumps([_role_json("a", "A")])
        with open(path, "w", encoding="utf-8") as f:
            f.write(payload)
        self.assertEqual(codex_council._read_roles_file(path), payload)

    def test_missing_file_usage_exits(self):
        missing = os.path.join(self.tmp.name, "nope.json")
        _assert_usage_exit(
            self, lambda: codex_council._read_roles_file(missing),
            expect_in_stderr="cannot read",
        )

    def test_missing_parent_mentions_staging_hint(self):
        missing = os.path.join(self.tmp.name, "missing", "roles.json")
        _assert_usage_exit(
            self, lambda: codex_council._read_roles_file(missing),
            expect_in_stderr="Staging hint",
        )

    def test_empty_path_usage_exits(self):
        _assert_usage_exit(
            self, lambda: codex_council._read_roles_file(""),
            expect_in_stderr="non-empty",
        )

    def test_invalid_utf8_file_usage_exits(self):
        path = os.path.join(self.tmp.name, "bad.json")
        with open(path, "wb") as f:
            f.write(b"\xff\xfe not utf-8")
        _assert_usage_exit(
            self, lambda: codex_council._read_roles_file(path),
            expect_in_stderr="not valid UTF-8",
        )

    def test_non_ascii_role_file_roundtrips(self):
        path = os.path.join(self.tmp.name, "roles.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump([{"id": "a", "label": "Café",
                        "instruction": _valid_instruction("réview €")}], f)
        roles = codex_council._parse_roles_json(codex_council._read_roles_file(path))
        self.assertEqual(roles[0].label, "Café")

    def test_empty_file_parses_to_invalid_json(self):
        """An explicitly-supplied empty file should surface a clear JSON
        error (main() parses unconditionally), not 'no roles requested'."""
        path = os.path.join(self.tmp.name, "empty.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("")
        _assert_usage_exit(
            self,
            lambda: codex_council._parse_roles_json(codex_council._read_roles_file(path)),
            expect_in_stderr="invalid JSON",
        )

    def test_file_roundtrips_through_parse(self):
        """The whole point: a file the shell never had to quote parses cleanly."""
        path = os.path.join(self.tmp.name, "roles.json")
        with open(path, "w") as f:
            json.dump([
                {"id": "alpha", "label": "A",
                 "instruction": _valid_instruction("do a")},
                {"id": "beta", "label": "B",
                 "instruction": _valid_instruction("do b")},
            ], f)
        roles = codex_council._parse_roles_json(codex_council._read_roles_file(path))
        self.assertEqual([r.id for r in roles], ["alpha", "beta"])


class CheckStagingDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.chmod(self.tmp.name, 0o700)

    def _write_valid_roles(self):
        with open(os.path.join(self.tmp.name, "roles.json"), "w", encoding="utf-8") as f:
            json.dump([_role_json("a", "A")], f)

    def _write_context(self, text="context"):
        with open(os.path.join(self.tmp.name, "context.md"), "w", encoding="utf-8") as f:
            f.write(text)

    def test_valid_staging_dir_prints_ok(self):
        self._write_valid_roles()
        self._write_context()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            codex_council._check_staging_dir(self.tmp.name)
        self.assertIn("staging OK", buf.getvalue())
        self.assertIn("(1 roles)", buf.getvalue())

    def test_missing_context_mentions_staging_hint(self):
        self._write_valid_roles()
        _assert_usage_exit(
            self,
            lambda: codex_council._check_staging_dir(self.tmp.name),
            expect_in_stderr="Staging hint",
        )

    def test_empty_context_is_rejected(self):
        self._write_valid_roles()
        self._write_context("   \n")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit) as ctx:
                codex_council._check_staging_dir(self.tmp.name)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("Empty Context file", buf.getvalue())

    def test_public_staging_dir_is_rejected(self):
        os.chmod(self.tmp.name, 0o755)
        self.addCleanup(lambda: os.chmod(self.tmp.name, 0o700))
        _assert_usage_exit(
            self,
            lambda: codex_council._check_staging_dir(self.tmp.name),
            expect_in_stderr="not private 0700",
        )


class ReadContextFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _path(self, name="context.md"):
        return os.path.join(self.tmp.name, name)

    def test_reads_context_file(self):
        path = self._path()
        with open(path, "w", encoding="utf-8") as f:
            f.write("context\n")
        self.assertEqual(codex_council._read_context_file(path), "context\n")

    def test_missing_context_file_usage_exits(self):
        path = self._path("missing.md")
        _assert_usage_exit(
            self,
            lambda: codex_council._read_context_file(path),
            expect_in_stderr="Staging hint",
        )

    def test_empty_context_file_exits_1(self):
        path = self._path()
        with open(path, "w", encoding="utf-8") as f:
            f.write("   \n")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit) as ctx:
                codex_council._read_context_file(path)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("Empty Context file", buf.getvalue())

    def test_context_file_reuses_utf8_and_byte_cap_guards(self):
        path = self._path()
        with open(path, "wb") as f:
            f.write(b"\xff\xfe bad")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit) as ctx:
                codex_council._read_context_file(path)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("not valid UTF-8", buf.getvalue())

        with patch.object(codex_council, "MAX_STDIN_BYTES", 4):
            with open(path, "wb") as f:
                f.write(b"abcde")
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                with self.assertRaises(SystemExit) as ctx:
                    codex_council._read_context_file(path)
            self.assertEqual(ctx.exception.code, 1)
            self.assertIn("exceeds", buf.getvalue())


class ReadStdinBodyTests(unittest.TestCase):
    """The cap is a BYTE cap (the prompt is UTF-8 encoded for codex), so the
    reader counts bytes, not characters."""

    def test_max_stdin_bytes_is_exactly_10_mib(self):
        self.assertEqual(codex_council.MAX_STDIN_BYTES, 10 << 20)
        self.assertEqual(codex_council.MAX_STDIN_BYTES, 10 * 1024 * 1024)

    def test_returns_decoded_body_under_cap(self):
        self.assertEqual(codex_council._read_stdin_body(io.BytesIO(b"hello")), "hello")

    def test_rejects_over_byte_cap(self):
        oversize = b"a" * (codex_council.MAX_STDIN_BYTES + 1)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit) as ctx:
                codex_council._read_stdin_body(io.BytesIO(oversize))
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("exceeds", buf.getvalue())

    def test_counts_bytes_not_characters(self):
        """Regression: 3 multibyte chars (9 bytes) must be rejected at an
        8-byte cap. A character-count check would have let it through."""
        with patch.object(codex_council, "MAX_STDIN_BYTES", 8):
            payload = "€€€".encode("utf-8")  # 3 chars, 9 bytes
            self.assertEqual(len(payload), 9)
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    codex_council._read_stdin_body(io.BytesIO(payload))
            self.assertEqual(ctx.exception.code, 1)

    def test_multibyte_under_cap_ok(self):
        with patch.object(codex_council, "MAX_STDIN_BYTES", 16):
            self.assertEqual(
                codex_council._read_stdin_body(io.BytesIO("€€".encode("utf-8"))),
                "€€",
            )

    def test_accepts_exactly_cap_bytes(self):
        with patch.object(codex_council, "MAX_STDIN_BYTES", 8):
            self.assertEqual(
                codex_council._read_stdin_body(io.BytesIO(b"abcdefgh")),  # exactly 8
                "abcdefgh",
            )

    def test_rejects_invalid_utf8(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit) as ctx:
                codex_council._read_stdin_body(io.BytesIO(b"\xff\xfe bad bytes"))
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("not valid UTF-8", buf.getvalue())

    def test_empty_rejected(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit) as ctx:
                codex_council._read_stdin_body(io.BytesIO(b"   \n  "))
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("Empty input", buf.getvalue())


class PromptSizeTests(unittest.TestCase):
    def test_composed_prompt_at_cap_allowed(self):
        role = codex_council.Role("architect", "Architect", "i")
        with patch.object(codex_council, "MAX_PROMPT_BYTES", len("i\n\nb\n\ni")):
            codex_council._validate_prompt_size(role, "b")

    def test_composed_prompt_over_cap_exits_1(self):
        role = codex_council.Role("architect", "Architect", "i")
        buf = io.StringIO()
        with patch.object(codex_council, "MAX_PROMPT_BYTES", len("i\n\nb\n\ni") - 1):
            with contextlib.redirect_stderr(buf):
                with self.assertRaises(SystemExit) as ctx:
                    codex_council._validate_prompt_size(role, "b")
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("architect", buf.getvalue())


class ArgParseTests(unittest.TestCase):
    def test_roles_file_parses_to_namespace(self):
        args = codex_council._parse_args(["--roles-file", "x.json"])
        self.assertEqual(args.roles_file, "x.json")

    def test_context_file_parses_to_namespace(self):
        args = codex_council._parse_args([
            "--roles-file", "x.json",
            "--context-file", "context.md",
        ])
        self.assertEqual(args.context_file, "context.md")

    def test_empty_roles_file_rejected(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit) as ctx:
                codex_council._parse_args(["--roles-file", ""])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("must be non-empty", buf.getvalue())

    def test_empty_context_file_rejected(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit) as ctx:
                codex_council._parse_args(["--context-file", ""])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("must be non-empty", buf.getvalue())

    def test_bare_invocation_leaves_roles_file_none(self):
        args = codex_council._parse_args([])
        self.assertIsNone(args.roles_file)

    def test_roles_json_flag_is_removed(self):
        """--roles-json no longer exists; argparse rejects it (exit 2)."""
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit) as ctx:
                codex_council._parse_args(["--roles-json", "[]"])
        self.assertEqual(ctx.exception.code, 2)


class NoTimeoutTests(unittest.TestCase):
    """No timeout, by design: the codex commands carry no timeout/retry
    config overrides (those live in the user's provider-scoped codex
    config), and the script enforces no wall-clock deadline."""

    def test_commands_have_no_config_overrides(self):
        for cmd in (codex_council._fresh_cmd("/r"),
                    codex_council._resume_cmd("/r", "sid")):
            self.assertNotIn("-c", cmd)
            self.assertFalse(any("timeout" in a or "retries" in a for a in cmd))

    def test_source_uses_no_run_level_timeout_primitive(self):
        """No run-level/wall-clock timeout, by design: pin the absence of any
        named timeout primitive so adding one is a conscious choice (this test
        fails) rather than a silent regression. Scans executable code only —
        comment and string/docstring spans are masked out, since this file is
        deliberately comment-heavy about NOT having a timeout. This catches the
        named-API timeouts below; it cannot catch a hand-rolled deadline (an
        asyncio.sleep watchdog or a time.monotonic cancel-loop), which has no
        fixed spelling to pin — that boundary is held by code review plus
        test_commands_have_no_config_overrides above."""
        import io as _io
        import re as _re
        import tokenize as _tokenize
        with open(codex_council.__file__, encoding="utf-8") as f:
            src = f.read()
        # Mask COMMENT and STRING token spans (preserving byte offsets) so the
        # scan sees executable code only, not prose that names these APIs.
        masked = list(src)
        offsets = [0]
        for line in src.splitlines(keepends=True):
            offsets.append(offsets[-1] + len(line))
        for tok in _tokenize.generate_tokens(_io.StringIO(src).readline):
            if tok.type in (_tokenize.COMMENT, _tokenize.STRING):
                start = offsets[tok.start[0] - 1] + tok.start[1]
                end = offsets[tok.end[0] - 1] + tok.end[1]
                for i in range(start, end):
                    if masked[i] != "\n":
                        masked[i] = " "
        code = "".join(masked)
        # (label, regex). Identifier boundaries avoid matching benign names
        # like `idle_timeout = N`; \s* tolerates spaced kwargs / calls.
        forbidden = (
            ("asyncio.wait_for", r"\basyncio\s*\.\s*wait_for\b"),
            ("asyncio.timeout", r"\basyncio\s*\.\s*timeout(?:_at)?\b"),
            ("signal.alarm", r"\bsignal\s*\.\s*alarm\b"),
            ("signal.setitimer", r"\bsignal\s*\.\s*setitimer\b"),
            (".settimeout(", r"\.\s*settimeout\s*\("),
            ("timeout=", r"\btimeout\s*="),
        )
        found = [name for name, pat in forbidden if _re.search(pat, code)]
        self.assertEqual(found, [], f"unexpected timeout primitive(s): {found}")


class DocsContractTests(unittest.TestCase):
    def _repo_file(self, *parts):
        return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", *parts))

    def test_skill_context_pipelines_are_fail_closed_and_filename_safe(self):
        path = self._repo_file(
            "plugins", "codex-council", "skills", "codex-council", "SKILL.md"
        )
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("set -euo pipefail", text)
        self.assertIn("git ls-files -z", text)
        self.assertIn("read -r -d ''", text)
        self.assertIn("file --brief --mime --", text)
        self.assertIn("git diff --cached", text)

    def test_readme_dev_hook_keeps_diagnostics(self):
        path = self._repo_file("README.md")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("codex-council-dev-link.log", text)
        self.assertNotIn(">/dev/null 2>&1 || true", text)


if __name__ == "__main__":
    unittest.main()
