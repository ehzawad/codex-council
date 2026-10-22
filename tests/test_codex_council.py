"""Unit tests for codex_council.py and the sibling modules it imports.

Runs without the Codex CLI installed. Covers helper behavior:
key/state per role, JSONL parsing, error classifiers, prompt
composition, command shape, the resume-thread-id mismatch footgun,
retry-on-retriable, and fan-out aggregation. Documentation contracts are
tested in tests/test_documentation_contract.py by DocumentationContractTests.
Reply files and per-role overrides are covered in
tests/test_replies_and_overrides.py; --follow, --status, and --reap in
tests/test_liveness.py; model discovery and the selection contract in
tests/test_model_discovery.py and tests/test_model_selection.py; the module
layout in tests/test_module_layout.py.

Lives outside the plugin subtree so end-user installs don't bundle it.
Run from repo root:
    python3 -m unittest discover -s tests -p 'test_*.py'
"""

import asyncio
import contextlib
import hashlib
import inspect
import io
import json
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# council_testlib puts the runner's scripts directory on sys.path, so it is
# imported before the runner modules.
import council_testlib  # noqa: E402,F401
import codex_council  # noqa: E402
import council_common  # noqa: E402
import council_failures  # noqa: E402
import council_liveness  # noqa: E402
import council_selection  # noqa: E402
from council_testlib import (  # noqa: E402
    FIXED_PROJECT_ROOT,
    SCRIPTS_DIR,
    assert_usage_exit as _assert_usage_exit,
)


FIXED_PROJECT_HASH = hashlib.sha256(FIXED_PROJECT_ROOT.encode()).hexdigest()[:16]


def _env_without_session_key():
    """Current env minus explicit and auto council session-scope keys."""
    excluded = {
        codex_council.SESSION_KEY_ENV,
        codex_council.MAX_PARALLEL_ENV,
        codex_council.STALL_SECS_ENV,
        *codex_council.AUTO_SESSION_ENV_VARS,
    }
    return {k: v for k, v in os.environ.items() if k not in excluded}


def _codex_run(rc, stdout, stderr, **flags):
    """Structured subprocess result for fake _run_codex_subprocess doubles."""
    return codex_council.CodexRun(
        returncode=rc, stdout=stdout, stderr=stderr, **flags
    )


def _retriable(text):
    """The retry class the classifier gives failure text on the fresh path
    with no structured records and no model sent, or None."""
    verdict = council_failures._failure_verdict(text, (), None)
    return verdict.kind if verdict.retriable else None


def _anchored(text):
    """The retry class of the anchored HTTP statuses in text, or None."""
    return council_failures._anchored_retriable_class(
        council_failures._extract_statuses(text))


def _classify(text, rc=1, phase="exec",
              decision=council_selection._INHERIT_DECISION):
    """The tagged error text for a failure, classified the way the runner
    does: one verdict, then the formatter."""
    verdict = council_failures._failure_verdict(
        text, (), decision.dispatch_model)
    return council_failures._classify_failure(text, rc, phase, decision,
                                              verdict)


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

    def test_retired_disable_switch_changes_nothing(self):
        env = _env_without_session_key()
        env["CODEX_COUNCIL_DISABLE_AUTO_SESSION_KEY"] = "1"
        env["TERM_SESSION_ID"] = "term-123"
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(codex_council._session_key(),
                             "TERM_SESSION_ID=term-123")


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

    def test_default_is_six(self):
        self.assertEqual(codex_council._max_parallel_roles(), 6)

    def test_codex_configuration_does_not_set_the_limit(self):
        for text in ("[agents]\nmax_threads = 9\n",
                     "[agents]\nmax_concurrent_threads_per_session = 9\n",
                     "this is not valid TOML = ["):
            with self.subTest(text=text):
                self._write_config(text)
                self.assertEqual(codex_council._max_parallel_roles(),
                                 codex_council.DEFAULT_MAX_PARALLEL)

    def test_council_override_sets_the_limit(self):
        os.environ[codex_council.MAX_PARALLEL_ENV] = "4"
        self.assertEqual(codex_council._max_parallel_roles(), 4)

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


class StatePrivacyTests(unittest.TestCase):
    """The saved-thread state directory and lock files are private whatever
    the umask; an unsafe existing directory is refused, never chmodded."""

    def setUp(self):
        self.project_patcher = patch.object(
            codex_council, "_project_root", return_value=FIXED_PROJECT_ROOT
        )
        self.project_patcher.start()
        self.addCleanup(self.project_patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, _env_without_session_key(),
                              clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def _state(self, path):
        patcher = patch.object(codex_council, "STATE_DIR", path)
        patcher.start()
        self.addCleanup(patcher.stop)
        return path

    def _umask(self, mask):
        previous = os.umask(mask)
        self.addCleanup(os.umask, previous)

    def _mode(self, path):
        return stat.S_IMODE(os.lstat(path).st_mode)

    def test_new_state_dir_lock_and_state_are_private_under_any_umask(self):
        for mask in (0o022, 0o000):
            with self.subTest(umask=f"{mask:03o}"):
                xdg = os.path.join(self.tmp.name, f"xdg-{mask:03o}", "state")
                state = self._state(os.path.join(xdg, "codex-council"))
                self._umask(mask)
                lock = codex_council._try_role_state_lock("architect")
                self.assertIsNotNone(lock)
                codex_council._release_role_state_lock(lock)
                codex_council.save_session("architect", "sid-private")
                self.assertEqual(self._mode(state), 0o700)
                # Missing ancestors are created 0700 as well (XDG asks it).
                self.assertEqual(self._mode(xdg), 0o700)
                self.assertEqual(
                    self._mode(codex_council._state_lock_path("architect")),
                    0o600)
                self.assertEqual(
                    self._mode(codex_council._state_path("architect")), 0o600)

    def test_an_owned_too_open_state_dir_and_lock_are_tightened(self):
        parent = os.path.join(self.tmp.name, "xdg")
        os.mkdir(parent)
        os.chmod(parent, 0o755)
        state = self._state(os.path.join(parent, "codex-council"))
        os.mkdir(state)
        os.chmod(state, 0o755)
        lock_path = codex_council._state_lock_path("architect")
        with open(lock_path, "w"):
            pass
        os.chmod(lock_path, 0o644)
        lock = codex_council._try_role_state_lock("architect")
        codex_council._release_role_state_lock(lock)
        self.assertEqual(self._mode(state), 0o700)
        self.assertEqual(self._mode(lock_path), 0o600)
        # An existing ancestor is never the runner's to change.
        self.assertEqual(self._mode(parent), 0o755)

    def test_a_symlinked_state_dir_is_refused_and_left_alone(self):
        target = os.path.join(self.tmp.name, "elsewhere")
        os.mkdir(target)
        os.chmod(target, 0o755)
        state = self._state(os.path.join(self.tmp.name, "codex-council"))
        os.symlink(target, state)
        for call in (lambda: codex_council._try_role_state_lock("architect"),
                     lambda: codex_council.save_session("architect", "sid")):
            with self.assertRaisesRegex(codex_council.StateDirError,
                                        "is a symlink"):
                call()
        self.assertEqual(self._mode(target), 0o755)
        self.assertEqual(os.listdir(target), [])

    def test_a_foreign_owned_state_dir_is_refused_and_left_alone(self):
        state = self._state(os.path.join(self.tmp.name, "codex-council"))
        os.mkdir(state)
        os.chmod(state, 0o755)
        foreign = os.geteuid() + 1
        with patch("os.geteuid", return_value=foreign):
            with self.assertRaisesRegex(codex_council.StateDirError,
                                        "is owned by uid"):
                codex_council._ensure_state_dir()
        self.assertEqual(self._mode(state), 0o755)

    def test_a_non_regular_lock_file_is_refused(self):
        state = self._state(os.path.join(self.tmp.name, "codex-council"))
        codex_council._ensure_state_dir()
        os.mkfifo(codex_council._state_lock_path("architect"), 0o600)
        with self.assertRaisesRegex(codex_council.StateDirError,
                                    "not an owned regular file"):
            codex_council._try_role_state_lock("architect")
        self.assertEqual(self._mode(state), 0o700)

    def test_a_lock_path_that_cannot_be_opened_is_a_state_dir_error(self):
        """A symlink, a directory, a socket, or a file this user cannot
        open at the lock path is refused in the runner's own words (never
        a raw OSError), and nothing there is changed or followed."""
        self._state(os.path.join(self.tmp.name, "codex-council"))
        codex_council._ensure_state_dir()
        lock_path = codex_council._state_lock_path("architect")
        target = os.path.join(self.tmp.name, "target")

        def symlink():
            os.symlink(target, lock_path)

        def directory():
            os.mkdir(lock_path, 0o700)

        def unix_socket():
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(listener.close)
            # Bound by its relative name: the full path can be longer
            # than a socket address allows.
            cwd = os.getcwd()
            os.chdir(os.path.dirname(lock_path))
            try:
                listener.bind(os.path.basename(lock_path))
            finally:
                os.chdir(cwd)

        def unreadable():
            os.close(os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o600))
            os.chmod(lock_path, 0o000)

        cases = [("symlink", symlink), ("directory", directory),
                 ("socket", unix_socket)]
        if os.geteuid() != 0:  # root opens a mode-000 file anyway
            cases.append(("unreadable", unreadable))
        for name, plant in cases:
            with self.subTest(lock=name):
                plant()
                try:
                    before = os.lstat(lock_path)
                    with self.assertRaises(codex_council.StateDirError) as ctx:
                        codex_council._try_role_state_lock("architect")
                    message = str(ctx.exception)
                    self.assertIn(repr(lock_path), message)
                    self.assertIn("is not an owned regular file this user can "
                                  "open; it is not changed or used", message)
                    self.assertIsNone(ctx.exception.__cause__)
                    self.assertTrue(ctx.exception.__suppress_context__)
                    after = os.lstat(lock_path)
                    self.assertEqual((after.st_ino, after.st_mode),
                                     (before.st_ino, before.st_mode))
                    self.assertFalse(os.path.lexists(target))
                finally:
                    # Each case starts from a clean lock path even when
                    # an earlier one failed.
                    if os.path.isdir(lock_path) and not os.path.islink(lock_path):
                        os.rmdir(lock_path)
                    else:
                        os.remove(lock_path)

    def test_a_relative_xdg_state_home_is_ignored(self):
        """XDG_STATE_HOME counts only when absolute (the XDG specification
        ignores a relative value, a literal "~/..." included): a relative
        one creates nothing under the current directory."""
        home = os.path.join(self.tmp.name, "home")
        cwd = os.path.join(self.tmp.name, "project")
        os.mkdir(home, 0o700)
        os.mkdir(cwd, 0o700)
        expected = os.path.join(home, ".local", "state", "codex-council")
        for value in ("state", "~/state", "./state", ""):
            with self.subTest(XDG_STATE_HOME=value):
                proc = subprocess.run(
                    [sys.executable, "-c",
                     "import codex_council\n"
                     "print(codex_council._ensure_state_dir())\n"
                     "print(codex_council.STATE_DIR)\n"],
                    cwd=cwd, capture_output=True, text=True, timeout=60,
                    env=dict(_env_without_session_key(), HOME=home,
                             XDG_STATE_HOME=value, PYTHONPATH=SCRIPTS_DIR,
                             PYTHONDONTWRITEBYTECODE="1"))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout.splitlines(),
                                 [expected, expected])
                self.assertEqual(self._mode(expected), 0o700)
                self.assertEqual(os.listdir(cwd), [])
        absolute = os.path.join(self.tmp.name, "xdg")
        with patch.dict(os.environ, {"XDG_STATE_HOME": absolute}):
            self.assertEqual(codex_council._state_home(), absolute)

    def test_a_relative_home_is_not_used_either(self):
        """A relative $HOME would put ~/.local/state under the current
        directory too, and an empty one at /.local/state; the account's home
        directory stands in for both."""
        account = os.path.join(self.tmp.name, "account")

        class Entry:
            pw_dir = account

        os.environ.pop("XDG_STATE_HOME", None)
        for home in ("relhome", "./relhome", ""):
            with self.subTest(HOME=home), \
                    patch.dict(os.environ, {"HOME": home}), \
                    patch.object(codex_council.pwd, "getpwuid",
                                 return_value=Entry) as lookup:
                self.assertEqual(codex_council._state_home(),
                                 os.path.join(account, ".local", "state"))
                lookup.assert_called_once_with(os.geteuid())
        with patch.dict(os.environ, {"HOME": account}), \
                patch.object(codex_council.pwd, "getpwuid") as lookup:
            self.assertEqual(codex_council._state_home(),
                             os.path.join(account, ".local", "state"))
            lookup.assert_not_called()
        # No passwd entry either: a relative state directory, which is
        # refused rather than created under the current directory.
        with patch.dict(os.environ, {"HOME": ""}), \
                patch.object(codex_council.pwd, "getpwuid", side_effect=KeyError):
            self.assertFalse(os.path.isabs(codex_council._state_home()))
        cwd = os.getcwd()
        os.chdir(self.tmp.name)
        try:
            with patch.object(codex_council, "STATE_DIR",
                              os.path.join(".local", "state", "codex-council")), \
                    self.assertRaisesRegex(codex_council.StateDirError,
                                           "no absolute saved-thread state directory"):
                codex_council._ensure_state_dir()
            self.assertFalse(os.path.lexists(".local"))
        finally:
            os.chdir(cwd)

    def test_the_state_dir_checked_is_the_one_used(self):
        """A "link/.." spelling is checked where the I/O resolves it, never
        at its lexically normalized twin."""
        real = os.path.join(self.tmp.name, "real")
        os.makedirs(os.path.join(real, "inner"), 0o700)
        os.symlink(os.path.join(real, "inner"),
                   os.path.join(self.tmp.name, "link"))
        state = self._state(
            os.path.join(self.tmp.name, "link", "..", "codex-council"))
        self.assertEqual(codex_council._ensure_state_dir(), state)
        lock = codex_council._try_role_state_lock("architect")
        self.assertIsNotNone(lock)
        codex_council._release_role_state_lock(lock)
        codex_council.save_session("architect", "sid-spelled")
        self.assertEqual(codex_council.load_session("architect")[0],
                         "sid-spelled")
        used = os.path.join(real, "codex-council")
        self.assertEqual(self._mode(used), 0o700)
        self.assertEqual(sorted(os.listdir(used)), sorted(
            os.path.basename(codex_council._state_path("architect")) + suffix
            for suffix in ("", ".lock")))
        self.assertFalse(os.path.lexists(
            os.path.join(self.tmp.name, "codex-council")))

    def test_an_owned_state_dir_of_any_other_mode_is_reset_to_0700(self):
        """No group/other bits is not enough: an owned real directory
        missing owner bits (0500, 000, ...) is reset to 0700 too, so the
        pre-flight never passes a state directory the role lock then
        cannot use."""
        for mode in (0o500, 0o300, 0o100, 0o600, 0o000):
            with self.subTest(mode=f"{mode:04o}"):
                state = self._state(
                    os.path.join(self.tmp.name, f"state-{mode:04o}"))
                os.mkdir(state)
                os.chmod(state, mode)
                self.addCleanup(os.chmod, state, 0o700)
                self.assertEqual(codex_council._ensure_state_dir(), state)
                self.assertEqual(self._mode(state), 0o700)
                lock = codex_council._try_role_state_lock("architect")
                self.assertIsNotNone(lock)
                codex_council._release_role_state_lock(lock)
                codex_council.save_session("architect", "sid-reset")
                self.assertEqual(codex_council.load_session("architect")[0],
                                 "sid-reset")

    def test_a_state_dir_a_concurrent_council_created_unusable_is_reset(self):
        """Two first creations race: the other council's mkdir under an
        owner-clearing umask (0o200 leaves 0500) wins, so this one's
        FileExistsError path must still end with a usable 0700 directory."""
        state = self._state(os.path.join(self.tmp.name, "codex-council"))
        real_mkdir = os.mkdir

        def lose_the_race(path, mode=0o777):
            real_mkdir(path, 0o500)
            raise FileExistsError(path)

        with patch.object(codex_council.os, "mkdir", lose_the_race):
            codex_council._ensure_state_dir()
        self.addCleanup(os.chmod, state, 0o700)
        self.assertEqual(self._mode(state), 0o700)
        lock = codex_council._try_role_state_lock("architect")
        self.assertIsNotNone(lock)
        codex_council._release_role_state_lock(lock)

    def test_private_writes_are_0600_under_an_owner_clearing_umask(self):
        """A umask can clear owner bits too: under 0o400 a file created
        0600 comes out 0200. The shared atomic writer (state, status,
        supervisor and reply files) and execution.claim set 0600 through
        their descriptor."""
        state = self._state(os.path.join(self.tmp.name, "codex-council"))
        run_dir = os.path.join(self.tmp.name, "run")
        os.mkdir(run_dir, 0o700)
        self._umask(0o400)
        written = os.path.join(run_dir, "written.json")
        council_common._atomic_write_private(written, b'{"ok": true}')
        codex_council._claim_execution(run_dir)
        codex_council.save_session("architect", "sid-umask")
        for path in (written,
                     os.path.join(run_dir, codex_council.EXECUTION_CLAIM_FILENAME),
                     codex_council._state_path("architect")):
            with self.subTest(file=os.path.basename(path)):
                self.assertEqual(self._mode(path), 0o600)
        with open(written, "rb") as f:
            self.assertEqual(f.read(), b'{"ok": true}')
        self.assertEqual(self._mode(state), 0o700)
        self.assertEqual(codex_council.load_session("architect")[0],
                         "sid-umask")


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
        for corrupt in (b"{not json", b"\xff\xfe{", b"1" + b"0" * 5000):
            with self.subTest(corrupt=corrupt[:8]):
                with patch.dict(os.environ, _env_without_session_key(),
                                clear=True):
                    os.makedirs(self.tmp.name, exist_ok=True)
                    with open(codex_council._state_path("architect"),
                              "wb") as f:
                        f.write(corrupt)
                    sid, meta = codex_council.load_session("architect")
                self.assertIsNone(sid)
                self.assertIsNone(meta)

    def test_load_valid_json_non_dict_returns_none_pair(self):
        """Valid JSON that is not an object (a list, a bare string) must
        degrade to a fresh start like malformed JSON, not raise
        AttributeError on meta.get."""
        for corrupt in ("[]", '"hello"', "42", "null"):
            with self.subTest(corrupt=corrupt):
                with patch.dict(os.environ, _env_without_session_key(), clear=True):
                    os.makedirs(self.tmp.name, exist_ok=True)
                    with open(codex_council._state_path("architect"), "w") as f:
                        f.write(corrupt)
                    sid, meta = codex_council.load_session("architect")
                self.assertIsNone(sid)
                self.assertIsNone(meta)

    def test_a_fifo_state_file_reads_as_no_saved_thread_at_once(self):
        """A FIFO at the state path must not hang a resume role (which
        holds its continuity lock meanwhile): it reads as no saved thread,
        like corrupt state."""
        with patch.dict(os.environ, _env_without_session_key(), clear=True):
            path = codex_council._state_path("architect")
            os.mkfifo(path, 0o600)
            loaded = []
            worker = threading.Thread(
                target=lambda: loaded.append(
                    codex_council.load_session("architect")),
                daemon=True)
            worker.start()
            worker.join(10)
            if worker.is_alive():
                # Release the blocked open so the thread can finish.
                with contextlib.suppress(OSError):
                    os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
                worker.join(10)
                self.fail("load_session blocked on a FIFO state file")
        self.assertEqual(loaded, [(None, None)])

    def test_a_directory_state_path_leaks_no_descriptor(self):
        """fdopen of a directory raises with its descriptor still open, so
        each such read used to leak one."""
        with patch.dict(os.environ, _env_without_session_key(), clear=True):
            os.mkdir(codex_council._state_path("architect"))
            before = len(os.listdir("/dev/fd"))
            for _ in range(20):
                self.assertEqual(codex_council.load_session("architect"),
                                 (None, None))
            self.assertEqual(len(os.listdir("/dev/fd")), before)

    def test_a_symlinked_or_foreign_state_file_is_not_used(self):
        """The state file is never read through a symlink, and only an
        owned regular file counts as a saved thread."""
        with patch.dict(os.environ, _env_without_session_key(), clear=True):
            codex_council.save_session("architect", "sid-elsewhere")
            path = codex_council._state_path("architect")
            elsewhere = os.path.join(self.tmp.name, "elsewhere.json")
            os.rename(path, elsewhere)
            os.symlink(elsewhere, path)
            self.assertEqual(codex_council.load_session("architect"),
                             (None, None))
            os.remove(path)
            os.rename(elsewhere, path)
            self.assertEqual(codex_council.load_session("architect")[0],
                             "sid-elsewhere")
            with patch("os.geteuid", return_value=os.geteuid() + 1):
                self.assertEqual(codex_council.load_session("architect"),
                                 (None, None))

    def test_clear_session_removes_only_that_role(self):
        with patch.dict(os.environ, _env_without_session_key(), clear=True):
            codex_council.save_session("architect", "sid-a")
            codex_council.save_session("security", "sid-s")
            codex_council.clear_session("architect")
            a_sid, _ = codex_council.load_session("architect")
            s_sid, _ = codex_council.load_session("security")
        self.assertIsNone(a_sid)
        self.assertEqual(s_sid, "sid-s")

    def test_save_is_durable_and_leaves_only_the_state_file(self):
        """save_session uses the shared atomic writer: the bytes are
        fsynced before the rename, the file is 0600, and neither a save nor
        a save whose rename fails leaves anything else in STATE_DIR,
        whatever the temp files are named."""
        real_fsync = os.fsync
        synced = []

        def fsync_spy(fd):
            synced.append(fd)
            return real_fsync(fd)

        with patch.dict(os.environ, _env_without_session_key(), clear=True):
            path = codex_council._state_path("architect")
            with patch.object(os, "fsync", side_effect=fsync_spy):
                codex_council.save_session("architect", "x")
            self.assertTrue(synced)
            self.assertEqual(os.listdir(self.tmp.name),
                             [os.path.basename(path)])
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            with patch.object(os, "replace",
                              side_effect=OSError("rename failed")):
                with self.assertRaises(OSError):
                    codex_council.save_session("architect", "y")
            self.assertEqual(os.listdir(self.tmp.name),
                             [os.path.basename(path)])
            self.assertEqual(codex_council.load_session("architect")[0], "x")

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

    def test_agent_message_with_unicode_line_separators_is_not_dropped(self):
        """codex/serde_json may emit U+2028/U+2029/U+0085 UNescaped inside a
        JSON string. str.splitlines() would tear that one physical record into
        invalid fragments and drop the reply; splitting on "\\n" preserves it."""
        for cp, name in ((" ", "U+2028"),
                         (" ", "U+2029"),
                         ("", "U+0085")):
            with self.subTest(sep=name):
                text = f"part one{cp}part two"
                # ensure_ascii=False -> the separator is a LITERAL char in the
                # physical JSONL line, exactly as codex emits it.
                jsonl = json.dumps(
                    {"type": "item.completed",
                     "item": {"type": "agent_message", "text": text}},
                    ensure_ascii=False,
                )
                self.assertIn(cp, jsonl)
                self.assertEqual(codex_council.extract_final_message(jsonl), text)


