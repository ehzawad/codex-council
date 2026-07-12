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
        codex_council.MAX_PARALLEL_ENV,
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
    """JSON-shaped role entry; instruction is array-only by contract,
    so a convenience string is wrapped into a single-item list."""
    if instruction is None:
        instruction = _valid_instruction("review")
    if isinstance(instruction, str):
        instruction = [instruction]
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

    def test_resolve_large_panel_without_a_role_count_cap(self):
        roles_in = [
            _make_role(f"r{i}", f"R{i}", _valid_instruction("x"))
            for i in range(100)
        ]
        roles = codex_council._resolve_roles(roles_in)
        self.assertEqual(len(roles), 100)


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


class MaxParallelTests(unittest.TestCase):
    def setUp(self):
        self.codex_home = tempfile.TemporaryDirectory()
        self.addCleanup(self.codex_home.cleanup)
        env = _env_without_session_key()
        env["CODEX_HOME"] = self.codex_home.name
        self.env_patcher = patch.dict(os.environ, env, clear=True)
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)

    def _write_config(self, text):
        with open(
            os.path.join(self.codex_home.name, "config.toml"),
            "w",
            encoding="utf-8",
        ) as f:
            f.write(text)

    def test_default_matches_current_codex_default(self):
        self.assertEqual(codex_council._max_parallel_roles(), 6)

    def test_reads_codex_agents_max_threads(self):
        self._write_config("[agents]\nmax_threads = 9\n")
        self.assertEqual(codex_council._max_parallel_roles(), 9)

    def test_pre311_fallback_reads_only_strict_positive_integer(self):
        self._write_config("[agents]\nmax_threads = 1_2 # intentional\n")
        with patch.object(codex_council, "tomllib", None):
            self.assertEqual(codex_council._max_parallel_roles(), 12)

    def test_pre311_fallback_does_not_read_nested_agents_table(self):
        self._write_config("[agents.worker]\nmax_threads = 99\n")
        with patch.object(codex_council, "tomllib", None):
            self.assertEqual(
                codex_council._max_parallel_roles(),
                codex_council.DEFAULT_MAX_PARALLEL,
            )

    def test_council_override_wins_over_codex_config(self):
        self._write_config("[agents]\nmax_threads = 9\n")
        os.environ[codex_council.MAX_PARALLEL_ENV] = "4"
        self.assertEqual(codex_council._max_parallel_roles(), 4)

    def test_invalid_config_falls_back_without_blocking_launch(self):
        self._write_config("this is not valid TOML = [")
        self.assertEqual(
            codex_council._max_parallel_roles(),
            codex_council.DEFAULT_MAX_PARALLEL,
        )

    def test_nonpositive_override_is_a_usage_error(self):
        os.environ[codex_council.MAX_PARALLEL_ENV] = "0"
        _assert_usage_exit(
            self,
            codex_council._max_parallel_roles,
            expect_in_stderr="must be a positive integer",
        )

    def test_nonnumeric_override_is_a_usage_error(self):
        os.environ[codex_council.MAX_PARALLEL_ENV] = "many"
        _assert_usage_exit(
            self,
            codex_council._max_parallel_roles,
            expect_in_stderr="must be a positive integer",
        )


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

    def test_long_role_id_is_hashed_in_state_key(self):
        rid = "parent-mapper-augmentation-auditor"
        with patch.dict(os.environ, _env_without_session_key(), clear=True):
            key = codex_council._project_key(rid)
        digest = hashlib.sha256(rid.encode("utf-8")).hexdigest()
        self.assertEqual(key, f"{FIXED_PROJECT_HASH}__role-sha256-{digest}")
        self.assertNotIn(rid, key)

    def test_very_long_role_ids_get_distinct_bounded_state_keys(self):
        with patch.dict(os.environ, _env_without_session_key(), clear=True):
            a = codex_council._project_key("a" * 100_000)
            b = codex_council._project_key("a" * 99_999 + "b")
        self.assertNotEqual(a, b)
        self.assertLess(len(a), 255)
        self.assertLess(len(b), 255)

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

    def test_server_overloaded_is_5xx_phrase(self):
        """Current codex-cli rewrites code-less overload errors to 'server
        overloaded'; 'backend overloaded' is older codex/provider text."""
        self.assertEqual(
            codex_council._retriable_class(
                "stream disconnected before completion: server overloaded"),
            "5xx",
        )
        self.assertEqual(
            codex_council._retriable_class("Server overloaded, retry shortly"),
            "5xx",
        )

    def test_backend_overloaded_kept_as_legacy_marker(self):
        self.assertEqual(
            codex_council._retriable_class("backend overloaded"), "5xx")

    def test_operator_overloaded_not_matched(self):
        self.assertIsNone(
            codex_council._retriable_class("error: operator overloaded in C++"))

    def test_stale_markers_match(self):
        self.assertTrue(codex_council._is_stale_resume_error(
            "Error: thread/resume failed: no rollout found for thread id abc (code -32600)"
        ))
        self.assertTrue(codex_council._is_stale_resume_error("THREAD NOT FOUND"))

    def test_retriable_helper_covers_both(self):
        # Bare "429" is no longer a marker (anchored parser covers numeric 429s),
        # so the substring helper now keys off the phrase forms.
        self.assertTrue(codex_council._is_retriable_error("429 too many requests"))
        self.assertTrue(codex_council._is_retriable_error("503 service unavailable"))
        self.assertFalse(codex_council._is_retriable_error("401 unauthorized"))

    def test_distinct_classes_dont_overlap(self):
        s = "no rollout found for thread id x"
        self.assertTrue(codex_council._is_stale_resume_error(s))
        self.assertFalse(codex_council._is_auth_error(s))
        self.assertFalse(codex_council._is_retriable_error(s))


# ---------- structured (HTTP-status-aware) classification ----------

def _nested_status_failure_text(status, message):
    """Build failure_text the way current codex-cli emits it: the numeric HTTP
    status lives only inside the nested JSON string under turn.failed."""
    nested = json.dumps({"type": "error", "status": status,
                         "error": {"message": message}})
    stdout = json.dumps({"type": "turn.failed", "error": {"message": nested}})
    return codex_council._failure_text(stdout, "")