class ExtractErrorMessagesTests(unittest.TestCase):
    def test_extracts_error_message(self):
        jsonl = '{"type":"error","message":"401 unauthorized"}'
        self.assertEqual(council_failures.extract_error_messages(jsonl), ["401 unauthorized"])

    def test_extracts_turn_failed_error_message(self):
        jsonl = '{"type":"turn.failed","error":{"message":"HTTP 429 Too Many Requests"}}'
        self.assertEqual(
            council_failures.extract_error_messages(jsonl),
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
            council_failures.extract_error_messages(jsonl),
            [inner, "The model is unsupported."],
        )

    def test_a_message_that_does_not_decode_is_kept_as_text(self):
        """A message too deeply nested or holding an out-of-range number
        is failure text, never an exception out of classification."""
        for message in ("[" * 100000 + "]" * 100000, "1" + "0" * 5000):
            with self.subTest(size=len(message)):
                jsonl = json.dumps({"type": "error", "message": message})
                self.assertEqual(
                    council_failures.extract_error_messages(jsonl), [message])


# ---------- error classifiers ----------

class ClassifierTests(unittest.TestCase):
    def test_auth_markers_match(self):
        self.assertTrue(council_failures._is_auth_error("401 Unauthorized: incorrect api key sk-..."))
        self.assertTrue(council_failures._is_auth_error(
            "stream error: authentication failed"))

    def test_rate_limit_markers_match(self):
        self.assertTrue(council_failures._is_rate_limit_error("HTTP 429 too many requests"))
        self.assertTrue(council_failures._is_rate_limit_error("rate_limit_exceeded"))

    def test_5xx_markers_match(self):
        self.assertTrue(council_failures._is_transient_5xx_error("502 bad gateway"))
        self.assertTrue(council_failures._is_transient_5xx_error("Service unavailable, retry later"))

    def test_server_overloaded_is_5xx_phrase(self):
        """codex-cli rewrites code-less overload errors to 'server
        overloaded'."""
        self.assertEqual(
            _retriable(
                "stream disconnected before completion: server overloaded"),
            "5xx",
        )
        self.assertEqual(
            _retriable(
                "Server overloaded, retry shortly"),
            "5xx",
        )

    def test_operator_overloaded_not_matched(self):
        self.assertIsNone(
            _retriable("error: operator overloaded in C++"))

    def test_stale_markers_match(self):
        self.assertTrue(council_failures._is_stale_resume_error(
            "Error: thread/resume failed: no rollout found for thread id abc (code -32600)"
        ))
        self.assertTrue(council_failures._is_stale_resume_error("THREAD NOT FOUND"))

    def test_retriable_class_covers_both(self):
        self.assertEqual(
            _retriable("429 too many requests"),
            "rate-limit")
        self.assertEqual(
            _retriable("503 service unavailable"),
            "5xx")
        self.assertIsNone(
            _retriable("401 unauthorized"))

    def test_distinct_classes_dont_overlap(self):
        s = "no rollout found for thread id x"
        self.assertTrue(council_failures._is_stale_resume_error(s))
        self.assertFalse(council_failures._is_auth_error(s))
        self.assertIsNone(_retriable(s))


# ---------- structured (HTTP-status-aware) classification ----------

def _nested_status_failure_text(status, message):
    """Build failure_text the way current codex-cli emits it: the numeric HTTP
    status lives only inside the nested JSON string under turn.failed."""
    nested = json.dumps({"type": "error", "status": status,
                         "error": {"message": message}})
    stdout = json.dumps({"type": "turn.failed", "error": {"message": nested}})
    return council_failures._failure_text(stdout, "")


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
            council_failures._extract_statuses('{"type":"error","status":429,"error":{}}'),
            [429],
        )

    def test_extract_statuses_from_unexpected_status_prose(self):
        self.assertEqual(
            council_failures._extract_statuses(
                "unexpected status 529 <unknown status code>: server overloaded"),
            [529],
        )

    def test_extract_statuses_from_last_status_prose(self):
        self.assertEqual(
            council_failures._extract_statuses(
                "exceeded retry limit, last status: 429 Too Many Requests"),
            [429],
        )

    def test_extract_statuses_requires_status_keyword(self):
        # The '429' inside a thread id must NOT be read as a status — this is
        # what keeps the stale-resume reorder safe.
        self.assertEqual(
            council_failures._extract_statuses(
                "no rollout found for thread id stale-429-sid (code -32600)"),
            [],
        )

    def test_extract_statuses_ignores_longer_digit_runs(self):
        self.assertEqual(council_failures._extract_statuses("status 4290 widgets"), [])

    # --- false positives fixed (status present, non-retriable) ---
    def test_status_400_with_bare_429_text_not_retriable(self):
        ft = _nested_status_failure_text(400, "branch revision 429 is invalid")
        self.assertIsNone(_retriable(ft))
        self.assertFalse(_classify(ft).startswith("[retriable:"))

    def test_status_400_service_unavailable_text_not_retriable(self):
        ft = _nested_status_failure_text(
            400, "plugin service unavailable for this account tier")
        self.assertIsNone(_retriable(ft))

    # --- false negatives fixed (real retriable status) ---
    def test_status_429_is_rate_limit(self):
        ft = _nested_status_failure_text(429, "rate limited")
        self.assertEqual(_retriable(ft), "rate-limit")

    def test_status_503_is_5xx(self):
        ft = _nested_status_failure_text(503, "temporarily down")
        self.assertEqual(_retriable(ft), "5xx")

    def test_status_529_overloaded_is_5xx(self):
        ft = _nested_status_failure_text(529, "server overloaded")
        self.assertEqual(_retriable(ft), "5xx")

    def test_unexpected_status_529_prose_is_5xx(self):
        ft = council_failures._failure_text(
            "", "unexpected status 529 <unknown status code>: server overloaded")
        self.assertEqual(_retriable(ft), "5xx")

    def test_http500_friendly_rewrite_is_5xx_via_phrase_fallback(self):
        # current codex-cli rewrites HTTP 500 to a code-less phrase; the
        # version-coupled marker catches it as a fallback (no status present).
        ft = council_failures._failure_text(
            "", "We're currently experiencing high demand, which may cause temporary errors.")
        self.assertIsNone(_anchored(ft))
        self.assertEqual(_retriable(ft), "5xx")

    # --- substring fallback preserved when no status present ---
    def test_plain_429_stderr_still_retriable_via_fallback(self):
        self.assertEqual(
            _retriable("HTTP 429 too many requests"), "rate-limit")

    def test_literal_5xx_strings_still_retriable_via_fallback(self):
        self.assertEqual(_retriable("502 bad gateway"), "5xx")
        self.assertEqual(
            _retriable("Service unavailable, retry later"), "5xx")

    # --- resume-reorder guard: the anchored class must NOT fire on stale ---
    def test_structured_retriable_does_not_fire_on_stale_429_message(self):
        self.assertIsNone(_anchored(
            "Error: no rollout found for thread id stale-429-sid (code -32600)"))

    # --- pins: no bare 529 marker; usage-limit never retriable-by-substring ---
    def test_no_bare_529_substring_marker(self):
        self.assertNotIn("529", council_failures.TRANSIENT_5XX_MARKERS)
        self.assertNotIn("529", council_failures.RATE_LIMIT_MARKERS)

    def test_usage_limit_tokens_not_in_retriable_markers(self):
        for tok in ("usage_limit", "usage limit", "usage_limit_reached"):
            self.assertNotIn(tok, council_failures.RATE_LIMIT_MARKERS)
            self.assertNotIn(tok, council_failures.TRANSIENT_5XX_MARKERS)

    def test_quota_exceeded_is_not_retriable(self):
        # Usage/quota caps do not clear within a 5s backoff, so they are NOT
        # retriable — matching the documented Retries contract (DESIGN/SKILL).
        self.assertIsNone(_retriable("quota exceeded"))
        self.assertIsNone(_retriable(
            "You have exceeded your monthly quota exceeded for this plan"))
        self.assertNotIn("quota exceeded", council_failures.RATE_LIMIT_MARKERS)

    def test_codeless_overload_markers_pinned_and_no_false_positive(self):
        # The code-less phrases codex-cli prints are caught...
        self.assertEqual(_retriable("server overloaded"), "5xx")
        self.assertEqual(
            _retriable(
                "We're currently experiencing high demand, please retry"), "5xx")
        # ...but the markers are specific enough not to match unrelated text.
        self.assertIsNone(
            _retriable("operator overloaded method failed"))

    # --- anchored detection: keyword + reason phrase (robustness caveat) ---
    def test_anchored_http_keyword_status_detected(self):
        self.assertEqual(
            council_failures._extract_statuses("HTTP 429 too many requests"), [429])
        self.assertEqual(
            council_failures._extract_statuses("status code 429 returned"),
            [429])
        self.assertEqual(
            _anchored("HTTP 429 Too Many Requests"),
            "rate-limit")

    def test_anchored_reason_phrase_status_detected(self):
        self.assertEqual(
            council_failures._extract_statuses("got 503 Service Unavailable"), [503])
        self.assertEqual(
            _anchored("502 Bad Gateway from upstream"),
            "5xx")

    def test_anchored_status_beats_stale_text(self):
        # Caveat-2: a real anchored 429 alongside a stale-looking phrase is
        # retriable, so on the resume path it beats the stale branch.
        self.assertEqual(
            _anchored(
                "HTTP 429 Too Many Requests; thread not found"),
            "rate-limit")

    def test_bare_digit_runs_are_not_anchored_statuses(self):
        # No keyword and no reason phrase -> not a status -> stale routing safe.
        self.assertEqual(
            council_failures._extract_statuses("commit 4291 merged at 503abc"), [])
        self.assertEqual(
            council_failures._extract_statuses("ticket #503 about checkout"),
            [])
        self.assertIsNone(_anchored(
            "no rollout found for thread id stale-429-sid (code -32600)"))

    def test_extract_statuses_dedupes_keyword_and_reason(self):
        # "last status: 429 Too Many Requests" matches BOTH anchors -> one 429.
        self.assertEqual(
            council_failures._extract_statuses(
                "exceeded retry limit, last status: 429 Too Many Requests"),
            [429])

    def test_url_host_or_port_is_not_a_status(self):
        # A URL host/port must not be read as an HTTP status (the keyword
        # separator class excludes "/", so "http://..." does not match).
        self.assertEqual(
            council_failures._extract_statuses("url: http://127.0.0.1:49818/v1/responses"), [])
        self.assertEqual(
            council_failures._extract_statuses("http://429.example.invalid/path"), [])
        self.assertIsNone(
            _retriable("bad request, url: http://503.example.test/v1"))

    def test_no_bare_digit_run_false_positive_in_the_verdict(self):
        # Anchored detection covers real 429 forms, so a bare digit run is not
        # retriable through the whole verdict either (not just _extract_*).
        self.assertIsNone(
            _retriable("commit 4291 merged"))
        self.assertIsNone(_retriable(
            "no rollout found for thread id stale-429-sid (code -32600)"))
        self.assertNotIn("429", council_failures.RATE_LIMIT_MARKERS)

    def test_statusless_400_invalid_request_error_suppresses_fallback(self):
        # current codex-cli can surface a 4xx as raw JSON with NO status key but
        # `"type": "invalid_request_error"`; that must NOT be retried even when
        # its message text contains a 5xx reason phrase or rate-limit wording.
        raw_su = ('{"error": {"message": "service unavailable for this account '
                  'tier", "type": "invalid_request_error"}}')
        self.assertEqual(council_failures._extract_statuses(raw_su), [])
        self.assertIsNone(_retriable(raw_su))
        raw_tmr = ('{"error": {"message": "too many requests in batch payload", '
                   '"type": "invalid_request_error"}}')
        self.assertIsNone(_retriable(raw_tmr))

    def test_invalid_request_error_does_not_block_real_retriable(self):
        # An anchored retriable status wins regardless of any type...
        self.assertEqual(
            _retriable(
                '{"status":429,"error":{"type":"rate_limit_error"}}'),
            "rate-limit")
        # ...and a status-less rate-limit phrase with no client-error type still
        # retries (suppression only fires on the non-retriable type).
        self.assertEqual(
            _retriable("upstream says too many requests, slow down"),
            "rate-limit")