class StructuredStatusClassifierTests(unittest.TestCase):
    """Status-first failure classification (current codex-cli).

    The numeric HTTP status parsed from the JSONL error body is the
    authoritative retriable signal; substring markers are a fallback only when
    no status is present. A non-retriable status (e.g. 400) suppresses the
    fallback so a stray '429'/'service unavailable' in a 4xx body is not
    mistaken for retriable.
    """

    # --- status extraction ---
    def test_extract_statuses_from_nested_json_key(self):
        self.assertEqual(
            codex_council._extract_statuses('{"type":"error","status":429,"error":{}}'),
            [429],
        )

    def test_extract_statuses_from_unexpected_status_prose(self):
        self.assertEqual(
            codex_council._extract_statuses(
                "unexpected status 529 <unknown status code>: backend overloaded"),
            [529],
        )

    def test_extract_statuses_from_last_status_prose(self):
        self.assertEqual(
            codex_council._extract_statuses(
                "exceeded retry limit, last status: 429 Too Many Requests"),
            [429],
        )

    def test_extract_statuses_requires_status_keyword(self):
        # The '429' inside a thread id must NOT be read as a status — this is
        # what keeps the stale-resume reorder safe.
        self.assertEqual(
            codex_council._extract_statuses(
                "no rollout found for thread id stale-429-sid (code -32600)"),
            [],
        )

    def test_extract_statuses_ignores_longer_digit_runs(self):
        self.assertEqual(codex_council._extract_statuses("status 4290 widgets"), [])

    # --- false positives fixed (status present, non-retriable) ---
    def test_status_400_with_bare_429_text_not_retriable(self):
        ft = _nested_status_failure_text(400, "branch revision 429 is invalid")
        self.assertIsNone(codex_council._retriable_class(ft))
        self.assertFalse(
            codex_council._classify_failure(ft, 1, "exec").startswith("[retriable:"))

    def test_status_400_service_unavailable_text_not_retriable(self):
        ft = _nested_status_failure_text(
            400, "plugin service unavailable for this account tier")
        self.assertIsNone(codex_council._retriable_class(ft))

    # --- false negatives fixed (real retriable status) ---
    def test_status_429_is_rate_limit(self):
        ft = _nested_status_failure_text(429, "rate limited")
        self.assertEqual(codex_council._retriable_class(ft), "rate-limit")

    def test_status_503_is_5xx(self):
        ft = _nested_status_failure_text(503, "temporarily down")
        self.assertEqual(codex_council._retriable_class(ft), "5xx")

    def test_status_529_overloaded_is_5xx(self):
        ft = _nested_status_failure_text(529, "backend overloaded")
        self.assertEqual(codex_council._retriable_class(ft), "5xx")

    def test_unexpected_status_529_prose_is_5xx(self):
        ft = codex_council._failure_text(
            "", "unexpected status 529 <unknown status code>: backend overloaded")
        self.assertEqual(codex_council._retriable_class(ft), "5xx")

    def test_http500_friendly_rewrite_is_5xx_via_phrase_fallback(self):
        # current codex-cli rewrites HTTP 500 to a code-less phrase; the
        # version-coupled marker catches it as a fallback (no status present).
        ft = codex_council._failure_text(
            "", "We're currently experiencing high demand, which may cause temporary errors.")
        self.assertIsNone(codex_council._structured_retriable_class(ft))
        self.assertEqual(codex_council._retriable_class(ft), "5xx")

    # --- substring fallback preserved when no status present ---
    def test_plain_429_stderr_still_retriable_via_fallback(self):
        self.assertEqual(
            codex_council._retriable_class("HTTP 429 too many requests"), "rate-limit")

    def test_literal_5xx_strings_still_retriable_via_fallback(self):
        self.assertEqual(codex_council._retriable_class("502 bad gateway"), "5xx")
        self.assertEqual(
            codex_council._retriable_class("Service unavailable, retry later"), "5xx")

    # --- resume-reorder guard: structured-retriable must NOT fire on stale ---
    def test_structured_retriable_does_not_fire_on_stale_429_message(self):
        self.assertIsNone(codex_council._structured_retriable_class(
            "Error: no rollout found for thread id stale-429-sid (code -32600)"))

    # --- pins: no bare 529 marker; usage-limit never retriable-by-substring ---
    def test_no_bare_529_substring_marker(self):
        self.assertNotIn("529", codex_council.TRANSIENT_5XX_MARKERS)
        self.assertNotIn("529", codex_council.RATE_LIMIT_MARKERS)

    def test_usage_limit_tokens_not_in_retriable_markers(self):
        for tok in ("usage_limit", "usage limit", "usage_limit_reached"):
            self.assertNotIn(tok, codex_council.RATE_LIMIT_MARKERS)
            self.assertNotIn(tok, codex_council.TRANSIENT_5XX_MARKERS)

    def test_quota_exceeded_is_not_retriable(self):
        # Usage/quota caps do not clear within a 5s backoff, so they are NOT
        # retriable — matching the documented Retries contract (DESIGN/SKILL).
        self.assertIsNone(codex_council._retriable_class("quota exceeded"))
        self.assertIsNone(codex_council._retriable_class(
            "You have exceeded your monthly quota exceeded for this plan"))
        self.assertNotIn("quota exceeded", codex_council.RATE_LIMIT_MARKERS)

    def test_codeless_overload_markers_pinned_and_no_false_positive(self):
        # Confirmed code-less fallback phrases (current and legacy) are caught...
        self.assertEqual(codex_council._retriable_class("backend overloaded"), "5xx")
        self.assertEqual(
            codex_council._retriable_class(
                "We're currently experiencing high demand, please retry"), "5xx")
        # ...but the markers are specific enough not to match unrelated text.
        self.assertIsNone(
            codex_council._retriable_class("operator overloaded method failed"))

    # --- anchored detection: keyword + reason phrase (robustness caveat) ---
    def test_anchored_http_keyword_status_detected(self):
        self.assertEqual(
            codex_council._extract_statuses("HTTP 429 too many requests"), [429])
        self.assertEqual(
            codex_council._extract_statuses("status code 429 returned"), [429])
        self.assertEqual(
            codex_council._structured_retriable_class("HTTP 429 Too Many Requests"),
            "rate-limit")

    def test_anchored_reason_phrase_status_detected(self):
        self.assertEqual(
            codex_council._extract_statuses("got 503 Service Unavailable"), [503])
        self.assertEqual(
            codex_council._structured_retriable_class("502 Bad Gateway from upstream"),
            "5xx")

    def test_anchored_status_beats_stale_text(self):
        # Caveat-2: a real anchored 429 alongside a stale-looking phrase is
        # retriable, so on the resume path it beats the stale branch.
        self.assertEqual(
            codex_council._structured_retriable_class(
                "HTTP 429 Too Many Requests; thread not found"),
            "rate-limit")

    def test_bare_digit_runs_are_not_anchored_statuses(self):
        # No keyword and no reason phrase -> not a status -> stale routing safe.
        self.assertEqual(
            codex_council._extract_statuses("commit 4291 merged at 503abc"), [])
        self.assertEqual(
            codex_council._extract_statuses("ticket #503 about checkout"), [])
        self.assertIsNone(codex_council._structured_retriable_class(
            "no rollout found for thread id stale-429-sid (code -32600)"))

    def test_extract_statuses_dedupes_keyword_and_reason(self):
        # "last status: 429 Too Many Requests" matches BOTH anchors -> one 429.
        self.assertEqual(
            codex_council._extract_statuses(
                "exceeded retry limit, last status: 429 Too Many Requests"),
            [429])

    def test_url_host_or_port_is_not_a_status(self):
        # A URL host/port must not be read as an HTTP status (the keyword
        # separator class excludes "/", so "http://..." does not match).
        self.assertEqual(
            codex_council._extract_statuses("url: http://127.0.0.1:49818/v1/responses"), [])
        self.assertEqual(
            codex_council._extract_statuses("http://429.example.invalid/path"), [])
        self.assertIsNone(
            codex_council._retriable_class("bad request, url: http://503.example.test/v1"))

    def test_no_bare_digit_run_false_positive_in_retriable_class(self):
        # Anchored detection covers real 429 forms, so a bare digit run is not
        # retriable at the _retriable_class level either (not just _extract_*).
        self.assertIsNone(codex_council._retriable_class("commit 4291 merged"))
        self.assertIsNone(codex_council._retriable_class(
            "no rollout found for thread id stale-429-sid (code -32600)"))
        self.assertNotIn("429", codex_council.RATE_LIMIT_MARKERS)

    def test_statusless_400_invalid_request_error_suppresses_fallback(self):
        # current codex-cli can surface a 4xx as raw JSON with NO status key but
        # `"type": "invalid_request_error"`; that must NOT be retried even when
        # its message text contains a 5xx reason phrase or rate-limit wording.
        raw_su = ('{"error": {"message": "service unavailable for this account '
                  'tier", "type": "invalid_request_error"}}')
        self.assertEqual(codex_council._extract_statuses(raw_su), [])
        self.assertIsNone(codex_council._retriable_class(raw_su))
        raw_tmr = ('{"error": {"message": "too many requests in batch payload", '
                   '"type": "invalid_request_error"}}')
        self.assertIsNone(codex_council._retriable_class(raw_tmr))

    def test_invalid_request_error_does_not_block_real_retriable(self):
        # An anchored retriable status wins regardless of any type...
        self.assertEqual(
            codex_council._retriable_class(
                '{"status":429,"error":{"type":"rate_limit_error"}}'),
            "rate-limit")
        # ...and a status-less rate-limit phrase with no client-error type still
        # retries (suppression only fires on the non-retriable type).
        self.assertEqual(
            codex_council._retriable_class("upstream says too many requests, slow down"),
            "rate-limit")