class ObservedCodexStringClassifierTests(unittest.TestCase):
    """Regression pins for exact strings observed from current codex-cli,
    in both directions (tagged when genuine, suppressed when echoed in a
    non-retriable body)."""

    def test_model_at_capacity_rewrite_is_5xx(self):
        # codex rewrites an HTTP 503 server_is_overloaded/slow_down to this
        # exact code-less sentence.
        self.assertEqual(
            _retriable(
                "Selected model is at capacity. Please try a different model."),
            "5xx",
        )

    def test_request_was_throttled_stream_message_is_rate_limit(self):
        # codex SSE response.failed handling discards code/status_code/
        # statusCode and keeps only the message.
        self.assertEqual(
            _retriable(
                "stream disconnected before completion: Request was throttled"),
            "rate-limit",
        )

    def test_bare_throttled_is_not_a_marker(self):
        self.assertIsNone(_retriable(
            "the deploy was throttled by CI"))
        self.assertNotIn("throttled", council_failures.RATE_LIMIT_MARKERS)

    def test_raw_400_body_with_status_code_key_is_suppressed(self):
        # Observed raw HTTP-400 surface: the transport status only appears as
        # a status_code JSON key; the message echoes "service unavailable".
        # The anchored 400 must suppress the 5xx phrase fallback.
        raw = ('{"error":{"status_code":400,"message":"Plugin service '
               'unavailable for this account tier","type":"bad_request"}}')
        self.assertEqual(council_failures._extract_statuses(raw), [400])
        self.assertIsNone(_retriable(raw))

    def test_status_code_spelling_variants_are_anchored(self):
        self.assertEqual(
            council_failures._extract_statuses('{"statusCode":503,"x":1}'),
            [503])
        self.assertEqual(
            council_failures._extract_statuses("status-code 429 returned"),
            [429])
        self.assertEqual(
            council_failures._extract_statuses("status_code: 429"), [429])
        self.assertEqual(
            council_failures._extract_statuses("status code 429 returned"),
            [429])

    def test_error_code_prefix_is_not_anchored(self):
        # "Error code:" is SDK wording, not current codex wording; adding it
        # would misread SDK errors quoted inside raw 400 bodies.
        self.assertEqual(council_failures._extract_statuses("Error code: 429"), [])

    def test_refresh_token_failure_is_auth(self):
        msg = ("Your access token could not be refreshed because your "
               "refresh token has expired.")
        self.assertTrue(council_failures._is_auth_error(msg))
        self.assertTrue(_classify(msg).startswith("[auth]"))

    def test_no_bad_request_suppression_marker(self):
        self.assertNotIn(
            "bad_request", council_failures.NONRETRIABLE_ERROR_TYPE_MARKERS)


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
        # The brief: count-neutral, verifier-framed, non-interactive,
        # evidence-first.
        for concept in (
            "you may be the only role, or one of several",
            "an independent cross-model collaborator advancing the current task",
            "the user's goal, requirements, and constraints are authoritative",
            "claims to verify against the workspace, not facts to accept",
            "non-interactive",
            "do not ask the user",
            "Do not spawn subagents unless your role instruction asks",
            "verified evidence",
            "what remains unverified",
            "Size any testing to the change",
            "the result first",
            "open questions",
        ):
            self.assertIn(concept, out)
        self.assertNotIn("the other roles", codex_council.COLLABORATION_BRIEF)
        self.assertNotIn("source of truth", codex_council.COLLABORATION_BRIEF)
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

    def test_brief_ranks_earlier_turns_below_the_current_context(self):
        """A resumed role still holds its earlier turns; the brief itself
        tells it that the current shared context and the workspace win, right
        after the verifier framing."""
        self.assertIn(
            "not facts to accept. If this conversation already holds earlier "
            "turns, treat them as background; where they conflict with the "
            "shared working context below or the workspace, the current "
            "context and workspace win. This run is non-interactive",
            codex_council.COLLABORATION_BRIEF,
        )

    def test_brief_has_no_all_caps_emphasis(self):
        self.assertNotRegex(codex_council.COLLABORATION_BRIEF, r"[A-Z]{4,}")

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
        async def fake_subproc(cmd, prompt, role_id=""):
            return _codex_run(0, _fresh_jsonl("new-sid", "All good."), "")
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "All good.")
        sid, _ = codex_council.load_session("architect")
        self.assertEqual(sid, "new-sid")

    async def test_codex_fails_returns_classified_error(self):
        role = _make_role("architect", "Architect")
        async def fake_subproc(cmd, prompt, role_id=""):
            return _codex_run(1, "", "401 unauthorized: incorrect api key sk-...")
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("[auth]"))

    async def test_rate_limit_tagged_for_retry(self):
        role = _make_role("architect", "Architect")
        async def fake_subproc(cmd, prompt, role_id=""):
            return _codex_run(1, "", "HTTP 429 too many requests")
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("[retriable:rate-limit]"))

    async def test_5xx_tagged_for_retry(self):
        role = _make_role("architect", "Architect")
        async def fake_subproc(cmd, prompt, role_id=""):
            return _codex_run(1, "", "502 bad gateway")
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("[retriable:5xx]"))

    async def test_stdout_error_jsonl_classifies_auth(self):
        role = _make_role("architect", "Architect")
        stdout = json.dumps({"type": "error", "message": "401 unauthorized"})
        async def fake_subproc(cmd, prompt, role_id=""):
            return _codex_run(1, stdout, "")
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
        async def fake_subproc(cmd, prompt, role_id=""):
            return _codex_run(1, stdout, "")
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
        async def fake_subproc(cmd, prompt, role_id=""):
            return _codex_run(1, stdout, "")
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertFalse(result.ok)
        self.assertIn("The model is unsupported.", result.error)

    async def test_no_agent_message_returns_failure_without_saving(self):
        role = _make_role("architect", "Architect")
        async def fake_subproc(cmd, prompt, role_id=""):
            return _codex_run(0, json.dumps({"type": "thread.started", "thread_id": "x"}), "")
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
        async def fake_subproc(cmd, prompt, role_id=""):
            calls.append(cmd)
            if "resume" in cmd:
                return _codex_run(1, "", "Error: thread/resume failed: no rollout found for thread id stale-sid (code -32600)")
            return _codex_run(0, _fresh_jsonl("brand-new-sid", "fresh ok"), "")
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertTrue(result.ok)
        self.assertEqual(codex_council.load_session("architect")[0],
                         "brand-new-sid")
        self.assertEqual(len(calls), 2, "expected resume → fresh fallthrough")
        sid, _ = codex_council.load_session("architect")
        self.assertEqual(sid, "brand-new-sid")
        self.assertEqual(result.warning, codex_council.STALE_RESUME_WARNING)

    async def test_stale_resume_warning_survives_a_failed_fresh_run(self):
        role = _make_role("architect", "Architect")
        codex_council.save_session("architect", "stale-sid")
        async def fake_subproc(cmd, prompt, role_id=""):
            if "resume" in cmd:
                return _codex_run(1, "", "no rollout found for thread id stale-sid")
            return _codex_run(1, "", "fresh exec blew up")
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "p", attempt=1)
        self.assertFalse(result.ok)
        self.assertEqual(result.warning, codex_council.STALE_RESUME_WARNING)

    async def test_stale_resume_warning_survives_a_retried_fresh_run(self):
        """Stale resume, then a retriable 503 on the fresh run, then a fresh
        success on attempt 2: attempt 2 loads no thread, yet the final
        result still says the role's earlier turns are gone."""
        role = _make_role("architect", "Architect")
        codex_council.save_session("architect", "stale-sid")
        calls = []
        async def fake_subproc(cmd, prompt, role_id=""):
            calls.append("resume" if "resume" in cmd else "fresh")
            if calls[-1] == "resume":
                return _codex_run(1, "", "no rollout found for thread id stale-sid")
            if len(calls) == 2:
                return _codex_run(1, "", "503 Service Unavailable")
            return _codex_run(0, _fresh_jsonl("brand-new-sid", "fresh ok"), "")
        with patch.object(codex_council.asyncio, "sleep", AsyncMock(return_value=None)), \
                patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_attempts(role, "p")
        self.assertEqual(calls, ["resume", "fresh", "fresh"])
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.warning, codex_council.STALE_RESUME_WARNING)
        self.assertEqual(codex_council.load_session("architect")[0], "brand-new-sid")

    async def test_stale_resume_with_429_in_thread_id_still_restarts_fresh(self):
        role = _make_role("architect", "Architect")
        codex_council.save_session("architect", "stale-429-sid")
        calls = []
        async def fake_subproc(cmd, prompt, role_id=""):
            calls.append(cmd)
            if "resume" in cmd:
                return _codex_run(
                    1, "",
                    "Error: no rollout found for thread id stale-429-sid",
                )
            return _codex_run(0, _fresh_jsonl("brand-new-sid", "fresh ok"), "")
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
        async def fake_subproc(cmd, prompt, role_id=""):
            calls.append(cmd)
            if "resume" in cmd:
                return _codex_run(1, stdout, "")
            return _codex_run(0, _fresh_jsonl("brand-new-sid", "fresh ok"), "")
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
        async def fake_subproc(cmd, prompt, role_id=""):
            calls.append(cmd)
            return _codex_run(0, _fresh_jsonl("DIFFERENT-sid", "happened anyway"), "")
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertTrue(result.ok)
        self.assertEqual(len(calls), 1, "must NOT re-run; just adopt the new id")
        self.assertIsNotNone(result.warning)
        self.assertIn("DIFFERENT-sid", result.warning)
        self.assertIn("expected-sid", result.warning)
        sid, _ = codex_council.load_session("architect")
        self.assertEqual(sid, "DIFFERENT-sid")

    async def test_resume_mismatch_without_message_persists_adopted_id(self):
        """When resume runs the turn on a DIFFERENT thread and that turn
        produced no agent_message, the adopted id must still be persisted:
        the stored id is proven wrong, and leaving it in place would repeat
        the silent-spawn footgun on every subsequent call."""
        role = _make_role("architect", "Architect")
        codex_council.save_session("architect", "expected-sid")
        stdout = json.dumps(
            {"type": "thread.started", "thread_id": "DIFFERENT-sid"}
        )
        async def fake_subproc(cmd, prompt, role_id=""):
            return _codex_run(0, stdout, "")
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertFalse(result.ok)
        self.assertIn("no agent_message", result.error)
        self.assertIsNotNone(result.warning)
        sid, _ = codex_council.load_session("architect")
        self.assertEqual(sid, "DIFFERENT-sid")

    async def test_resume_with_no_thread_started_event_keeps_stored_id(self):
        """Codex may omit thread.started on resume; treat as a normal resume."""
        role = _make_role("architect", "Architect")
        codex_council.save_session("architect", "kept-sid")
        async def fake_subproc(cmd, prompt, role_id=""):
            return _codex_run(0, _resume_jsonl_no_thread_event("resumed text"), "")
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertTrue(result.ok)
        self.assertEqual(codex_council.load_session("architect")[0], "kept-sid")
        self.assertEqual(result.text, "resumed text")

    async def test_fresh_path_msg_without_thread_started_is_still_ok(self):
        """Regression: a fresh codex call that emits agent_message but no
        thread.started must not drop the reply. Continuity is lost (we
        can't resume), but the user still gets the answer for this turn."""
        role = _make_role("architect", "Architect")
        # No saved session, so this goes the fresh path; stdout has no thread.started.
        async def fake_subproc(cmd, prompt, role_id=""):
            return _codex_run(0, _resume_jsonl_no_thread_event("answer without id"), "")
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "answer without id")
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
        async def fake_subproc(cmd, prompt, role_id=""):
            return _codex_run(1, stdout, "")
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertFalse(result.ok)
        self.assertFalse((result.error or "").startswith("[retriable:"))

    async def test_fresh_status529_tagged_retriable_5xx(self):
        role = _make_role("architect", "Architect")
        stdout = _status_turn_failed_stdout(529, "server overloaded")
        async def fake_subproc(cmd, prompt, role_id=""):
            return _codex_run(1, stdout, "")
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("[retriable:5xx]"))

    async def test_run_role_attempts_retries_on_structured_5xx(self):
        role = _make_role("architect", "Architect")
        stdout = _status_turn_failed_stdout(503, "temporarily down")
        async def fake_subproc(cmd, prompt, role_id=""):
            return _codex_run(1, stdout, "")
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
        async def fake_subproc(cmd, prompt, role_id=""):
            return _codex_run(1, stdout, "")
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
        async def fake_subproc(cmd, prompt, role_id=""):
            return _codex_run(1, stdout, "")
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
        async def fake_subproc(cmd, prompt, role_id=""):
            return _codex_run(1, "", "HTTP 429 Too Many Requests while resuming; thread not found in cache")
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
                    elapsed_seconds=0.1, attempts=attempt, retriable=True,
                )
            return codex_council.RoleResult(
                role=r, ok=True, text="finally", elapsed_seconds=0.2,
                attempts=attempt,
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
                elapsed_seconds=0.1, attempts=attempt, retriable=True,
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


class RunCouncilAsyncTests(unittest.IsolatedAsyncioTestCase):
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
        with patch.object(codex_council, "_open_private_lock",
                          return_value=fake_file):
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
                elapsed_seconds=0.1, attempts=1,
            )
        with patch.object(codex_council, "_run_role_attempts", side_effect=fake_role):
            results = await codex_council.run_council(
                self._roles("tester", "architect"), "body", max_parallel=6,
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
                max_parallel=6,
            )
        self.assertEqual(len(results), 3)
        crashed = [r for r in results if r.role.id == "security"][0]
        self.assertFalse(crashed.ok)
        self.assertIn("orchestrator-exception", crashed.error)
        self.assertIn("RuntimeError", crashed.error)
        siblings = [r for r in results if r.role.id != "security"]
        self.assertTrue(all(r.ok for r in siblings))

    async def test_roles_run_through_fanout(self):
        """A caller-supplied Role flows through run_council."""
        custom = codex_council.Role(
            "ml-fairness", "ML Fairness", "audit bias thoroughly."
        )

        async def fake_role(role, prompt):
            return codex_council.RoleResult(
                role=role, ok=True, text=f"reply-{role.id}",
                elapsed_seconds=0.1, attempts=1,
            )
        with patch.object(codex_council, "_run_role_attempts", side_effect=fake_role):
            results = await codex_council.run_council(
                [custom], "body", max_parallel=6)
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

    async def test_lock_probe_backoff_grows_and_is_capped(self):
        """A same-role continuity-lock waiter must not poll at a fixed 0.1s
        forever: the other council holds the lock with no run-level deadline,
        so the probe interval doubles up to LOCK_PROBE_MAX_BACKOFF_SECS."""
        role = self._roles("architect")[0]
        held_box = [codex_council._try_role_state_lock(role.id)]
        self.assertIsNotNone(held_box[0])
        probe_sleeps = []
        real_sleep = asyncio.sleep

        async def recording_sleep(delay, *args, **kwargs):
            # The heartbeat and the status tick sleep far longer; only the
            # short waiter probes are the subject here.
            if delay <= codex_council.LOCK_PROBE_MAX_BACKOFF_SECS:
                probe_sleeps.append(delay)
                if len(probe_sleeps) >= 7 and held_box[0] is not None:
                    codex_council._release_role_state_lock(held_box[0])
                    held_box[0] = None
            await real_sleep(0)

        async def fake_role(r, prompt):
            return codex_council.RoleResult(
                role=r, ok=True, text="ok", elapsed_seconds=0.01,
            )

        try:
            with contextlib.redirect_stderr(io.StringIO()):
                with patch.object(codex_council.asyncio, "sleep", recording_sleep):
                    with patch.object(
                        codex_council, "_run_role_attempts", side_effect=fake_role
                    ):
                        results = await asyncio.wait_for(
                            codex_council.run_council(
                                [role], "body", max_parallel=1
                            ),
                            timeout=10,
                        )
        finally:
            if held_box[0] is not None:
                codex_council._release_role_state_lock(held_box[0])

        self.assertTrue(results[0].ok)
        self.assertEqual(
            probe_sleeps[:7], [0.1, 0.2, 0.4, 0.8, 1.6, 2.0, 2.0]
        )

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

        async def fake_subproc(cmd, prompt, role_id=""):
            active["count"] += 1
            active["max"] = max(active["max"], active["count"])
            if active["count"] == 2:
                both_started.set()
            try:
                await release.wait()
                return _codex_run(0, _fresh_jsonl(f"sid-{len(prompt)}", "ok"), "")
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
    the E2E happy-path test). No replies_dir is passed, so these lines carry
    no ` reply=` suffix; reply files are covered in test_replies_and_overrides."""

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
                    max_parallel=6,
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

    class _HeartbeatWatch(io.StringIO):
        """A stderr buffer that releases the fake roles once it holds
        `wanted` heartbeat lines: the roles end on the heartbeats they
        wait for, never on a wall-clock guess."""

        def __init__(self, wanted):
            super().__init__()
            self.wanted = wanted
            self.seen = asyncio.Event()

        def write(self, text):
            written = super().write(text)
            if self.getvalue().count("still running after") >= self.wanted:
                self.seen.set()
            return written

    def _waiting_role(self, watch):
        async def fake_role(role, prompt):
            await watch.seen.wait()
            return codex_council.RoleResult(
                role=role, ok=True, text="ok", elapsed_seconds=0.1,
            )
        return fake_role

    async def test_long_run_emits_periodic_status_heartbeat(self):
        buf = self._HeartbeatWatch(wanted=2)
        with patch.object(codex_council, "_run_role_attempts",
                          side_effect=self._waiting_role(buf)):
            with patch.object(codex_council, "PROGRESS_HEARTBEAT_SECS", 0.01):
                with contextlib.redirect_stderr(buf):
                    await codex_council.run_council(
                        self._roles("architect", "security"),
                        "body",
                        max_parallel=1,
                    )

        heartbeats = [
            line for line in buf.getvalue().splitlines()
            if "still running after" in line
        ]
        self.assertGreaterEqual(len(heartbeats), 2)
        self.assertIn("completed=0/2", heartbeats[0])
        self.assertRegex(heartbeats[0], r"active=1 \(architect quiet=\d+s\)")
        self.assertIn("queued=1", heartbeats[0])
        self.assertIn("watchdog=1800s", heartbeats[0])
        self.assertIn("version=", heartbeats[0])

    async def test_heartbeat_reports_watchdog_disabled_when_env_is_zero(self):
        buf = self._HeartbeatWatch(wanted=1)
        os.environ[codex_council.STALL_SECS_ENV] = "0"
        try:
            with patch.object(codex_council, "_run_role_attempts",
                              side_effect=self._waiting_role(buf)):
                with patch.object(codex_council, "PROGRESS_HEARTBEAT_SECS", 0.01):
                    with contextlib.redirect_stderr(buf):
                        await codex_council.run_council(
                            self._roles("architect"), "body", max_parallel=1,
                        )
        finally:
            os.environ.pop(codex_council.STALL_SECS_ENV, None)

        heartbeats = [
            line for line in buf.getvalue().splitlines()
            if "still running after" in line
        ]
        self.assertGreaterEqual(len(heartbeats), 1)
        self.assertIn("watchdog=disabled", heartbeats[0])


# ---------- roles JSON parsing (--roles-file contents) ----------

class ParseRolesJsonTests(unittest.TestCase):
    def test_single_role_happy_path(self):
        instruction = _valid_instruction("Audit for bias")
        raw = json.dumps([_role_json("ml-fairness", "ML Fairness", instruction)])
        roles = codex_council._parse_roles_json(raw)
        self.assertEqual(len(roles), 1)
        self.assertEqual(roles[0].id, "ml-fairness")
        self.assertEqual(roles[0].label, "ML Fairness")
        self.assertEqual(roles[0].instruction, instruction)

    def test_multiple_roles_preserve_order(self):
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
        """An instruction is a list of sentences, never one string."""
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

    def test_instruction_without_scope_boilerplate_is_preserved(self):
        instruction = "Reproduce the reported data mismatch."
        raw = json.dumps([_role_json("x", "L", instruction)])
        role = codex_council._parse_roles_json(raw)[0]
        self.assertEqual(role.instruction, instruction)
        prompt = codex_council._compose_prompt(role, "Current evidence")
        self.assertIn("If nothing material falls in your lens", prompt)
        self.assertEqual(prompt.count(instruction), 2)

    def test_runner_supplies_cadence_without_requiring_authored_prose(self):
        raw = json.dumps([_role_json("x", "L", "Compare the alternatives.")])
        role = codex_council._parse_roles_json(raw)[0]
        self.assertNotIn("Thoroughness beats speed.", role.instruction)
        prompt = codex_council._compose_prompt(role, "Current evidence")
        self.assertEqual(prompt.count("Thoroughness beats speed."), 1)

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

    def test_id_with_trailing_newline_raises(self):
        """`$` would accept "architect\\n" (it matches before a final newline),
        injecting a newline into state filenames and report/progress lines;
        `\\Z` rejects it."""
        raw = json.dumps([_role_json("architect\n", "L")])
        _assert_usage_exit(
            self, lambda: codex_council._parse_roles_json(raw),
            expect_in_stderr="must match",
        )

    def test_id_with_embedded_newline_raises(self):
        raw = json.dumps([_role_json("arch\nitect", "L")])
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
    """Unknown keys ('"_": ""', '"instruction_note": ""') expose schema
    mistakes; they must be rejected with a rewrite-the-whole-file recovery
    message, never silently
    accepted."""

    def _entry_with(self, extra_keys):
        entry = _role_json("alpha", "A")
        entry.update(extra_keys)
        return json.dumps([entry])

    def test_an_unknown_underscore_key_is_rejected(self):
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
            expect_in_stderr="rewrite the entire file passed to --roles-file",
        )

    def test_unknown_key_reported_before_missing_field(self):
        """Freeze diagnostic precedence: an object with both an unknown key
        and a missing required field reports the unknown key, because the
        recovery (full rewrite) covers both defects."""
        raw = json.dumps([{"id": "x", "instruction": _valid_instruction("i"),
                           "_": ""}])
        _assert_usage_exit(
            self, lambda: codex_council._parse_roles_json(raw),
            expect_in_stderr="unknown field(s) '_'",
        )

    def test_required_keys_alone_still_accepted(self):
        roles = codex_council._parse_roles_json(json.dumps([_role_json("a", "A")]))
        self.assertEqual(roles[0].id, "a")


class InstructionListFormTests(unittest.TestCase):
    """List-form instruction: nonempty sentence-sized strings the script
    joins into the single paragraph Codex sees."""

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

    def test_task_sentences_need_no_scope_boilerplate(self):
        items = ["Investigate the data.", "Explain the uncertainty."]
        roles = self._roles(items)
        self.assertEqual(roles[0].instruction, " ".join(items))

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
            expect_in_stderr="rewrite the entire file passed to --roles-file",
        )

    def test_string_form_rejected_with_array_recovery(self):
        raw = json.dumps([{"id": "x", "label": "L", "instruction": "one two"}])
        _assert_usage_exit(
            self, lambda: codex_council._parse_roles_json(raw),
            expect_in_stderr="rewrite the entire file passed to --roles-file",
        )


class ParseRolesPanelTests(unittest.TestCase):
    """A parsed panel keeps its input order and has no role-count cap."""

    def test_panel_keeps_input_order(self):
        raw = json.dumps([
            {"id": "data-pipeline", "label": "Data",
             "instruction": [_valid_instruction("review pipeline")]},
            {"id": "ml-fairness", "label": "Fair",
             "instruction": [_valid_instruction("audit bias")]},
        ])
        roles = codex_council._parse_roles_json(raw)
        self.assertEqual([r.id for r in roles], ["data-pipeline", "ml-fairness"])

    def test_large_panel_has_no_role_count_cap(self):
        entries = [
            {"id": f"role-{i}", "label": f"R{i}",
             "instruction": [_valid_instruction("x")]}
            for i in range(100)
        ]
        roles = codex_council._parse_roles_json(json.dumps(entries))
        self.assertEqual(len(roles), 100)


class ProjectRootCacheTests(unittest.TestCase):
    def setUp(self):
        council_common._project_root_cache.clear()

    def tearDown(self):
        council_common._project_root_cache.clear()

    def test_only_one_git_call_across_many_lookups(self):
        calls = {"count": 0}
        def fake_run(*args, **_kwargs):
            calls["count"] += 1
            from subprocess import CompletedProcess
            return CompletedProcess(args=args[0], returncode=0, stdout="/x\n", stderr="")
        with patch.object(council_common.subprocess, "run", side_effect=fake_run):
            for _ in range(5):
                council_common._project_root()
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


class PrivateStatPolicyTests(unittest.TestCase):
    """The one private-path policy behind the staging and follow gate, the
    replies directory, and the snapshot reader, checked in a fixed order:
    symlink, file type, owner, then group/other permission bits."""

    @staticmethod
    def _st(kind, mode, uid=None):
        uid = os.geteuid() if uid is None else uid
        return os.stat_result((kind | mode, 0, 0, 1, uid, 0, 0, 0, 0, 0))

    def test_each_rule_and_its_fragment(self):
        other = os.geteuid() + 1
        for st, directory, expected in (
            (self._st(stat.S_IFDIR, 0o700), True, None),
            (self._st(stat.S_IFREG, 0o600), False, None),
            (self._st(stat.S_IFREG, 0o400), False, None),
            (self._st(stat.S_IFLNK, 0o700), True, ("symlink", "is a symlink")),
            (self._st(stat.S_IFLNK, 0o600), False,
             ("symlink", "is a symlink")),
            (self._st(stat.S_IFREG, 0o700), True,
             ("type", "is not a directory")),
            (self._st(stat.S_IFDIR, 0o600), False,
             ("type", "is not a regular file")),
            (self._st(stat.S_IFIFO, 0o600), False,
             ("type", "is not a regular file")),
            (self._st(stat.S_IFDIR, 0o755, other), True,
             ("owner", f"is owned by uid {other}")),
            (self._st(stat.S_IFDIR, 0o750), True, ("mode", "is mode 0750")),
            (self._st(stat.S_IFREG, 0o620), False, ("mode", "is mode 0620")),
            (self._st(stat.S_IFREG, 0o604), False, ("mode", "is mode 0604")),
        ):
            with self.subTest(mode=oct(st.st_mode), directory=directory):
                self.assertEqual(
                    council_common._private_stat_problem(
                        st, directory=directory),
                    expected)

    def test_replies_dir_reports_the_shared_fragments(self):
        with tempfile.TemporaryDirectory() as run_dir:
            replies = os.path.join(run_dir, codex_council.REPLIES_SUBDIR)
            os.mkdir(replies, 0o750)
            os.chmod(replies, 0o750)
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                self.assertIsNone(codex_council._prepare_replies_dir(run_dir))
        self.assertIn(f"{replies!r} is mode 0750, not private; the final "
                      "report is unaffected", buf.getvalue())


class CheckStagingDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.chmod(self.tmp.name, 0o700)
        # Preflight requires codex on PATH; keep these tests hermetic
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

    def test_valid_but_uncaptured_staging_dir_is_refused(self):
        # The happy path needs --prepare (tests/test_codex_council_cli.py).
        self._write_valid_roles()
        self._write_context()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _assert_usage_exit(
                self, lambda: codex_council._check_staging_dir(self.tmp.name),
                expect_in_stderr="no completed capture")
        self.assertNotIn("staging OK", buf.getvalue())

    def test_missing_context_mentions_staging_hint(self):
        self._write_valid_roles()
        _assert_usage_exit(
            self,
            lambda: codex_council._check_staging_dir(self.tmp.name),
            expect_in_stderr="Staging hint",
        )

    def test_empty_context_is_rejected_as_usage_error(self):
        """Exit 2 like every other staging defect; exit 1 stays for runtime
        outcomes (no successful role, undelivered output, or incomplete
        required coverage or evidence)."""
        self._write_valid_roles()
        self._write_context("   \n")
        _assert_usage_exit(
            self,
            lambda: codex_council._check_staging_dir(self.tmp.name),
            expect_in_stderr="empty or whitespace-only",
        )

    def test_missing_codex_fails_preflight(self):
        """'staging OK' while the codex binary is missing defers the
        failure to a background launch whose error lands only in
        err.log."""
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
        """A hint like 'Create it with mktemp -d' is satisfiable by
        chmod/mkdir on the same predictable path; the recovery must
        demand abandoning the dir for a NEW mktemp one."""
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
        self.assertIn("Write request.json, roles.json, and context.md in the NEW "
                      "directory, run --prepare and then --discover there", err)

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

    @unittest.skipIf(
        os.geteuid() == 0,
        "root bypasses the 0o000 permission barrier (CAP_DAC_OVERRIDE), "
        "so lstat never fails with EACCES",
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
        """A missing-path error must demand a fresh mktemp -d, never a
        mkdir of the same predictable path (which would defeat the
        private-staging guarantee)."""
        missing = os.path.join(self.tmp.name, "missing")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit) as ctx:
                codex_council._check_staging_dir(missing)
        self.assertEqual(ctx.exception.code, 2)
        err = buf.getvalue()
        self.assertIn("does not exist", err)
        self.assertIn("do not mkdir it", err)

    def test_a_directory_that_already_launched_is_refused(self):
        """Each launch needs its own directory: a directory holding launch
        output may hold a running council, so the pre-flight refuses it
        before anything can truncate out.md or err.log. Any launch output
        counts, a dangling symlink included, and the refusal comes before
        the staged files are even parsed."""
        self._write_valid_roles()
        self._write_context()

        def make_file(path):
            with open(path, "w", encoding="utf-8"):
                pass

        makers = {
            "out.md": make_file,
            "err.log": make_file,
            "replies": lambda path: os.mkdir(path, 0o700),
            "dangling out.md": lambda path: os.symlink(
                os.path.join(self.tmp.name, "missing"), path),
        }
        for label, make in makers.items():
            name = label.split()[-1]
            path = os.path.join(self.tmp.name, name)
            with self.subTest(output=label):
                make(path)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    err = _assert_usage_exit(
                        self,
                        lambda: codex_council._check_staging_dir(
                            self.tmp.name),
                        expect_in_stderr="already holds a council launch",
                    )
                self.assertIn(f"({name} present)", err)
                self.assertIn("every launch needs its own directory", err)
                self.assertIn("Run `mktemp -d` again. Write request.json, "
                              "roles.json, and context.md in the NEW "
                              "directory, run --prepare", err)
                self.assertEqual(out.getvalue(), "")
                (os.rmdir if name == "replies" else os.remove)(path)
        # Refused before parsing: a broken roles file never gets that far.
        with open(os.path.join(self.tmp.name, "roles.json"), "w",
                  encoding="utf-8") as f:
            f.write("{not json")
        make_file(os.path.join(self.tmp.name, "err.log"))
        _assert_usage_exit(
            self, lambda: codex_council._check_staging_dir(self.tmp.name),
            expect_in_stderr="already holds a council launch")

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
        """Exit 2 like every other staging defect; exit 1 stays for runtime
        outcomes (no successful role, undelivered output, or incomplete
        required coverage or evidence)."""
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

    def test_staged_launch_context_message_starts_over_elsewhere(self):
        """At a staged launch the directory already holds this launch, so
        neither content defect asks for a preflight re-run there."""
        for content, fix in ((b"   \n", "decision-complete working context"),
                             (b"\xff\xfe bad", "as UTF-8 text")):
            with self.subTest(fix=fix):
                path = self._path()
                with open(path, "wb") as f:
                    f.write(content)
                err = _assert_usage_exit(
                    self,
                    lambda path=path: codex_council._read_context_file(
                        path, staged_launch=True),
                    expect_in_stderr=council_common.STAGED_LAUNCH_RESTART,
                )
                self.assertIn("Write the new context.md ", err)
                self.assertIn(fix, err)
                self.assertNotIn("re-run --check-staging-dir", err)

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

    def test_help_documents_launch_privacy_replies_and_follow(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit):
                codex_council._parse_args(["--help"])
        help_text = " ".join(out.getvalue().split())
        self.assertIn("must be private (0700, user-owned, non-symlink)", help_text)
        self.assertIn("Run --prepare RUNDIR first", help_text)
        self.assertNotIn("--roles-file", help_text)
        self.assertNotIn("--context-file", help_text)
        self.assertIn("mktemp -d", help_text)
        self.assertIn("--skill-contract", help_text)
        self.assertIn("replies/", help_text)
        self.assertIn("--follow", help_text)

    def test_staged_inputs_parse_only_for_the_supervised_child(self):
        args = codex_council._parse_args([
            "--roles-file", "x.json", "--context-file", "context.md",
            "--supervisor-lock-fd", "3",
        ])
        self.assertEqual((args.roles_file, args.context_file), ("x.json", "context.md"))
        for argv in (["--roles-file", "x.json"], ["--context-file", "context.md"],
                     ["--roles-file", "x.json", "--context-file", "context.md"]):
            buf = io.StringIO()
            with self.subTest(argv=argv), contextlib.redirect_stderr(buf), \
                    self.assertRaises(SystemExit) as ctx:
                codex_council._parse_args(argv)
            self.assertEqual(ctx.exception.code, 2)
            self.assertIn("there is no direct launch", buf.getvalue())

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



class _FakeEofStream:
    async def read(self, n):
        return b""


class _FakeStdin:
    def write(self, data):
        pass

    async def drain(self):
        pass

    def close(self):
        pass


def _fake_proc(wait_exc):
    class FakeProc:
        pid = 424242
        returncode = None
        stdout = _FakeEofStream()
        stderr = _FakeEofStream()
        stdin = _FakeStdin()

        async def wait(self):
            raise wait_exc

    return FakeProc()


class RunCodexSubprocessTests(unittest.IsolatedAsyncioTestCase):
    """Spawn/encode/reap behavior of the real _run_codex_subprocess."""

    async def test_encode_happens_before_spawn(self):
        """A prompt UTF-8 cannot encode (a lone surrogate) must fail BEFORE the
        child is spawned, so no codex process is left blocked on stdin."""
        create_mock = AsyncMock()
        with patch.object(
            codex_council.asyncio, "create_subprocess_exec", create_mock
        ):
            with self.assertRaises(UnicodeEncodeError):
                await codex_council._run_codex_subprocess(
                    ["codex"], "bad \ud800 prompt"
                )
        create_mock.assert_not_called()

    async def test_reaps_child_on_non_cancel_error(self):
        """proc.wait() raising anything (not just CancelledError) after the
        child exists must tear down the process group, not leak it."""
        proc = _fake_proc(RuntimeError("boom"))
        terminate_mock = AsyncMock()
        with patch.object(
            codex_council.asyncio, "create_subprocess_exec",
            AsyncMock(return_value=proc),
        ), patch.object(
            codex_council.os, "getpgid", return_value=proc.pid
        ), patch.object(
            codex_council, "_terminate_process_group", terminate_mock
        ):
            with self.assertRaises(RuntimeError):
                await codex_council._run_codex_subprocess(["codex"], "ok prompt")
        terminate_mock.assert_awaited_once()

    async def test_cancellation_still_reaps_child(self):
        """The cancellation reap path converges on the same single
        termination owner."""
        proc = _fake_proc(asyncio.CancelledError())
        terminate_mock = AsyncMock()
        with patch.object(
            codex_council.asyncio, "create_subprocess_exec",
            AsyncMock(return_value=proc),
        ), patch.object(
            codex_council.os, "getpgid", return_value=proc.pid
        ), patch.object(
            codex_council, "_terminate_process_group", terminate_mock
        ):
            with self.assertRaises(asyncio.CancelledError):
                await codex_council._run_codex_subprocess(["codex"], "ok prompt")
        terminate_mock.assert_awaited_once()


class ForceUtf8StreamsTests(unittest.TestCase):
    def test_reconfigures_stdout_stderr_to_utf8(self):
        calls = []

        class FakeStream:
            def reconfigure(self, **kw):
                calls.append(kw)

        with patch.object(codex_council.sys, "stdout", FakeStream()), \
             patch.object(codex_council.sys, "stderr", FakeStream()):
            codex_council._force_utf8_streams()
        self.assertEqual(len(calls), 2)
        for kw in calls:
            self.assertEqual(kw.get("encoding"), "utf-8")
            self.assertEqual(kw.get("errors"), "replace")

    def test_tolerates_stream_without_reconfigure(self):
        class Bare:
            pass

        with patch.object(codex_council.sys, "stdout", Bare()), \
             patch.object(codex_council.sys, "stderr", Bare()):
            codex_council._force_utf8_streams()  # must not raise

    def test_swallows_reconfigure_errors(self):
        class Boom:
            def reconfigure(self, **kw):
                raise ValueError("nope")

        with patch.object(codex_council.sys, "stdout", Boom()), \
             patch.object(codex_council.sys, "stderr", Boom()):
            codex_council._force_utf8_streams()  # must not raise


class SignalLatchTests(unittest.IsolatedAsyncioTestCase):
    """The first SIGINT, SIGTERM, or SIGHUP is latched: it cancels the
    council once and sets the exit; a repeated one can neither cancel the
    cleanup it started nor change the exit code."""

    def setUp(self):
        for signum in codex_council.TERMINATION_SIGNALS:
            self.addCleanup(signal.signal, signum, signal.getsignal(signum))

    async def test_a_repeated_signal_never_disrupts_cleanup(self):
        running = asyncio.Event()
        cleanup = {"started": asyncio.Event(), "done": False, "cancels": 0}

        async def fake_run_council(roles, body, max_parallel,
                                   replies_dir=None):
            running.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleanup["cancels"] += 1
                cleanup["started"].set()
                # Teardown awaits: a second cancel() would land here.
                for _ in range(20):
                    try:
                        await asyncio.sleep(0.01)
                    except asyncio.CancelledError:
                        cleanup["cancels"] += 1
                        raise
                cleanup["done"] = True
                raise

        with patch.object(codex_council, "run_council", fake_run_council):
            task = asyncio.create_task(
                codex_council._run_council_with_signals([], "body", 1))
            await running.wait()
            os.kill(os.getpid(), signal.SIGTERM)
            await cleanup["started"].wait()
            for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
                os.kill(os.getpid(), signum)
                await asyncio.sleep(0.02)
            results, signum = await task
        self.assertIsNone(results)
        self.assertEqual(signum, signal.SIGTERM)
        self.assertTrue(cleanup["done"])
        self.assertEqual(cleanup["cancels"], 1)
        # After the latch, a late signal is ignored rather than fatal.
        for signum in codex_council.TERMINATION_SIGNALS:
            self.assertIs(signal.getsignal(signum), signal.SIG_IGN)

    async def test_no_signal_leaves_the_dispositions_alone(self):
        async def fake_run_council(roles, body, max_parallel,
                                   replies_dir=None):
            return ["result"]

        before = {s: signal.getsignal(s)
                  for s in codex_council.TERMINATION_SIGNALS}
        with patch.object(codex_council, "run_council", fake_run_council):
            outcome = await codex_council._run_council_with_signals(
                [], "body", 1)
        self.assertEqual(outcome, (["result"], None))
        self.assertNotIn(signal.SIG_IGN, [
            signal.getsignal(s) for s in codex_council.TERMINATION_SIGNALS
            if before[s] is not signal.SIG_IGN])


class NoRunLevelDeadlineTests(unittest.TestCase):
    """The council has no total elapsed-time or run-level deadline: a role
    may run indefinitely while its codex subprocess keeps producing output
    bytes. The only wall-clock mechanism is the per-subprocess
    OUTPUT-INACTIVITY watchdog, which is deliberately hand-rolled
    (asyncio.sleep + time.monotonic). The codex commands carry no
    timeout/retry config overrides (those live in the user's
    provider-scoped codex config)."""

    def test_commands_have_safety_but_no_model_or_deadline_overrides(self):
        for cmd in (codex_council._fresh_cmd("/r"),
                    codex_council._resume_cmd("/r", "sid")):
            council_testlib.assert_no_model_overrides(self, cmd)
            self.assertEqual(council_testlib.config_values(cmd),
                             {"approval_policy": "never"})
            self.assertEqual(cmd[cmd.index("--sandbox") + 1], "read-only")
            self.assertFalse(any("timeout" in a or "retries" in a for a in cmd))

    def test_source_uses_no_run_level_timeout_primitive(self):
        """No run-level deadline, by design: pin the absence of any named
        timeout primitive so adding one is a conscious choice (this test
        fails) rather than a silent regression. Scans the executable code of
        every runner module (the entry script and its sibling modules) —
        comment and string/docstring spans are masked out, since the runner
        is deliberately comment-heavy about the deadline it does NOT have.
        The output-inactivity watchdog must stay hand-rolled (an
        asyncio.sleep loop over time.monotonic): the named APIs below would
        impose a deadline on the subprocess await itself, which is exactly
        what the design forbids. Bounded metadata, process-table,
        post-exit-drain and teardown operations are allowed (the table
        below); none of them imposes a run-level deadline."""
        import io as _io
        import re as _re
        import tokenize as _tokenize
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
        # Bounds that never limit a running role: the post-exit drain and
        # the exit waits of _process_exit (codex has already exited or been
        # killed), and council_liveness's bounded ps call.
        allowed = {("council_common.py", "timeout="): 1,
                   ("codex_council.py", "timeout="): 4,
                   ("council_liveness.py", "timeout="): 1,
                   ("council_workspace.py", "timeout="): 1}
        modules = sorted(name for name in os.listdir(SCRIPTS_DIR)
                         if name.endswith(".py"))
        self.assertIn("codex_council.py", modules)
        for module in modules:
            with open(os.path.join(SCRIPTS_DIR, module),
                      encoding="utf-8") as f:
                src = f.read()
            # Mask COMMENT and STRING token spans (preserving byte offsets)
            # so the scan sees executable code only, not prose that names
            # these APIs.
            masked = list(src)
            offsets = [0]
            for line in _io.StringIO(src).readlines():
                offsets.append(offsets[-1] + len(line))
            for tok in _tokenize.generate_tokens(_io.StringIO(src).readline):
                if tok.type in (_tokenize.COMMENT, _tokenize.STRING):
                    start = offsets[tok.start[0] - 1] + tok.start[1]
                    end = offsets[tok.end[0] - 1] + tok.end[1]
                    for i in range(start, end):
                        if masked[i] != "\n":
                            masked[i] = " "
            code = "".join(masked)
            found = [
                name for name, pat in forbidden
                if len(_re.findall(pat, code)) > allowed.get((module, name), 0)
            ]
            with self.subTest(module=module):
                self.assertEqual(
                    found, [], f"unexpected timeout primitive(s): {found}")
        # The new timeout belongs only to metadata collection, not role work.
        import ast as _ast
        with open(os.path.join(SCRIPTS_DIR, "council_workspace.py"),
                  encoding="utf-8") as f:
            tree = _ast.parse(f.read())
        timeout_owners = []
        for node in tree.body:
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                for call in _ast.walk(node):
                    if isinstance(call, _ast.Call):
                        for kw in call.keywords:
                            if kw.arg == "timeout":
                                timeout_owners.append((node.name, _ast.literal_eval(kw.value)))
        self.assertEqual(timeout_owners, [("_git", 15)])
        self.assertIn("timeout=timeout",
                      inspect.getsource(council_common._project_root))

    def test_only_start_and_cancel_have_their_own_bounded_waits(self):
        """The two control deadlines are not a council's: --start waits
        (START_WAIT_SECS) only for its supervisor to start, and --cancel
        (CANCEL_GRACE_SECS, CANCEL_KILL_WAIT_SECS) only for the runner it
        signalled to end. Each constant is read in exactly its own
        command, never on the path that runs roles."""
        import ast as _ast
        self.assertLessEqual(codex_council.START_WAIT_SECS, 30)
        self.assertLessEqual(council_liveness.CANCEL_GRACE_SECS, 60)
        self.assertLessEqual(council_liveness.CANCEL_KILL_WAIT_SECS, 30)
        users = {}
        for module in (codex_council, council_liveness):
            tree = _ast.parse(inspect.getsource(module))
            for func in _ast.walk(tree):
                if not isinstance(func, (_ast.FunctionDef,
                                         _ast.AsyncFunctionDef)):
                    continue
                for node in _ast.walk(func):
                    if isinstance(node, _ast.Name) and node.id in (
                            "START_WAIT_SECS", "CANCEL_GRACE_SECS",
                            "CANCEL_KILL_WAIT_SECS"):
                        users.setdefault(node.id, set()).add(func.name)
        self.assertEqual(users, {
            "START_WAIT_SECS": {"_start_command"},
            "CANCEL_GRACE_SECS": {"cancel_command"},
            "CANCEL_KILL_WAIT_SECS": {"cancel_command"},
        })


# ---------- output-inactivity watchdog (env, flags, policy) ----------

class StallSecsEnvTests(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(
            os.environ, _env_without_session_key(), clear=True
        )
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)

    def test_unset_defaults_to_1800(self):
        self.assertEqual(codex_council._stall_secs(), 1800)
        self.assertEqual(codex_council.DEFAULT_STALL_SECS, 1800)

    def test_zero_disables(self):
        os.environ[codex_council.STALL_SECS_ENV] = "0"
        self.assertEqual(codex_council._stall_secs(), 0)

    def test_positive_integer_override(self):
        os.environ[codex_council.STALL_SECS_ENV] = "42"
        self.assertEqual(codex_council._stall_secs(), 42)

    def test_negative_is_a_usage_error(self):
        os.environ[codex_council.STALL_SECS_ENV] = "-5"
        _assert_usage_exit(
            self, codex_council._stall_secs,
            expect_in_stderr="must be a positive integer",
        )

    def test_nonnumeric_is_a_usage_error(self):
        os.environ[codex_council.STALL_SECS_ENV] = "soon"
        _assert_usage_exit(
            self, codex_council._stall_secs,
            expect_in_stderr="must be a positive integer",
        )

    def test_heartbeat_cadence_is_at_most_300s_for_every_watchdog(self):
        """The heartbeat never goes quieter than five minutes: enabled at
        the default, short, or very large, and disabled (0) alike. Read
        from the computed cadence, never by sleeping."""
        self.assertEqual(codex_council.PROGRESS_HEARTBEAT_SECS, 300)
        for stall_secs in (0, 1, 90, 600, codex_council.DEFAULT_STALL_SECS,
                           7200, 10**9):
            with self.subTest(stall_secs=stall_secs):
                cadence = codex_council._heartbeat_secs(stall_secs)
                self.assertLessEqual(cadence, 300)
                self.assertEqual(cadence, 300)
        # The cadence comes from the environment the launch reads.
        for raw in ("0", "86400"):
            with self.subTest(env=raw), \
                    patch.dict(os.environ, {codex_council.STALL_SECS_ENV: raw}):
                self.assertEqual(codex_council._heartbeat_secs(
                    codex_council._stall_secs()), 300)

    def test_watchdog_desc_rendering(self):
        self.assertEqual(codex_council._watchdog_desc(1800), "1800s")
        self.assertEqual(codex_council._watchdog_desc(0), "disabled")


class EventFlagScannerTests(unittest.TestCase):
    def _feed(self, scanner, text):
        scanner.feed(text.encode("utf-8"))

    def test_turn_completed_detected(self):
        s = codex_council._EventFlagScanner()
        self._feed(s, '{"type":"turn.completed","usage":{}}\n')
        self.assertTrue(s.turn_completed)
        self.assertFalse(s.unsafe_to_replay)

    def test_agent_message_and_reasoning_are_replay_safe(self):
        s = codex_council._EventFlagScanner()
        self._feed(
            s,
            '{"type":"item.completed","item":{"type":"agent_message","text":"x"}}\n'
            '{"type":"item.started","item":{"type":"reasoning"}}\n',
        )
        self.assertFalse(s.unsafe_to_replay)

    def test_codex_error_notices_are_replay_safe(self):
        """An `error` item is Codex's own notice (message only), such as the
        advisory that a resumed thread was recorded with another model; it
        is not tool work. A real tool item beside it still counts."""
        notice = ('{"type":"item.completed","item":{"type":"error",'
                  '"message":"This session was recorded with model `a` but '
                  'is resuming with `b`."}}\n')
        s = codex_council._EventFlagScanner()
        self._feed(s, notice)
        self.assertFalse(s.unsafe_to_replay)
        self._feed(
            s, '{"type":"item.started","item":{"type":"file_change"}}\n')
        self.assertTrue(s.unsafe_to_replay)

    def test_command_execution_started_is_unsafe(self):
        s = codex_council._EventFlagScanner()
        self._feed(
            s,
            '{"type":"item.started","item":{"type":"command_execution"}}\n',
        )
        self.assertTrue(s.unsafe_to_replay)

    def test_unknown_item_type_is_unsafe_conservatively(self):
        s = codex_council._EventFlagScanner()
        self._feed(
            s,
            '{"type":"item.completed","item":{"type":"future_gizmo_call"}}\n',
        )
        self.assertTrue(s.unsafe_to_replay)

    def test_flags_survive_chunk_boundaries_inside_a_line(self):
        line = '{"type":"item.started","item":{"type":"mcp_tool_call"}}\n'
        s = codex_council._EventFlagScanner()
        for i in range(0, len(line), 7):
            self._feed(s, line[i:i + 7])
        self.assertTrue(s.unsafe_to_replay)

    def test_finish_scans_unterminated_final_line(self):
        s = codex_council._EventFlagScanner()
        self._feed(s, '{"type":"turn.completed"}')  # no trailing newline
        self.assertFalse(s.turn_completed)
        s.finish()
        self.assertTrue(s.turn_completed)

    def test_blank_lines_are_ignored(self):
        s = codex_council._EventFlagScanner()
        self._feed(s, "\n  \n\r\n")
        s.finish()
        self.assertFalse(s.turn_completed)
        self.assertFalse(s.unsafe_to_replay)

    def test_a_line_that_is_not_a_json_object_is_unknown_work(self):
        """Such a line could hide a tool item, so it is never replay-safe;
        each kind is checked alone, the unterminated last line included."""
        undecodable_tool_item = (
            b'{"type":"item.started","item":{"type":"command_execution",'
            b'"exit_code":1' + b"0" * 5000 + b"}}")
        for line in (b"not json", b"[]", b"null", b"\xff",
                     b"[" * 100000 + b"]" * 100000, undecodable_tool_item):
            with self.subTest(line=line[:16]):
                s = codex_council._EventFlagScanner()
                s.feed(line + b"\n")
                self.assertTrue(s.unsafe_to_replay)
                self.assertFalse(s.turn_completed)
                s = codex_council._EventFlagScanner()
                s.feed(line)
                s.finish()
                self.assertTrue(s.unsafe_to_replay)

    def test_an_item_type_that_is_not_a_string_is_unknown_work(self):
        """A list, object or other non-string item type marks the attempt
        unsafe without raising out of feed(), and the lines after it are
        still scanned."""
        for item_type in ([], {}, 5, None, True):
            with self.subTest(item_type=item_type):
                event = {"type": "item.started", "item": {"type": item_type}}
                s = codex_council._EventFlagScanner()
                self._feed(s, json.dumps(event) + '\n{"type":"turn.completed"}\n')
                self.assertTrue(s.unsafe_to_replay)
                self.assertTrue(s.turn_completed)
                s = codex_council._EventFlagScanner()
                self._feed(s, json.dumps(event))
                s.finish()
                self.assertTrue(s.unsafe_to_replay)

    def test_any_scanning_surprise_is_unknown_work_and_never_raises(self):
        real = codex_council._EventFlagScanner._replay_safe

        def surprising(scanner, event):
            if event.get("surprise"):
                raise LookupError("an event shape nobody expected")
            return real(scanner, event)

        with patch.object(codex_council._EventFlagScanner, "_replay_safe",
                          surprising):
            s = codex_council._EventFlagScanner()
            self._feed(s, '{"surprise":1}\n{"type":"turn.completed"}\n')
            self.assertTrue(s.unsafe_to_replay)
            self.assertTrue(s.turn_completed)
            s = codex_council._EventFlagScanner()
            self._feed(s, '{"surprise":1}')
            s.finish()
            self.assertTrue(s.unsafe_to_replay)


def _stalled_run(stdout="", stderr="", turn_completed=False,
                 unsafe_to_replay=False):
    return codex_council.CodexRun(
        returncode=-15, stdout=stdout, stderr=stderr, stalled=True,
        turn_completed=turn_completed, unsafe_to_replay=unsafe_to_replay,
    )


class StallPolicyTests(unittest.IsolatedAsyncioTestCase):
    """Role-layer stall policy: the structured stall verdict is handled
    before any text classification."""

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

    async def test_wedged_after_completed_turn_is_success_with_warning(self):
        role = _make_role("architect", "Architect")
        stdout = _fresh_jsonl("wedged-sid", "the full reply") + "\n" + json.dumps(
            {"type": "turn.completed", "usage": {}}
        )
        async def fake_subproc(cmd, prompt, role_id=""):
            return _stalled_run(stdout=stdout, turn_completed=True)
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_attempts(role, "prompt")
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "the full reply")
        self.assertEqual(result.attempts, 1)  # never retried
        self.assertIn("wedged after completing its turn", result.warning)
        sid, _ = codex_council.load_session("architect")
        self.assertEqual(sid, "wedged-sid")

    async def test_replay_safe_stall_is_retriable_then_terminal(self):
        role = _make_role("architect", "Architect")
        calls = {"count": 0}
        async def fake_subproc(cmd, prompt, role_id=""):
            calls["count"] += 1
            return _stalled_run()
        with patch.object(codex_council, "RETRY_BACKOFF_SECS", 0), \
             patch.object(codex_council.asyncio, "sleep", AsyncMock(return_value=None)), \
             patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_attempts(role, "prompt")
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("[retriable:stall]"))
        self.assertIn("no side-effect-capable work was observed", result.error)
        # The budget is spent: the final error never promises a retry.
        self.assertNotIn("retrying", result.error)
        self.assertEqual(result.attempts, codex_council.MAX_RETRY_ATTEMPTS)
        self.assertEqual(calls["count"], codex_council.MAX_RETRY_ATTEMPTS)

    async def test_unsafe_stall_is_terminal_after_one_attempt(self):
        role = _make_role("architect", "Architect")
        calls = {"count": 0}
        stdout = json.dumps(
            {"type": "item.started", "item": {"type": "command_execution"}}
        )
        async def fake_subproc(cmd, prompt, role_id=""):
            calls["count"] += 1
            return _stalled_run(stdout=stdout, unsafe_to_replay=True)
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_attempts(role, "prompt")
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("[stall]"))
        self.assertIn("tool work may have begun", result.error)
        self.assertIn("re-invoke the role manually", result.error)
        self.assertEqual(calls["count"], 1)

    async def test_unsafe_stall_quotes_incomplete_message_without_promoting(self):
        role = _make_role("architect", "Architect")
        stdout = "\n".join([
            json.dumps({"type": "item.started",
                        "item": {"type": "command_execution"}}),
            json.dumps({"type": "item.completed",
                        "item": {"type": "agent_message", "text": "partial"}}),
        ])
        async def fake_subproc(cmd, prompt, role_id=""):
            return _stalled_run(stdout=stdout, unsafe_to_replay=True)
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertFalse(result.ok)  # no turn.completed -> never auto-promoted
        self.assertIn("partial", result.error)

    async def test_stalled_resume_with_stale_looking_stderr_keeps_state(self):
        """Partial stale/auth text in a killed run's stderr must not clear
        resume state: the structured stall verdict outranks text sniffing."""
        role = _make_role("architect", "Architect")
        codex_council.save_session("architect", "live-sid")
        async def fake_subproc(cmd, prompt, role_id=""):
            return _stalled_run(stderr="no rollout found for thread id live-sid")
        with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
            result = await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("[retriable:stall]"))
        sid, _ = codex_council.load_session("architect")
        self.assertEqual(sid, "live-sid")


class StallWatchdogIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Real subprocesses under a tiny CODEX_COUNCIL_STALL_SECS: the hand-
    rolled watchdog kills silent children, spares chatty ones, and derives
    the event flags from the buffered JSONL."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = _env_without_session_key()
        env[codex_council.STALL_SECS_ENV] = "1"
        self.env_patcher = patch.dict(os.environ, env, clear=True)
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)

    def _script(self, name, body):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return [sys.executable, path]

    _PREAMBLE = (
        "import json, sys, time\n"
        "def emit(obj):\n"
        "    sys.stdout.write(json.dumps(obj) + '\\n')\n"
        "    sys.stdout.flush()\n"
    )

    async def test_silent_hang_is_stalled_and_replay_safe(self):
        cmd = self._script("hang.py", self._PREAMBLE + "time.sleep(300)\n")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            run = await codex_council._run_codex_subprocess(cmd, "p", role_id="r1")
        self.assertTrue(run.stalled)
        self.assertFalse(run.turn_completed)
        self.assertFalse(run.unsafe_to_replay)
        err = buf.getvalue()
        self.assertIn("[codex-council:r1] stall threshold reached", err)
        self.assertRegex(err, r"quiet=\d+s, watchdog=1s\); terminating attempt")

    async def test_completed_turn_then_hang_flags_turn_completed(self):
        cmd = self._script("wedge.py", self._PREAMBLE + (
            "emit({'type': 'thread.started', 'thread_id': 'sid-w'})\n"
            "emit({'type': 'item.completed',"
            " 'item': {'type': 'agent_message', 'text': 'done reply'}})\n"
            "emit({'type': 'turn.completed', 'usage': {}})\n"
            "time.sleep(300)\n"
        ))
        with contextlib.redirect_stderr(io.StringIO()):
            run = await codex_council._run_codex_subprocess(cmd, "p")
        self.assertTrue(run.stalled)
        self.assertTrue(run.turn_completed)
        self.assertEqual(
            codex_council.extract_final_message(run.stdout), "done reply")

    async def test_resume_advisory_then_hang_stays_replay_safe(self):
        """A resume onto another model makes Codex print an `error` item
        advisory first; a stall after it is still retriable, never a
        terminal "tool work may have begun"."""
        cmd = self._script("advisory.py", self._PREAMBLE + (
            "emit({'type': 'thread.started', 'thread_id': 'sid-a'})\n"
            "emit({'type': 'turn.started'})\n"
            "emit({'type': 'item.completed', 'item': {'type': 'error',"
            " 'message': 'This session was recorded with model `a` but is"
            " resuming with `b`.'}})\n"
            "time.sleep(300)\n"
        ))
        with contextlib.redirect_stderr(io.StringIO()):
            run = await codex_council._run_codex_subprocess(cmd, "p")
        self.assertTrue(run.stalled)
        self.assertFalse(run.unsafe_to_replay)
        result = codex_council._stalled_role_result(
            _make_role("architect", "Architect"), run, "sid-a", attempt=1,
            started=0.0)
        self.assertTrue(result.error.startswith("[retriable:stall]"),
                        result.error)
        # The retry decision is carried as data, not read from the tag.
        self.assertTrue(result.retriable)

    async def test_tool_start_then_hang_is_unsafe_to_replay(self):
        cmd = self._script("tool.py", self._PREAMBLE + (
            "emit({'type': 'thread.started', 'thread_id': 'sid-t'})\n"
            "emit({'type': 'item.started',"
            " 'item': {'type': 'command_execution', 'command': 'sleep'}})\n"
            "time.sleep(300)\n"
        ))
        with contextlib.redirect_stderr(io.StringIO()):
            run = await codex_council._run_codex_subprocess(cmd, "p")
        self.assertTrue(run.stalled)
        self.assertFalse(run.turn_completed)
        self.assertTrue(run.unsafe_to_replay)

    async def test_slow_but_chatty_child_never_trips(self):
        # Emits a byte every 0.3s for ~2.4s — far past the 1s threshold in
        # quiet-time terms only if bytes stopped; they don't, so no stall.
        cmd = self._script("chatty.py", self._PREAMBLE + (
            "for _ in range(8):\n"
            "    sys.stdout.write('\\n'); sys.stdout.flush()\n"
            "    time.sleep(0.3)\n"
            "emit({'type': 'thread.started', 'thread_id': 'sid-c'})\n"
            "emit({'type': 'item.completed',"
            " 'item': {'type': 'agent_message', 'text': 'chatty ok'}})\n"
        ))
        run = await codex_council._run_codex_subprocess(cmd, "p")
        self.assertFalse(run.stalled)
        self.assertEqual(run.returncode, 0)
        self.assertEqual(
            codex_council.extract_final_message(run.stdout), "chatty ok")

    async def test_zero_disables_the_watchdog(self):
        os.environ[codex_council.STALL_SECS_ENV] = "0"
        cmd = self._script("hang0.py", self._PREAMBLE + "time.sleep(300)\n")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            task = asyncio.create_task(
                codex_council._run_codex_subprocess(cmd, "p")
            )
            # Well past the smallest possible threshold: still running.
            await asyncio.sleep(1.3)
            self.assertFalse(task.done())
            # Explicit teardown: cancellation reaps the hung child.
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertNotIn("stall threshold reached", buf.getvalue())


class StartLineTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_fresh_start_line_format(self):
        role = _make_role("architect", "Architect")
        async def fake_subproc(cmd, prompt, role_id=""):
            return _codex_run(0, _fresh_jsonl(), "")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
                await codex_council._run_role_once(role, "prompt", attempt=1)
        self.assertIn(
            "[codex-council] architect: started (fresh) attempt=1/2 "
            "watchdog=1800s",
            buf.getvalue(),
        )

    async def test_resume_start_line_reports_watchdog_disabled(self):
        role = _make_role("architect", "Architect")
        codex_council.save_session("architect", "sid-1")
        os.environ[codex_council.STALL_SECS_ENV] = "0"
        async def fake_subproc(cmd, prompt, role_id=""):
            return _codex_run(0, _resume_jsonl_no_thread_event(), "")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with patch.object(codex_council, "_run_codex_subprocess", side_effect=fake_subproc):
                await codex_council._run_role_once(role, "prompt", attempt=2)
        self.assertIn(
            "[codex-council] architect: started (resume) attempt=2/2 "
            "watchdog=disabled",
            buf.getvalue(),
        )


class RoleLivenessDescTests(unittest.TestCase):
    def setUp(self):
        self.run = council_liveness.RunStatus()
        self.run.begin(["r"])

    def test_quiet_seconds_since_last_output(self):
        self.run.output("r", 100.0)
        self.assertEqual(self.run.describe("r", 142.4), "r quiet=42s")

    def test_retry_wait_replaces_stale_quiet(self):
        self.run.output("r", 100.0)
        self.run.update("r", state="retry-wait")
        self.assertEqual(self.run.describe("r", 500.0), "r retry-wait")

    def test_unknown_state_degrades_to_bare_id(self):
        self.assertEqual(self.run.describe("r", 1.0), "r")


# ---------- best-effort diagnostics (advisory stderr) ----------

class DiagnosticsHelperTests(unittest.TestCase):
    def setUp(self):
        self._real_stderr = sys.stderr
        self.addCleanup(self._restore)

    def _restore(self):
        sys.stderr = self._real_stderr
        council_common._diagnostics["stream"] = None

    def _fail_stream(self, exc):
        class Failing:
            encoding = "utf-8"

            def __init__(self):
                self.writes = 0

            def write(self, s):
                self.writes += 1
                raise exc

            def flush(self):
                pass

            def close(self):
                pass

        return Failing()

    def test_broken_pipe_redirects_permanently(self):
        failing = self._fail_stream(BrokenPipeError())
        sys.stderr = failing
        council_common._diag("one")
        self.assertIsNotNone(council_common._diagnostics["stream"])
        # Subsequent writes are no-ops against the retired stream.
        council_common._diag("two")
        self.assertEqual(failing.writes, 1)

    def test_closed_stream_valueerror_redirects_permanently(self):
        sys.stderr = self._fail_stream(ValueError("I/O operation on closed file"))
        council_common._diag("one")
        self.assertIsNotNone(council_common._diagnostics["stream"])

    def test_ebadf_redirects_permanently(self):
        sys.stderr = self._fail_stream(OSError(9, "Bad file descriptor"))
        council_common._diag("one")
        self.assertIsNotNone(council_common._diagnostics["stream"])

    def test_transient_oserror_is_suppressed_but_stream_stays_live(self):
        failing = self._fail_stream(OSError(11, "Resource temporarily unavailable"))
        sys.stderr = failing
        council_common._diag("one")  # suppressed, no redirect
        self.assertIsNone(council_common._diagnostics["stream"])
        council_common._diag("two")  # the stream is still being used
        self.assertEqual(failing.writes, 2)

    def test_retry_still_happens_when_stderr_dies_before_the_notice(self):
        async def scenario():
            role = _make_role("architect", "Architect")
            attempts = {"count": 0}

            async def fake_once(r, prompt, attempt):
                attempts["count"] += 1
                if attempt == 1:
                    return codex_council.RoleResult(
                        role=r, ok=False, error="[retriable:5xx] 503",
                        elapsed_seconds=0.1, attempts=attempt, retriable=True,
                    )
                return codex_council.RoleResult(
                    role=r, ok=True, text="recovered", elapsed_seconds=0.1,
                    attempts=attempt,
                )

            with patch.object(codex_council.asyncio, "sleep",
                              AsyncMock(return_value=None)):
                with patch.object(codex_council, "_run_role_once",
                                  side_effect=fake_once):
                    return await codex_council._run_role_attempts(role, "p")

        sys.stderr = self._fail_stream(BrokenPipeError())
        result = asyncio.run(scenario())
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "recovered")
        self.assertEqual(result.attempts, 2)


# ---------- reply preservation / warning composition ----------

class AppendWarningTests(unittest.TestCase):
    def test_none_plus_new_returns_new(self):
        self.assertEqual(codex_council._append_warning(None, "b"), "b")

    def test_existing_kept_first_never_overwritten(self):
        self.assertEqual(
            codex_council._append_warning("continuity lost", "save failed"),
            "continuity lost; save failed",
        )

    def test_empty_new_keeps_existing(self):
        self.assertEqual(codex_council._append_warning("a", None), "a")
        self.assertEqual(codex_council._append_warning("a", ""), "a")


class ReplyPreservationTests(unittest.IsolatedAsyncioTestCase):
    """A completed reply outranks session continuity: persistence failures
    downgrade to warnings, never to failures."""

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

    async def test_fresh_save_failure_keeps_reply_with_warning(self):
        role = _make_role("architect", "Architect")
        async def fake_subproc(cmd, prompt, role_id=""):
            return _codex_run(0, _fresh_jsonl("sid-x", "the reply"), "")
        with patch.object(codex_council, "save_session",
                          side_effect=OSError(13, "Permission denied")):
            with patch.object(codex_council, "_run_codex_subprocess",
                              side_effect=fake_subproc):
                result = await codex_council._run_role_once(role, "p", attempt=1)
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "the reply")
        self.assertIn("reply completed; session state could not be persisted",
                      result.warning)

    async def test_matching_resume_save_failure_keeps_reply(self):
        role = _make_role("architect", "Architect")
        codex_council.save_session("architect", "sid-1")
        stdout = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "sid-1"}),
            json.dumps({"type": "item.completed",
                        "item": {"type": "agent_message", "text": "resumed"}}),
        ])
        async def fake_subproc(cmd, prompt, role_id=""):
            return _codex_run(0, stdout, "")
        with patch.object(codex_council, "save_session",
                          side_effect=OSError(28, "No space left on device")):
            with patch.object(codex_council, "_run_codex_subprocess",
                              side_effect=fake_subproc):
                result = await codex_council._run_role_once(role, "p", attempt=1)
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "resumed")
        self.assertIn("could not be persisted", result.warning)

    async def test_adoption_save_failure_clears_proven_wrong_state(self):
        role = _make_role("architect", "Architect")
        codex_council.save_session("architect", "expected-sid")
        cleared = []
        async def fake_subproc(cmd, prompt, role_id=""):
            return _codex_run(0, _fresh_jsonl("DIFFERENT-sid", "reply"), "")
        with patch.object(codex_council, "save_session",
                          side_effect=OSError(13, "Permission denied")):
            with patch.object(codex_council, "clear_session",
                              side_effect=lambda rid: cleared.append(rid)):
                with patch.object(codex_council, "_run_codex_subprocess",
                                  side_effect=fake_subproc):
                    result = await codex_council._run_role_once(role, "p", attempt=1)
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "reply")
        self.assertIn("prior continuity lost", result.warning)
        self.assertIn("could not be persisted", result.warning)
        self.assertEqual(cleared, ["architect"])

    async def test_adoption_save_and_clear_both_failing_warns_of_repeat(self):
        role = _make_role("architect", "Architect")
        codex_council.save_session("architect", "expected-sid")
        async def fake_subproc(cmd, prompt, role_id=""):
            return _codex_run(0, _fresh_jsonl("DIFFERENT-sid", "reply"), "")
        with patch.object(codex_council, "save_session",
                          side_effect=OSError(13, "Permission denied")):
            with patch.object(codex_council, "clear_session",
                              side_effect=OSError(13, "Permission denied")):
                with patch.object(codex_council, "_run_codex_subprocess",
                                  side_effect=fake_subproc):
                    result = await codex_council._run_role_once(role, "p", attempt=1)
        self.assertTrue(result.ok)
        self.assertIn("may repeat adoption next invocation", result.warning)

    async def test_stale_clear_failure_healed_by_fresh_save_needs_no_clear_warning(self):
        role = _make_role("architect", "Architect")
        codex_council.save_session("architect", "stale-sid")
        async def fake_subproc(cmd, prompt, role_id=""):
            if "resume" in cmd:
                return _codex_run(1, "", "no rollout found for thread id stale-sid")
            return _codex_run(0, _fresh_jsonl("new-sid", "fresh reply"), "")
        with patch.object(codex_council, "clear_session",
                          side_effect=OSError(13, "Permission denied")):
            with patch.object(codex_council, "_run_codex_subprocess",
                              side_effect=fake_subproc):
                result = await codex_council._run_role_once(role, "p", attempt=1)
        self.assertTrue(result.ok)
        # The atomic fresh save replaced the stale file: self-healed, so
        # only the lost continuity is reported.
        self.assertEqual(result.warning, codex_council.STALE_RESUME_WARNING)
        sid, _ = codex_council.load_session("architect")
        self.assertEqual(sid, "new-sid")

    async def test_stale_clear_failure_without_new_id_warns_stale_remains(self):
        role = _make_role("architect", "Architect")
        codex_council.save_session("architect", "stale-sid")
        async def fake_subproc(cmd, prompt, role_id=""):
            if "resume" in cmd:
                return _codex_run(1, "", "no rollout found for thread id stale-sid")
            # Fresh success WITHOUT a thread.started: nothing to save.
            return _codex_run(0, _resume_jsonl_no_thread_event("fresh reply"), "")
        with patch.object(codex_council, "clear_session",
                          side_effect=OSError(13, "Permission denied")):
            with patch.object(codex_council, "_run_codex_subprocess",
                              side_effect=fake_subproc):
                result = await codex_council._run_role_once(role, "p", attempt=1)
        self.assertTrue(result.ok)
        self.assertIn("stale session state could not be cleared", result.warning)

    async def test_stale_clear_failure_carries_warning_through_fresh_failure(self):
        role = _make_role("architect", "Architect")
        codex_council.save_session("architect", "stale-sid")
        async def fake_subproc(cmd, prompt, role_id=""):
            if "resume" in cmd:
                return _codex_run(1, "", "no rollout found for thread id stale-sid")
            return _codex_run(1, "", "fresh exec blew up")
        with patch.object(codex_council, "clear_session",
                          side_effect=OSError(13, "Permission denied")):
            with patch.object(codex_council, "_run_codex_subprocess",
                              side_effect=fake_subproc):
                result = await codex_council._run_role_once(role, "p", attempt=1)
        self.assertFalse(result.ok)
        self.assertIn("stale session state could not be cleared", result.warning)

    def test_clear_session_ignores_only_missing_file(self):
        with patch.dict(os.environ, _env_without_session_key(), clear=True):
            codex_council.clear_session("architect")  # missing: no raise
            codex_council.save_session("architect", "sid")
            os.chmod(self.tmp.name, 0o500)
            self.addCleanup(lambda: os.chmod(self.tmp.name, 0o700))
            with self.assertRaises(OSError):
                codex_council.clear_session("architect")


# ---------- report metadata escaping (full splitlines boundary set) ----------

class ReportInlineBoundaryTests(unittest.TestCase):
    _BOUNDARY_CHARS = (
        "\r", "\n", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e",
        "\x85", "\u2028", "\u2029",
    )

    def test_linebreak_chars_constant_covers_full_boundary_set(self):
        self.assertEqual(
            tuple(council_common.LINEBREAK_CHARS), self._BOUNDARY_CHARS)

    def test_every_boundary_char_is_escaped_to_one_line(self):
        for ch in self._BOUNDARY_CHARS:
            with self.subTest(char=hex(ord(ch))):
                out = council_common._report_inline(f"left{ch}right")
                self.assertEqual(len(out.splitlines()), 1)
                self.assertNotIn(ch, out)

    def test_every_non_printable_char_is_escaped(self):
        """Terminal controls in catalog, config, or Codex text (ESC, BEL,
        C1 CSI, DEL, bidi overrides, lone surrogates) never reach a
        terminal; printable text, including non-ASCII, is unchanged."""
        for ch, escaped in (
            ("\x1b", "\\x1b"), ("\x07", "\\x07"), ("\x00", "\\x00"),
            ("\t", "\\t"), ("\x7f", "\\x7f"), ("\x9b", "\\x9b"),
            ("‮", "\\u202e"), ("​", "\\u200b"),
            (" ", "\\xa0"), ("\udcff", "\\udcff"),
            ("\U000e0001", "\\U000e0001"), ("\n", "\\n"),
            ("\x85", "\\u0085"),
        ):
            with self.subTest(char=hex(ord(ch))):
                self.assertEqual(
                    council_common._report_inline(f"a{ch}b"), f"a{escaped}b")
        printable = "plain — café 日本 [codex-council] x=1 reply=/p"
        self.assertEqual(council_common._report_inline(printable), printable)

    def test_diagnostic_lines_escape_the_reply_marker(self):
        """_log_inline keeps foreign text from hiding a diagnostic line
        from the follower's reply-path filter."""
        line = council_common._log_inline(
            "[codex-council:r] warn reply=/tmp/x\x1b")
        self.assertEqual(line, "[codex-council:r] warn reply\\x3d/tmp/x\\x1b")
        self.assertTrue(council_liveness._reply_path_ok(
            line, "/abs/run/replies"))

    def test_warning_and_failed_lines_stay_single_report_lines(self):
        for ch in self._BOUNDARY_CHARS:
            with self.subTest(char=hex(ord(ch))):
                role = _make_role("architect", "Architect")
                results = [
                    codex_council.RoleResult(
                        role=role, ok=True, text="body",
                        warning=f"warn{ch}tail", elapsed_seconds=0.1,
                    ),
                    codex_council.RoleResult(
                        role=_make_role("security", "Security"), ok=False,
                        error=f"boom{ch}tail", elapsed_seconds=0.1,
                    ),
                ]
                report = codex_council._format_report(results, 0.2)
                warn_lines = [
                    ln for ln in report.splitlines()
                    if ln.startswith("_Warning: ")
                ]
                fail_lines = [
                    ln for ln in report.splitlines()
                    if ln.startswith("_Failed: ")
                ]
                self.assertEqual(len(warn_lines), 1)
                self.assertEqual(len(fail_lines), 1)
                self.assertTrue(warn_lines[0].endswith("tail_"))
                self.assertTrue(fail_lines[0].endswith("tail_"))

    def test_label_with_nel_is_rejected(self):
        raw = json.dumps([_role_json("x", "Good\x85Forged")])
        _assert_usage_exit(
            self, lambda: codex_council._parse_roles_json(raw),
            expect_in_stderr="label must not contain newlines",
        )


# ---------- uniform roles-file rewrite recovery ----------

class RolesRewriteRecoveryTests(unittest.TestCase):
    """Every roles-file validation failure carries the identical full-rewrite
    recovery sentence exactly once."""

    _CORE = "rewrite the entire file passed to --roles-file"

    def _invalid_payloads(self):
        ok = _valid_instruction("review")
        return {
            "invalid-json": "{not json",
            "non-list-top-level": json.dumps({"id": "x"}),
            "empty-panel": json.dumps([]),
            "non-object-entry": json.dumps(["nope"]),
            "unknown-field": json.dumps([{**_role_json("a", "A"), "_": ""}]),
            "missing-field": json.dumps([{"label": "L", "instruction": [ok]}]),
            "empty-id": json.dumps([_role_json("", "A")]),
            "non-string-label": json.dumps(
                [{"id": "a", "label": 3, "instruction": [ok]}]),
            "bad-id-regex": json.dumps([_role_json("Bad ID", "A")]),
            "label-linebreak": json.dumps([_role_json("a", "A\nB")]),
            "wrong-instruction-type": json.dumps(
                [{"id": "a", "label": "A", "instruction": "string form"}]),
            "empty-instruction-list": json.dumps(
                [{"id": "a", "label": "A", "instruction": []}]),
            "blank-instruction-item": json.dumps(
                [{"id": "a", "label": "A", "instruction": ["   "]}]),
            "bad-execution-type": json.dumps([
                {**_role_json("a", "A"), "execution": "bypass"}]),
            "unauthorized-execution": json.dumps([
                {**_role_json("a", "A"), "execution": {"sandbox": "bypass"}}]),
            "duplicate-id": json.dumps(
                [_role_json("a", "A"), _role_json("a", "A2")]),
        }

    def test_every_invalid_class_names_the_shared_recovery_exactly_once(self):
        for name, raw in self._invalid_payloads().items():
            with self.subTest(defect=name):
                buf = io.StringIO()
                with contextlib.redirect_stderr(buf):
                    with self.assertRaises(SystemExit) as ctx:
                        codex_council._parse_roles_json(raw)
                self.assertEqual(ctx.exception.code, 2)
                err = buf.getvalue()
                self.assertEqual(err.count(self._CORE), 1, err)
                self.assertIn("do not patch, append, or replace", err)

    def test_staged_launch_scope_swaps_in_the_new_directory_form(self):
        """Inside _roles_recovery every class carries the staged launch's
        form exactly once and no pre-flight re-run; outside it, the default
        comes back."""
        staged = council_common.STAGED_LAUNCH_ROLES_RECOVERY
        for name, raw in self._invalid_payloads().items():
            with self.subTest(defect=name):
                with council_common._roles_recovery(staged):
                    err = _assert_usage_exit(
                        self, lambda raw=raw: codex_council._parse_roles_json(raw),
                        expect_in_stderr=staged,
                    )
                self.assertEqual(err.count(staged), 1, err)
                self.assertNotIn(self._CORE, err)
                self.assertNotIn("re-run the pre-flight", err)
        _assert_usage_exit(
            self, lambda: codex_council._parse_roles_json("{not json"),
            expect_in_stderr=council_common.ROLES_REWRITE_RECOVERY,
        )


# ---------- version visibility ----------

class PluginVersionTests(unittest.TestCase):
    def test_resolves_manifest_three_levels_above_scripts(self):
        manifest = os.path.abspath(os.path.join(
            os.path.dirname(council_common.__file__),
            "..", "..", "..", ".claude-plugin", "plugin.json",
        ))
        with open(manifest, encoding="utf-8") as f:
            expected = json.load(f)["version"]
        self.assertEqual(council_common._plugin_version(), expected)

    def test_unknown_on_unresolvable_manifest(self):
        with patch.object(council_common, "__file__", "/x.py"):
            self.assertEqual(council_common._plugin_version(), "unknown")






if __name__ == "__main__":
    unittest.main()