# ---------- prompt composition ----------

class ComposePromptTests(unittest.TestCase):
    def test_frames_shared_context_and_bookends_with_role_instruction(self):
        role = _make_role("architect", "Architect",
                          _valid_instruction("Review as architect"))
        out = codex_council._compose_prompt(role, "BODY")
        self.assertTrue(out.startswith(role.instruction + "\n\n"))
        self.assertTrue(out.endswith("\n\n" + role.instruction))
        self.assertIn(codex_council.COLLABORATION_BRIEF, out)
        self.assertIn("## Shared working context\n\nBODY", out)
        for concept in (
            "actual problem",
            "in-flight artifacts and modules",
            "active bugs, errors, and tests",
            "known unknowns",
            "possibly wrong assumptions",
            "converging or still exploratory",
            "reconcile with the other roles toward the same goal",
        ):
            self.assertIn(concept, out)
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


def _status_turn_failed_stdout(status, message):
    """A codex turn.failed JSONL line whose nested body carries an HTTP status."""
    nested = json.dumps({"type": "error", "status": status,
                         "error": {"message": message}})
    return json.dumps({"type": "turn.failed", "error": {"message": nested}})


class RunRoleStructuredStatusTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end (through _run_role_once / _run_role_attempts) of status-aware
    classification: false-positive suppression, 5xx retry, and the resume
    ordering where a structured 5xx beats the stale branch."""

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

    async def test_fresh_status400_with_429_text_not_tagged_retriable(self):
        """A fresh exec failing rc!=0 with a nested status-400 body that mentions
        '429' must NOT be tagged retriable (status suppresses the substring)."""
        role = _make_role("architect", "Architect")
        stdout = _status_turn_failed_stdout(400, "branch revision 429 is invalid")
        async def fake_subproc(cmd, prompt):
            return 1, stdout, ""
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertFalse(result.ok)
        self.assertFalse((result.error or "").startswith("[retriable:"))

    async def test_fresh_status529_tagged_retriable_5xx(self):
        role = _make_role("architect", "Architect")
        stdout = _status_turn_failed_stdout(529, "backend overloaded")
        async def fake_subproc(cmd, prompt):
            return 1, stdout, ""
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("[retriable:5xx]"))

    async def test_run_role_attempts_retries_on_structured_5xx(self):
        role = _make_role("architect", "Architect")
        stdout = _status_turn_failed_stdout(503, "temporarily down")
        async def fake_subproc(cmd, prompt):
            return 1, stdout, ""
        with patch.object(codex_council.asyncio, "sleep", AsyncMock(return_value=None)):
            with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
                result = await codex_council._run_role_attempts(role, "prompt")
        self.assertFalse(result.ok)
        self.assertEqual(result.attempts, codex_council.MAX_RETRY_ATTEMPTS)
        self.assertTrue(result.error.startswith("[retriable:5xx]"))

    async def test_resume_structured_503_retries_and_keeps_state(self):
        """On resume, a structured 5xx is retriable and must NOT clear state —
        structured-retriable is checked before the stale branch."""
        role = _make_role("architect", "Architect")
        codex_council.save_session("architect", "live-sid")
        stdout = _status_turn_failed_stdout(503, "temporarily down")
        async def fake_subproc(cmd, prompt):
            return 1, stdout, ""
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("[retriable:5xx]"))
        sid, _ = codex_council.load_session("architect")
        self.assertEqual(sid, "live-sid")  # NOT cleared (structured-retriable beat stale)

    async def test_resume_auth_first_even_with_status_and_stale_text(self):
        """Auth must win over BOTH structured-retriable (a 429 status) and stale
        on resume, and must never clear state."""
        role = _make_role("architect", "Architect")
        codex_council.save_session("architect", "live-sid")
        nested = json.dumps({"type": "error", "status": 429,
                             "error": {"message": "401 unauthorized; thread not found"}})
        stdout = json.dumps({"type": "turn.failed", "error": {"message": nested}})
        async def fake_subproc(cmd, prompt):
            return 1, stdout, ""
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("[auth]"))
        sid, _ = codex_council.load_session("architect")
        self.assertEqual(sid, "live-sid")  # auth never clears state

    async def test_resume_anchored_429_prose_beats_stale_and_keeps_state(self):
        """Caveat-2 end-to-end: a resume failing with anchored 'HTTP 429 Too Many
        Requests' AND a stale-looking phrase is retried (not stale-cleared),
        because anchored-status retriable is checked before the stale branch."""
        role = _make_role("architect", "Architect")
        codex_council.save_session("architect", "live-sid")
        async def fake_subproc(cmd, prompt):
            return 1, "", "HTTP 429 Too Many Requests while resuming; thread not found in cache"
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("[retriable:rate-limit]"))
        sid, _ = codex_council.load_session("architect")
        self.assertEqual(sid, "live-sid")  # NOT cleared — anchored retriable beat stale


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


class RunRoleAttemptsTests(unittest.IsolatedAsyncioTestCase):
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
            result = await codex_council._run_role_attempts(role, "prompt")
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
            result = await codex_council._run_role_attempts(role, "prompt")
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
            result = await codex_council._run_role_attempts(role, "prompt")
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

    def test_contended_nonblocking_lock_probe_closes_its_descriptor(self):
        class FakeLockFile:
            closed = False

            def close(self):
                self.closed = True

        fake_file = FakeLockFile()
        with patch("builtins.open", return_value=fake_file):
            with patch.object(
                codex_council.fcntl, "flock", side_effect=BlockingIOError
            ):
                lock = codex_council._try_role_state_lock("architect")
        self.assertIsNone(lock)
        self.assertTrue(fake_file.closed)

    async def test_parallel_fanout_preserves_order(self):
        async def fake_role(role, prompt):
            return codex_council.RoleResult(
                role=role, ok=True, text=f"reply-{role.id}",
                elapsed_seconds=0.1, attempts=1, thread_id=f"sid-{role.id}",
            )
        with patch.object(codex_council, "_run_role_attempts", side_effect=fake_role):
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
        with patch.object(codex_council, "_run_role_attempts", side_effect=fake_role):
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
        with patch.object(codex_council, "_run_role_attempts", side_effect=fake_role):
            results = await codex_council.run_council([custom], "body")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].role.id, "ml-fairness")
        self.assertTrue(results[0].ok)

    async def test_large_panel_never_exceeds_active_role_limit(self):
        roles = [_make_role(f"role-{i}", f"Role {i}") for i in range(12)]
        active = {"count": 0, "max": 0}

        async def fake_role(role, prompt):
            active["count"] += 1
            active["max"] = max(active["max"], active["count"])
            try:
                await asyncio.sleep(0.01)
                return codex_council.RoleResult(
                    role=role, ok=True, text="ok", elapsed_seconds=0.01,
                )
            finally:
                active["count"] -= 1

        with patch.object(codex_council, "_run_role_attempts", side_effect=fake_role):
            results = await codex_council.run_council(
                roles, "body", max_parallel=3,
            )
        self.assertEqual(len(results), 12)
        self.assertEqual(active["max"], 3)

    async def test_invalid_active_role_limit_fails_instead_of_deadlocking(self):
        with self.assertRaisesRegex(ValueError, "positive integer"):
            await codex_council.run_council(
                [self._roles("architect")[0]], "body", max_parallel=0,
            )

    async def test_cancellation_stops_active_and_queued_roles(self):
        roles = [_make_role(f"role-{i}", f"Role {i}") for i in range(3)]
        started = asyncio.Event()
        cancelled = []

        async def fake_role(role, prompt):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(role.id)
                raise

        with patch.object(codex_council, "_run_role_attempts", side_effect=fake_role):
            task = asyncio.create_task(
                codex_council.run_council(roles, "body", max_parallel=1)
            )
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(cancelled, ["role-0"])
        released = codex_council._try_role_state_lock("role-0")
        self.assertIsNotNone(released)
        codex_council._release_role_state_lock(released)

    async def test_cancellation_while_waiting_on_contended_lock_leaks_nothing(self):
        role = self._roles("architect")[0]
        held_lock = codex_council._try_role_state_lock(role.id)
        self.assertIsNotNone(held_lock)
        started_attempts = []

        async def fake_role(r, prompt):
            started_attempts.append(r.id)
            return codex_council.RoleResult(role=r, ok=True, text="unexpected")

        try:
            with patch.object(
                codex_council, "_run_role_attempts", side_effect=fake_role
            ):
                council = asyncio.create_task(
                    codex_council.run_council([role], "body", max_parallel=1)
                )
                await asyncio.sleep(0.05)
                council.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await council
        finally:
            codex_council._release_role_state_lock(held_lock)

        self.assertEqual(started_attempts, [])
        released = codex_council._try_role_state_lock(role.id)
        self.assertIsNotNone(released)
        codex_council._release_role_state_lock(released)

    async def test_role_waiting_on_state_lock_does_not_starve_free_role(self):
        """A continuity-lock waiter must not consume the only exec permit."""
        blocked, free = self._roles("architect", "security")
        held_lock = codex_council._try_role_state_lock(blocked.id)
        self.assertIsNotNone(held_lock)
        free_started = asyncio.Event()

        async def fake_role(role, prompt):
            if role.id == free.id:
                free_started.set()
            return codex_council.RoleResult(
                role=role, ok=True, text=f"reply-{role.id}",
                elapsed_seconds=0.01, attempts=1,
            )

        try:
            with patch.object(
                codex_council, "_run_role_attempts", side_effect=fake_role
            ):
                council = asyncio.create_task(
                    codex_council.run_council(
                        [blocked, free], "body", max_parallel=1
                    )
                )
                await asyncio.wait_for(free_started.wait(), timeout=1)
                self.assertFalse(council.done())
                codex_council._release_role_state_lock(held_lock)
                held_lock = None
                results = await asyncio.wait_for(council, timeout=1)
        finally:
            if held_lock is not None:
                codex_council._release_role_state_lock(held_lock)

        self.assertEqual([r.role.id for r in results], [blocked.id, free.id])
        self.assertTrue(all(r.ok for r in results))

    async def test_same_role_runs_are_serialized_by_state_lock(self):
        role = _make_role("architect", "Architect")
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        calls = {"count": 0}
        active = {"count": 0, "max": 0}

        async def fake_role(r, prompt):
            calls["count"] += 1
            active["count"] += 1
            active["max"] = max(active["max"], active["count"])
            try:
                if calls["count"] == 1:
                    first_started.set()
                    await release_first.wait()
                return codex_council.RoleResult(
                    role=r, ok=True, text=f"reply-{calls['count']}",
                    elapsed_seconds=0.01,
                )
            finally:
                active["count"] -= 1

        with patch.object(
            codex_council, "_run_role_attempts", side_effect=fake_role
        ):
            t1 = asyncio.create_task(
                codex_council.run_council([role], "prompt-1", max_parallel=1)
            )
            await first_started.wait()
            t2 = asyncio.create_task(
                codex_council.run_council([role], "prompt-2", max_parallel=1)
            )
            await asyncio.sleep(0.05)
            self.assertEqual(active["max"], 1)
            self.assertEqual(calls["count"], 1)
            release_first.set()
            results = await asyncio.gather(t1, t2)

        self.assertTrue(all(council[0].ok for council in results))
        self.assertEqual(calls["count"], 2)

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
            task = asyncio.create_task(
                codex_council.run_council(roles, "prompt", max_parallel=2)
            )
            await both_started.wait()
            self.assertEqual(active["max"], 2)
            release.set()
            results = await task

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
        with patch.object(codex_council, "_run_role_attempts", side_effect=fake_role):
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

    async def test_long_run_emits_periodic_status_heartbeat(self):
        release = asyncio.Event()

        async def fake_role(role, prompt):
            await release.wait()
            return codex_council.RoleResult(
                role=role, ok=True, text="ok", elapsed_seconds=0.1,
            )

        async def release_after_heartbeats():
            await asyncio.sleep(0.035)
            release.set()

        buf = io.StringIO()
        with patch.object(codex_council, "_run_role_attempts", side_effect=fake_role):
            with patch.object(codex_council, "PROGRESS_HEARTBEAT_SECS", 0.01):
                with contextlib.redirect_stderr(buf):
                    releaser = asyncio.create_task(release_after_heartbeats())
                    await codex_council.run_council(
                        self._roles("architect", "security"),
                        "body",
                        max_parallel=1,
                    )
                    await releaser

        heartbeats = [
            line for line in buf.getvalue().splitlines()
            if "still running after" in line
        ]
        self.assertGreaterEqual(len(heartbeats), 2)
        self.assertIn("completed=0/2", heartbeats[0])
        self.assertIn("active=1 (architect)", heartbeats[0])
        self.assertIn("queued=1", heartbeats[0])


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

    def test_long_role_id_is_unrestricted(self):
        raw = json.dumps([_role_json("a" * 100_000, "L")])
        roles = codex_council._parse_roles_json(raw)
        self.assertEqual(roles[0].id, "a" * 100_000)

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

    def test_string_instruction_rejected(self):
        """The legacy single-string form is gone: array-only contract."""
        raw = json.dumps([{"id": "x", "label": "L",
                           "instruction": _valid_instruction("review")}])
        _assert_usage_exit(
            self, lambda: codex_council._parse_roles_json(raw),
            expect_in_stderr="must be a JSON array",
        )

    def test_unicode_line_separator_in_item_is_normalized(self):
        """U+2028 inside a list item is whitespace-collapsed, not an error
        \u2014 the joined paragraph is single-line by construction."""
        raw = json.dumps([_role_json("x", "L", [
            "one\u2028two; if nothing material, say so clearly.",
            "Thoroughness beats speed.",
        ])])
        roles = codex_council._parse_roles_json(raw)
        self.assertIn("one two", roles[0].instruction)
        self.assertNotIn(" ", roles[0].instruction)

    def test_large_label_is_accepted(self):
        raw = json.dumps([_role_json("x", "a" * 100_000)])
        roles = codex_council._parse_roles_json(raw)
        self.assertEqual(roles[0].label, "a" * 100_000)

    def test_large_multibyte_instruction_is_accepted(self):
        instruction = "€" * 100_000 + "; if nothing material, say so clearly. " \
            "Thoroughness beats speed."
        raw = json.dumps([_role_json("x", "L", instruction)])
        roles = codex_council._parse_roles_json(raw)
        self.assertEqual(roles[0].instruction, instruction)

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

    def test_reported_issue_role_id_is_accepted(self):
        rid = "parent-mapper-augmentation-auditor"
        roles = codex_council._parse_roles_json(
            json.dumps([_role_json(rid, "Parent Mapper Auditor")])
        )
        self.assertEqual(roles[0].id, rid)

    def test_duplicate_id_in_payload_raises(self):
        raw = json.dumps([
            _role_json("alpha", "A", _valid_instruction("do a")),
            _role_json("alpha", "A2", _valid_instruction("again")),
        ])
        _assert_usage_exit(
            self, lambda: codex_council._parse_roles_json(raw),
            expect_in_stderr="duplicate id",
        )


class UnknownRoleKeyTests(unittest.TestCase):
    """Stray keys are the corruption signature of a glitched LLM write
    (GH issue #2: '"_": ""', '"instruction_note": ""'); they must be
    rejected with a rewrite-the-whole-file recovery message, never
    silently accepted."""

    def _entry_with(self, extra_keys):
        entry = _role_json("alpha", "A")
        entry.update(extra_keys)
        return json.dumps([entry])

    def test_issue2_filler_key_rejected(self):
        _assert_usage_exit(
            self,
            lambda: codex_council._parse_roles_json(self._entry_with({"_": ""})),
            expect_in_stderr="unknown field(s) '_'",
        )

    def test_instruction_note_filler_key_rejected(self):
        _assert_usage_exit(
            self,
            lambda: codex_council._parse_roles_json(
                self._entry_with({"instruction_note": ""})),
            expect_in_stderr="unknown field(s) 'instruction_note'",
        )

    def test_multiple_unknown_keys_all_named_sorted(self):
        raw = self._entry_with({"zz": 1, "_": ""})
        _assert_usage_exit(
            self, lambda: codex_council._parse_roles_json(raw),
            expect_in_stderr="unknown field(s) '_', 'zz'",
        )

    def test_recovery_message_demands_full_rewrite(self):
        _assert_usage_exit(
            self,
            lambda: codex_council._parse_roles_json(self._entry_with({"_": ""})),
            expect_in_stderr="Rewrite the whole roles.json file",
        )

    def test_unknown_key_reported_before_missing_field(self):
        """Freeze diagnostic precedence: a corrupted object with both a
        filler key and a missing required field reports the filler key,
        because the recovery (full rewrite) covers both defects."""
        raw = json.dumps([{"id": "x", "instruction": _valid_instruction("i"),
                           "_": ""}])
        _assert_usage_exit(
            self, lambda: codex_council._parse_roles_json(raw),
            expect_in_stderr="unknown field(s) '_'",
        )

    def test_exact_three_keys_still_accepted(self):
        roles = codex_council._parse_roles_json(json.dumps([_role_json("a", "A")]))
        self.assertEqual(roles[0].id, "a")


class InstructionListFormTests(unittest.TestCase):
    """List-form instruction: sentence-sized items the script joins into
    the single paragraph Codex sees. Exists so the LLM writer never has
    to emit a multi-KB single-line JSON string (GH issue #2)."""

    def _roles(self, items):
        return codex_council._parse_roles_json(
            json.dumps([{"id": "x", "label": "L", "instruction": items}])
        )

    def test_list_joined_with_single_spaces(self):
        roles = self._roles([
            "Audit the join logic.",
            "If nothing material, say so clearly.",
            "Thoroughness beats speed.",
        ])
        self.assertEqual(
            roles[0].instruction,
            "Audit the join logic. If nothing material, say so clearly. "
            "Thoroughness beats speed.",
        )

    def test_items_with_linebreaks_and_runs_are_normalized(self):
        roles = self._roles([
            "Audit\nthe  join logic.",
            "If nothing material,\r\nsay so clearly.",
            "Thoroughness beats speed.",
        ])
        self.assertEqual(
            roles[0].instruction,
            "Audit the join logic. If nothing material, say so clearly. "
            "Thoroughness beats speed.",
        )

    def test_single_item_list_accepted(self):
        roles = self._roles([_valid_instruction("solo")])
        self.assertEqual(roles[0].instruction, _valid_instruction("solo"))

    def test_empty_list_raises(self):
        _assert_usage_exit(
            self, lambda: self._roles([]),
            expect_in_stderr="instruction list must not be empty",
        )

    def test_non_string_item_raises_with_index(self):
        _assert_usage_exit(
            self, lambda: self._roles(["ok", 7, "Thoroughness beats speed."]),
            expect_in_stderr="instruction list item 1",
        )

    def test_blank_item_raises_with_index(self):
        _assert_usage_exit(
            self, lambda: self._roles(["ok", "   "]),
            expect_in_stderr="instruction list item 1",
        )

    def test_scope_phrase_checked_on_joined_paragraph(self):
        _assert_usage_exit(
            self,
            lambda: self._roles(["Review only.", "Thoroughness beats speed."]),
            expect_in_stderr="nothing material",
        )

    def test_cadence_sentence_must_end_joined_paragraph(self):
        _assert_usage_exit(
            self,
            lambda: self._roles([
                "Thoroughness beats speed.",
                "If nothing material, say so clearly.",
            ]),
            expect_in_stderr="Thoroughness beats speed.",
        )

    def test_large_joined_paragraph_is_accepted(self):
        items = ["a" * 5000, "b" * 5000,
                 "If nothing material, say so clearly.",
                 "Thoroughness beats speed."]
        roles = self._roles(items)
        self.assertEqual(roles[0].instruction, " ".join(items))

    def test_wrong_instruction_type_steers_to_array_form(self):
        raw = json.dumps([{"id": "x", "label": "L", "instruction": 5}])
        _assert_usage_exit(
            self, lambda: codex_council._parse_roles_json(raw),
            expect_in_stderr="JSON array of non-empty strings",
        )

    def test_list_item_error_names_full_rewrite_recovery(self):
        _assert_usage_exit(
            self, lambda: self._roles(["ok", 7]),
            expect_in_stderr="rewrite the whole roles.json",
        )

    def test_string_form_rejected_with_array_recovery(self):
        raw = json.dumps([{"id": "x", "label": "L", "instruction": "one two"}])
        _assert_usage_exit(
            self, lambda: codex_council._parse_roles_json(raw),
            expect_in_stderr="rewrite the whole roles.json file using the array form",
        )


class ResolveRolesJsonIntegrationTests(unittest.TestCase):
    """End-to-end of JSON parsing through resolution (custom roles only)."""

    def test_json_invocation_resolves(self):
        raw = json.dumps([
            {"id": "data-pipeline", "label": "Data",
             "instruction": [_valid_instruction("review pipeline")]},
            {"id": "ml-fairness", "label": "Fair",
             "instruction": [_valid_instruction("audit bias")]},
        ])
        custom = codex_council._parse_roles_json(raw)
        roles = codex_council._resolve_roles(custom)
        self.assertEqual([r.id for r in roles], ["data-pipeline", "ml-fairness"])

    def test_large_panel_resolves_via_json(self):
        entries = [
            {"id": f"role-{i}", "label": f"R{i}",
             "instruction": [_valid_instruction("x")]}
            for i in range(100)
        ]
        custom = codex_council._parse_roles_json(json.dumps(entries))
        roles = codex_council._resolve_roles(custom)
        self.assertEqual(len(roles), 100)


class ProjectRootCacheTests(unittest.TestCase):
    def setUp(self):
        codex_council._project_root.cache_clear()

    def tearDown(self):
        codex_council._project_root.cache_clear()

    def test_only_one_git_call_across_many_lookups(self):
        calls = {"count": 0}
        def fake_run(*args, **_kwargs):
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

    def test_symlinked_roles_file_is_rejected(self):
        target = os.path.join(self.tmp.name, "target.json")
        link = os.path.join(self.tmp.name, "roles.json")
        with open(target, "w", encoding="utf-8") as f:
            json.dump([_role_json("a", "A")], f)
        os.symlink(target, link)
        _assert_usage_exit(
            self,
            lambda: codex_council._read_roles_file(link),
            expect_in_stderr="symbolic links are not accepted",
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
                        "instruction": [_valid_instruction("réview €")]}], f)
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
                 "instruction": [_valid_instruction("do a")]},
                {"id": "beta", "label": "B",
                 "instruction": [_valid_instruction("do b")]},
            ], f)
        roles = codex_council._parse_roles_json(codex_council._read_roles_file(path))
        self.assertEqual([r.id for r in roles], ["alpha", "beta"])


class CheckStagingDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.chmod(self.tmp.name, 0o700)
        # Preflight now requires codex on PATH; keep these tests hermetic
        # so they pass on codex-less machines.
        which_patcher = patch("shutil.which", return_value="/fake/bin/codex")
        which_patcher.start()
        self.addCleanup(which_patcher.stop)

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
        self.assertIn("(1 roles; max parallel 6)", buf.getvalue())

    def test_missing_context_mentions_staging_hint(self):
        self._write_valid_roles()
        _assert_usage_exit(
            self,
            lambda: codex_council._check_staging_dir(self.tmp.name),
            expect_in_stderr="Staging hint",
        )

    def test_empty_context_is_rejected_as_usage_error(self):
        """Exit 2 like every other staging defect — exit 1 stays reserved
        for 'every role failed at runtime'."""
        self._write_valid_roles()
        self._write_context("   \n")
        _assert_usage_exit(
            self,
            lambda: codex_council._check_staging_dir(self.tmp.name),
            expect_in_stderr="empty or whitespace-only",
        )

    def test_missing_codex_fails_preflight(self):
        """'staging OK' while the codex binary is missing defers the
        failure to a background launch whose error lands only in err.log
        (GH issue #1's suspected nvm-switch failure class)."""
        self._write_valid_roles()
        self._write_context()
        out = io.StringIO()
        with patch("shutil.which", return_value=None):
            with contextlib.redirect_stdout(out):
                _assert_usage_exit(
                    self,
                    lambda: codex_council._check_staging_dir(self.tmp.name),
                    expect_in_stderr="Codex CLI not found on PATH",
                )
        self.assertNotIn("staging OK", out.getvalue())

    def test_missing_codex_message_is_install_neutral(self):
        self._write_valid_roles()
        self._write_context()
        buf = io.StringIO()
        with patch("shutil.which", return_value=None):
            with contextlib.redirect_stderr(buf):
                with self.assertRaises(SystemExit):
                    codex_council._check_staging_dir(self.tmp.name)
        err = buf.getvalue()
        self.assertNotIn("npm i -g", err)
        self.assertIn("/opt/homebrew/bin", err)
        self.assertIn("Current PATH:", err)

    def test_public_staging_dir_is_rejected(self):
        os.chmod(self.tmp.name, 0o755)
        self.addCleanup(lambda: os.chmod(self.tmp.name, 0o700))
        _assert_usage_exit(
            self,
            lambda: codex_council._check_staging_dir(self.tmp.name),
            expect_in_stderr="not private 0700",
        )

    def test_mode_failure_recovery_forbids_chmod_and_reuse(self):
        """GH issue #1: the old hint ('Create it with mktemp -d') was
        satisfiable by chmod/mkdir on the same predictable path; the
        recovery must demand abandoning the dir for a NEW mktemp one."""
        os.chmod(self.tmp.name, 0o775)
        self.addCleanup(lambda: os.chmod(self.tmp.name, 0o700))
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit) as ctx:
                codex_council._check_staging_dir(self.tmp.name)
        self.assertEqual(ctx.exception.code, 2)
        err = buf.getvalue()
        self.assertIn("abandon this directory", err)
        self.assertIn("do not chmod it", err)
        self.assertIn("do not reuse its name", err)
        self.assertIn("`mktemp -d` again", err)
        self.assertIn("re-Write BOTH roles.json and context.md", err)

    def test_symlink_to_private_dir_is_rejected(self):
        real = os.path.join(self.tmp.name, "real")
        os.mkdir(real, 0o700)
        link = os.path.join(self.tmp.name, "link")
        os.symlink(real, link)
        _assert_usage_exit(
            self,
            lambda: codex_council._check_staging_dir(link),
            expect_in_stderr="is a symlink",
        )

    def test_symlink_with_trailing_slash_is_rejected(self):
        """lstat('link/') follows the final symlink (the slash demands a
        directory target), so an un-normalized path bypassed the gate."""
        real = os.path.join(self.tmp.name, "real")
        os.mkdir(real, 0o700)
        with open(os.path.join(real, "roles.json"), "w", encoding="utf-8") as f:
            json.dump([_role_json("a", "A")], f)
        with open(os.path.join(real, "context.md"), "w", encoding="utf-8") as f:
            f.write("context")
        link = os.path.join(self.tmp.name, "link")
        os.symlink(real, link)
        _assert_usage_exit(
            self,
            lambda: codex_council._check_staging_dir(link + "/"),
            expect_in_stderr="is a symlink",
        )

    def test_unreadable_lstat_failure_is_not_reported_as_missing(self):
        """EACCES & co. must not be diagnosed as 'does not exist'."""
        outer = os.path.join(self.tmp.name, "outer")
        inner = os.path.join(outer, "inner")
        os.makedirs(inner, mode=0o700)
        os.chmod(outer, 0o000)
        self.addCleanup(lambda: os.chmod(outer, 0o700))
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit) as ctx:
                codex_council._check_staging_dir(inner)
        self.assertEqual(ctx.exception.code, 2)
        err = buf.getvalue()
        self.assertIn("cannot inspect", err)
        self.assertNotIn("does not exist", err)

    def test_foreign_owned_dir_is_rejected(self):
        self._write_valid_roles()
        self._write_context()
        real_euid = os.geteuid()
        with patch("os.geteuid", return_value=real_euid + 1):
            _assert_usage_exit(
                self,
                lambda: codex_council._check_staging_dir(self.tmp.name),
                expect_in_stderr="not the invoking user",
            )

    def test_nonexistent_path_does_not_invite_mkdir(self):
        """The old 'is not a directory' wording plausibly invited
        `mkdir <same path>` — recreating exactly issue #1's 0775 dir."""
        missing = os.path.join(self.tmp.name, "missing")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit) as ctx:
                codex_council._check_staging_dir(missing)
        self.assertEqual(ctx.exception.code, 2)
        err = buf.getvalue()
        self.assertIn("does not exist", err)
        self.assertIn("do not mkdir it", err)

    def test_regular_file_is_rejected_as_not_directory(self):
        path = os.path.join(self.tmp.name, "afile")
        with open(path, "w", encoding="utf-8") as f:
            f.write("x")
        _assert_usage_exit(
            self,
            lambda: codex_council._check_staging_dir(path),
            expect_in_stderr="is not a directory",
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

    def test_symlinked_context_file_is_rejected(self):
        target = self._path("target.md")
        link = self._path()
        with open(target, "w", encoding="utf-8") as f:
            f.write("context")
        os.symlink(target, link)
        _assert_usage_exit(
            self,
            lambda: codex_council._read_context_file(link),
            expect_in_stderr="symbolic links are not accepted",
        )

    def test_empty_context_file_is_usage_error(self):
        """Exit 2 like every other staging defect; exit 1 stays reserved
        for runtime failures (stdin defects, all-roles-failed)."""
        path = self._path()
        with open(path, "w", encoding="utf-8") as f:
            f.write("   \n")
        _assert_usage_exit(
            self,
            lambda: codex_council._read_context_file(path),
            expect_in_stderr="empty or whitespace-only",
        )

    def test_empty_context_message_names_recovery(self):
        path = self._path()
        with open(path, "w", encoding="utf-8") as f:
            f.write("   \n")
        _assert_usage_exit(
            self,
            lambda: codex_council._read_context_file(path),
            expect_in_stderr="re-run --check-staging-dir",
        )

    def test_context_file_invalid_utf8_is_a_usage_error(self):
        path = self._path()
        with open(path, "wb") as f:
            f.write(b"\xff\xfe bad")
        _assert_usage_exit(
            self,
            lambda: codex_council._read_context_file(path),
            expect_in_stderr="not valid UTF-8",
        )

    def test_large_context_file_is_accepted_without_truncation(self):
        path = self._path()
        content = "€" * 4_000_000
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        self.assertEqual(codex_council._read_context_file(path), content)


class ReadStdinBodyTests(unittest.TestCase):
    def test_returns_decoded_body(self):
        self.assertEqual(codex_council._read_stdin_body(io.BytesIO(b"hello")), "hello")

    def test_large_multibyte_stdin_is_accepted_without_truncation(self):
        content = "€" * 4_000_000
        self.assertEqual(
            codex_council._read_stdin_body(io.BytesIO(content.encode("utf-8"))),
            content,
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


class PromptCompositionTests(unittest.TestCase):
    def test_large_prompt_is_composed_without_truncation(self):
        role = codex_council.Role("architect", "Architect", "i")
        body = "b" * 12_000_000
        prompt = codex_council._compose_prompt(role, body)
        self.assertEqual(
            prompt,
            (
                f"i\n\n{codex_council.COLLABORATION_BRIEF}\n\n"
                f"## Shared working context\n\n{body}\n\ni"
            ),
        )
        self.assertEqual(prompt.count(body), 1)


class ArgParseTests(unittest.TestCase):
    def test_help_describes_contextual_programmatic_collaboration(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as ctx:
                codex_council._parse_args(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        help_text = out.getvalue()
        self.assertIn("context-grounded, role-framed Codex collaborators", help_text)
        self.assertIn("implementation, research, or problem-solving", help_text)
        self.assertIn("there is no built-in catalog", help_text)

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

    def _read_repo_file(self, *parts):
        with open(self._repo_file(*parts), encoding="utf-8") as f:
            return f.read()

    @staticmethod
    def _flat(text):
        return " ".join(text.split())

    def test_skill_context_pipelines_are_fail_closed_and_filename_safe(self):
        skill = self._read_repo_file(
            "plugins", "codex-council", "skills", "codex-council", "SKILL.md"
        )
        reference = self._read_repo_file(
            "plugins", "codex-council", "skills", "codex-council",
            "references", "context-staging.md",
        )
        self.assertIn("references/context-staging.md", skill)
        for required in (
            "set -euo pipefail",
            "git ls-files -z",
            "read -r -d ''",
            "file --brief --mime --",
            "git diff --cached",
        ):
            self.assertIn(required, reference)

    def test_runtime_and_skill_have_no_stale_size_or_panel_caps(self):
        script_path = self._repo_file(
            "plugins", "codex-council", "skills", "codex-council",
            "scripts", "codex_council.py",
        )
        skill_path = self._repo_file(
            "plugins", "codex-council", "skills", "codex-council", "SKILL.md"
        )
        with open(script_path, encoding="utf-8") as f:
            script = f.read()
        with open(skill_path, encoding="utf-8") as f:
            skill = f.read()
        for stale_name in (
            "\nMAX_PARALLEL =", "MAX_STDIN_BYTES", "MAX_PROMPT_BYTES",
            "ROLE_ID_MAX_LEN", "ROLE_LABEL_MAX_BYTES",
            "ROLE_INSTRUCTION_MAX_BYTES", "_validate_prompt_size",
        ):
            self.assertNotIn(stale_name, script)
        for stale_contract in (
            "Max 6 roles per call", "≤32 chars", "≤80 UTF-8 bytes",
            "≤8192 UTF-8 bytes", "over 10 MiB", "tail -c 131072",
            "size <= 32768",
        ):
            self.assertNotIn(stale_contract, skill)
        self.assertIn(
            "no plugin-imposed content-size or panel-count caps",
            skill,
        )
        self.assertIn("never truncates", skill)

    def test_skill_documents_adaptive_context_concurrency_and_progress(self):
        path = self._repo_file(
            "plugins", "codex-council", "skills", "codex-council", "SKILL.md"
        )
        with open(path, encoding="utf-8") as f:
            text = f.read()
        for required in (
            "decision-complete working set",
            "Recent working context at high fidelity",
            "Older durable context as a faithful summary",
            "CODEX_COUNCIL_MAX_PARALLEL",
            "in-process queue",
            "status heartbeat every 30 minutes",
            "one-shot 30-minute wake-up",
            "TaskOutput",
        ):
            self.assertIn(required, text)
        self.assertNotIn("every role is launched in parallel", text)

    def test_skill_frontmatter_uses_contextual_programmatic_routing(self):
        text = self._read_repo_file(
            "plugins", "codex-council", "skills", "codex-council", "SKILL.md"
        )
        frontmatter = text.split("---", 2)[1]
        flat = self._flat(frontmatter)
        for required in (
            "codex council",
            "codex coterie",
            "codex team",
            "/codex-council:codex-council",
            "project implementation",
            "computer science",
            "software/ML engineering",
            "DevSecOps",
            "autonomously ask and answer contextual working questions",
        ):
            self.assertIn(required, flat)
        for stale in (
            "Auto-use ONLY",
            "Otherwise stop",
            "only if all three",
            "Only proceed past this gate",
        ):
            self.assertNotIn(stale, text)

    def test_disambiguation_prefers_codex_before_claude_ultracode(self):
        text = self._read_repo_file(
            "plugins", "codex-council", "skills", "codex-council", "SKILL.md"
        )
        section = text.split(
            "## Disambiguation when the requested agent workflow is unclear", 1
        )[1].split("## Step 1", 1)[0]
        flat = self._flat(section)
        self.assertIn(
            "Question: \"Did you mean Claude's built-in Agent subagents, "
            "or the Codex council/coterie/team?\"",
            flat,
        )
        self.assertIn('Header: "Which?"', flat)
        codex = 'Option 1: "codex-council (Recommended)"'
        claude = 'Option 2: "Claude dynamic workflow (ultracode)"'
        self.assertIn(codex, flat)
        self.assertIn(claude, flat)
        self.assertLess(flat.index(codex), flat.index(claude))
        self.assertIn("OpenAI Codex role-framed collaborators", flat)
        self.assertIn("Claude Code's built-in Agent subagents", flat)
        self.assertIn("never an automatic stop", flat)
        self.assertIn("Do not ask merely because an exact trigger name is absent", flat)
        self.assertNotIn("Claude Agent subagents (Recommended)", section)

    def test_step1_synthesizes_roles_from_the_full_live_situation(self):
        text = self._read_repo_file(
            "plugins", "codex-council", "skills", "codex-council", "SKILL.md"
        )
        section = text.split("## Step 1", 1)[1].split("## Step 2", 1)[0]
        flat = self._flat(section)
        for required in (
            "Privately ask and answer",
            "larger problem",
            "project are they implementing",
            "files, modules, features, objects, drafts, datasets, queries",
            "bugs, errors, symptoms, regressions",
            "hypotheses and evidence",
            "known unknowns",
            "unknown unknowns or blind spots",
            "unstated, contradictory, outdated, or possibly wrong",
            "converging on a defined goal",
            "zigzagging through exploratory unknowns",
            "Ask the user only when a missing choice would materially change",
        ):
            self.assertIn(required, flat)

    def test_context_working_set_passes_the_full_situational_map(self):
        text = self._read_repo_file(
            "plugins", "codex-council", "skills", "codex-council", "SKILL.md"
        )
        section = text.split("## Building the context", 1)[1].split(
            "## Runtime continuity", 1
        )[0]
        flat = self._flat(section)
        for required in (
            "Problem, project, trajectory, and immediate objective",
            "exploratory/zigzagging through unknowns",
            "files, modules, features, objects, drafts",
            "bugs, errors, symptoms, regressions",
            "attempted fixes, working theories",
            "Recent working context at high fidelity",
            "Current primary evidence",
            "Older durable context as a faithful summary",
            "known unknowns, plausible blind spots",
            "unstated or possibly wrong assumptions",
            "Live problem-solving and implementation map",
        ):
            self.assertIn(required, flat)

    def test_programmatic_domain_lean_is_synced_without_a_role_catalog(self):
        paths = (
            ("plugins", "codex-council", "skills", "codex-council", "SKILL.md"),
            ("README.md",),
            ("DESIGN.md",),
        )
        for parts in paths:
            flat = self._flat(self._read_repo_file(*parts)).lower()
            for required in (
                "general-purpose",
                "project implementation",
                "computer science",
                "software",
                "ml/ai engineering",
                "devsecops",
                "technical research",
            ):
                self.assertIn(required, flat, f"missing {required!r} in {parts}")
        combined = "\n".join(self._read_repo_file(*parts) for parts in paths)
        self.assertIn("no built-in role catalog", combined.lower())

    def test_skill_core_stays_within_progressive_disclosure_budget(self):
        text = self._read_repo_file(
            "plugins", "codex-council", "skills", "codex-council", "SKILL.md"
        )
        self.assertLessEqual(len(text.splitlines()), 500)

    def test_plugin_copy_is_synced_on_adaptive_general_purpose_collaboration(self):
        canonical = (
            "Adaptive, context-driven Codex council for project implementation, "
            "computer science, software/ML engineering, DevSecOps, research, and "
            "other complex work — Claude orchestrates role-framed agents to "
            "collaborate and reconcile toward one shared goal."
        )
        marketplace_path = self._repo_file(".claude-plugin", "marketplace.json")
        manifest_path = self._repo_file(
            "plugins", "codex-council", ".claude-plugin", "plugin.json"
        )
        skill_path = self._repo_file(
            "plugins", "codex-council", "skills", "codex-council", "SKILL.md"
        )
        readme_path = self._repo_file("README.md")
        with open(marketplace_path, encoding="utf-8") as f:
            marketplace = json.load(f)
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertEqual(marketplace["metadata"]["description"], canonical)
        self.assertEqual(marketplace["plugins"][0]["description"], canonical)
        self.assertEqual(manifest["description"], canonical)
        with open(skill_path, encoding="utf-8") as f:
            skill = f.read()
        with open(readme_path, encoding="utf-8") as f:
            readme = f.read()
        combined = skill + "\n" + readme
        self.assertIn("adaptive", combined.lower())
        self.assertIn("general-purpose", combined)
        self.assertIn("AGI-style", combined)
        self.assertIn("not a claim", combined)
        self.assertNotIn("Multi-perspective parallel Codex review —", combined)

    def test_skill_and_readme_share_all_explicit_codex_trigger_names(self):
        paths = (
            self._repo_file(
                "plugins", "codex-council", "skills", "codex-council",
                "SKILL.md",
            ),
            self._repo_file("README.md"),
        )
        for path in paths:
            with open(path, encoding="utf-8") as f:
                text = f.read().lower()
            for trigger in ("codex council", "codex coterie", "codex team"):
                self.assertIn(trigger, text)

    def test_readme_dev_hook_keeps_diagnostics(self):
        path = self._repo_file("README.md")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("codex-council-dev-link.log", text)
        self.assertNotIn(">/dev/null 2>&1 || true", text)

    def test_design_md_resume_footgun_describes_uuid_error_path(self):
        """T2a: the corrected wording must describe the real current codex-cli behavior
        (unknown UUID errors; only a non-UUID name silently spawns), not the
        old inaccurate 'bogus_or_invalid_uuid silently falls through' claim."""
        path = self._repo_file("DESIGN.md")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("no rollout found", text)
        self.assertIn("thread *name*", text)
        self.assertNotIn("bogus_or_invalid_uuid", text)

    def test_design_md_documents_structured_status_classification(self):
        path = self._repo_file("DESIGN.md")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("_extract_statuses", text)
        self.assertIn("500", text)
        self.assertIn("Usage/quota", text)

    def test_skill_md_documents_vscode_pid_caveat(self):
        path = self._repo_file(
            "plugins", "codex-council", "skills", "codex-council",
            "references", "runtime-behavior.md")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("same VS Code window", text)
        self.assertIn("CODEX_COUNCIL_SESSION_KEY", text)

    def test_skill_md_documents_usage_limit_nonretriable(self):
        path = self._repo_file(
            "plugins", "codex-council", "skills", "codex-council",
            "references", "runtime-behavior.md")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("Usage/quota-limit", text)


if __name__ == "__main__":
    unittest.main()
