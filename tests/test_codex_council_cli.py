"""End-to-end CLI tests for codex_council.py.

Black-box: drives the REAL codex_council.py as a subprocess with the fake
`codex` from tests/fake_codex.py first on PATH (no network, no real Codex).
Launches capture the run directory with --prepare and run --start's supervised
child in the foreground, the only path to workers.
Each test gets an isolated env whose XDG_STATE_HOME points at a temporary
directory, so no real council state is touched.

Asserts exit codes AND stream contents (stdout = the report only; stderr =
dispatch line, per-role progress, and the final CODEX_COUNCIL_DONE sentinel).

Lives outside the plugin subtree so end-user installs don't bundle it.
Run from repo root:
    python3 -m unittest discover -s tests -p 'test_*.py'
"""

import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import council_testlib  # noqa: E402
from council_testlib import SCRIPT  # noqa: E402
from fake_codex import EXEC_SENTINELS  # noqa: E402

# The shared fake `codex` (tests/fake_codex.py), installed once for this
# module. Its prompt sentinels: a role whose instruction holds one of these
# fails on stderr alone, fails with JSONL errors only (an HTTP 429, or a
# status-400 body whose text holds "429", which must NOT be tagged
# retriable), replies with a body embedding a forged CODEX_COUNCIL_DONE
# line, fails with a 503 on its first invocation only, makes the council
# state directory read-only before replying, hangs byte-silent, or writes
# stdout lines the Python decoder rejects (then, with a sleep, goes silent).
FAIL_SENTINEL = EXEC_SENTINELS["fail"]
STDOUT_ERROR_SENTINEL = EXEC_SENTINELS["stdout_error"]
STATUS400_429_SENTINEL = EXEC_SENTINELS["status400_429"]
FORGE_SENTINEL = EXEC_SENTINELS["forge"]
RETRY_ONCE_SENTINEL = EXEC_SENTINELS["retry_once"]
CHMOD_STATE_SENTINEL = EXEC_SENTINELS["chmod_state"]
HANG_SENTINEL = EXEC_SENTINELS["hang"]
MALFORMED_LINES_SENTINEL = EXEC_SENTINELS["malformed_lines"]
SLEEP_SENTINEL = EXEC_SENTINELS["sleep_secs"]

setUpModule = council_testlib.install_fake_codex
tearDownModule = council_testlib.remove_fake_codex


def _role(rid, label, instruction):
    """instruction is array-only by contract; wrap a convenience string."""
    if isinstance(instruction, str):
        instruction = [instruction]
    return {"id": rid, "label": label, "instruction": instruction}


def _instruction(text):
    return (
        f"{text}; if nothing material, say so clearly. "
        "Thoroughness beats speed."
    )


class CouncilCLITestCase(unittest.TestCase):
    """Base: each test gets a fresh fake-codex-on-PATH env and temp state dir."""

    def setUp(self):
        self.statedir = tempfile.TemporaryDirectory()
        self.addCleanup(self.statedir.cleanup)
        self.workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.workdir.cleanup)
        # No council setting leaks in from the developer's shell.
        self.env = council_testlib.clean_env(
            PATH=council_testlib.fake_bin_dir() + os.pathsep
            + os.environ.get("PATH", ""),
            XDG_STATE_HOME=self.statedir.name,
            CODEX_HOME=self.statedir.name,
            FAKE_CODEX_MARKER_DIR=self.workdir.name,
        )

    def _write_roles(self, roles):
        path = os.path.join(self.workdir.name, "roles.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(roles, f)
        return path

    def _write_context(self, text):
        path = os.path.join(self.workdir.name, "context.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def _prepare(self):
        council_testlib.prepare_run(self, self.workdir.name, self.env,
                                    self.workdir.name)

    def _launch(self, roles, context="please review this change\n", *,
                env=None, merged=False, **kwargs):
        """Stage roles.json and context.md, capture, and run the council."""
        self._write_roles(roles)
        self._write_context(context)
        self._prepare()
        return council_testlib.run_supervised(
            self.workdir.name, self.env if env is None else env,
            self.workdir.name, merged=merged, **kwargs)

    def _child(self, roles_path, context_path, lock_fd):
        """The supervised child's argv with a given descriptor, for the
        checks it makes before it trusts --start's claim."""
        return subprocess.run(
            [sys.executable, SCRIPT, "--roles-file", roles_path,
             "--context-file", context_path, "--skill-contract",
             council_testlib.EPOCH, "--supervisor-lock-fd", str(lock_fd)],
            capture_output=True, text=True, env=self.env, cwd=self.workdir.name,
            stdin=subprocess.DEVNULL, pass_fds=(lock_fd,))

    def _dummy_fd(self):
        fd = os.open(os.devnull, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        return fd

    def _run(self, *, input, args=()):
        return subprocess.run(
            [sys.executable, SCRIPT, *args],
            input=input,
            capture_output=True,
            text=True,
            env=self.env,
            cwd=self.workdir.name,
        )

    def _run_merged(self, *, input, args=()):
        # stdout+stderr to ONE stream so their relative byte order is faithful:
        # lets us prove the report is fully written BEFORE the final sentinel.
        return subprocess.run(
            [sys.executable, SCRIPT, *args],
            input=input,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=self.env,
            cwd=self.workdir.name,
        )

    def _last_nonempty_stderr_line(self, stderr):
        lines = [ln for ln in stderr.splitlines() if ln.strip()]
        return lines[-1] if lines else ""


class BareInvocationTests(CouncilCLITestCase):
    def test_bare_invocation_exits_2(self):
        proc = self._run(input="some context here\n")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        # Not just the code — the safety-net message must survive too.
        self.assertIn("nothing to do: capture RUNDIR with --prepare", proc.stderr)

    def test_empty_roles_file_arg_exits_2(self):
        proc = self._run(input="some context here\n", args=("--roles-file", ""))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("must be non-empty", proc.stderr)

    def test_direct_launch_is_refused_before_reading_anything(self):
        roles_path = self._write_roles([
            _role("architect", "Architect", _instruction("Review architecture")),
        ])
        context_path = self._write_context("please review\n")
        for args in (("--roles-file", roles_path),
                     ("--roles-file", roles_path, "--context-file", context_path)):
            with self.subTest(args=args):
                proc = self._run(input="piped context\n", args=args)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("there is no direct launch", proc.stderr)
                self.assertEqual(proc.stdout, "")
        self.assertEqual(sorted(os.listdir(self.workdir.name)),
                         ["context.md", "roles.json"])

    def test_missing_roles_parent_is_refused_with_abandon_recovery(self):
        # The lexical parent does not exist, so the launch-side privacy gate
        # rejects it first — with the abandon-the-path recovery, never an
        # invitation to mkdir the same predictable path.
        missing = os.path.join(self.workdir.name, "missing")
        proc = self._child(os.path.join(missing, "roles.json"),
                           os.path.join(missing, "context.md"), self._dummy_fd())
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("--roles-file", proc.stderr)
        self.assertIn("does not exist", proc.stderr)
        self.assertIn("do not mkdir it", proc.stderr)
        self.assertNotIn("[codex-council] dispatching", proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_mismatched_staging_paths_report_both_inputs(self):
        actual = tempfile.TemporaryDirectory()
        requested = tempfile.TemporaryDirectory()
        self.addCleanup(actual.cleanup)
        self.addCleanup(requested.cleanup)
        with open(os.path.join(actual.name, "roles.json"), "w", encoding="utf-8") as f:
            json.dump([
                _role("architect", "Architect",
                      _instruction("Review architecture")),
            ], f)
        with open(os.path.join(actual.name, "context.md"), "w", encoding="utf-8") as f:
            f.write("real context\n")
        roles_path = os.path.join(requested.name, "roles.json")
        context_path = os.path.join(requested.name, "context.md")
        lock = council_testlib.claim_supervisor_lock(requested.name)
        self.addCleanup(os.close, lock)
        proc = self._child(roles_path, context_path, lock)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn(roles_path, proc.stderr)
        self.assertIn(context_path, proc.stderr)
        self.assertIn("same mktemp directory", proc.stderr)
        self.assertNotIn("[codex-council] dispatching", proc.stderr)
        self.assertNotIn("CODEX_COUNCIL_DONE", proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_existing_roles_and_context_in_different_dirs_exits_2(self):
        roles_dir = tempfile.TemporaryDirectory()
        context_dir = tempfile.TemporaryDirectory()
        self.addCleanup(roles_dir.cleanup)
        self.addCleanup(context_dir.cleanup)
        roles_path = os.path.join(roles_dir.name, "roles.json")
        context_path = os.path.join(context_dir.name, "context.md")
        with open(roles_path, "w", encoding="utf-8") as f:
            json.dump([
                _role("architect", "Architect",
                      _instruction("Review architecture")),
            ], f)
        with open(context_path, "w", encoding="utf-8") as f:
            f.write("real context\n")
        proc = self._child(roles_path, context_path, self._dummy_fd())
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("same mktemp directory", proc.stderr)
        self.assertIn(roles_dir.name, proc.stderr)
        self.assertIn(context_dir.name, proc.stderr)
        self.assertNotIn("[codex-council] dispatching", proc.stderr)
        self.assertEqual(proc.stdout, "")


class HappyPathTests(CouncilCLITestCase):
    def test_reported_long_role_id_runs_and_uses_a_safe_state_filename(self):
        rid = "parent-mapper-augmentation-auditor"
        proc = self._launch([
            _role(rid, "Parent Mapper Auditor", _instruction("Review mapping")),
        ])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(rid, proc.stdout)
        digest = hashlib.sha256(rid.encode("utf-8")).hexdigest()
        state_root = os.path.join(self.statedir.name, "codex-council")
        state_files = [name for name in os.listdir(state_root) if name.endswith(".json")]
        self.assertEqual(len(state_files), 1)
        self.assertTrue(
            state_files[0].endswith(f"__role-sha256-{digest}.json"),
            state_files[0],
        )

    def test_an_unsafe_state_directory_refuses_the_launch_before_dispatch(self):
        elsewhere = os.path.join(self.statedir.name, "elsewhere")
        os.mkdir(elsewhere)
        os.chmod(elsewhere, 0o755)
        os.symlink(elsewhere, os.path.join(self.statedir.name, "codex-council"))
        proc = self._launch([
            _role("architect", "Architect", _instruction("Review architecture")),
        ])
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("invocation contract: saved-thread state directory",
                      proc.stderr)
        self.assertIn("is a symlink", proc.stderr)
        self.assertNotIn("dispatching", proc.stderr)
        self.assertEqual(os.listdir(elsewhere), [])
        self.assertEqual(stat.S_IMODE(os.stat(elsewhere).st_mode), 0o755)

    def test_the_preflight_refuses_an_unsafe_state_directory(self):
        elsewhere = os.path.join(self.statedir.name, "elsewhere")
        os.mkdir(elsewhere)
        os.chmod(elsewhere, 0o755)
        os.symlink(elsewhere, os.path.join(self.statedir.name, "codex-council"))
        self._write_roles([
            _role("architect", "Architect", _instruction("Review architecture")),
        ])
        self._write_context("please review this change\n")
        self._prepare()
        proc = self._run(input="",
                         args=("--check-staging-dir", self.workdir.name))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("--check-staging-dir: saved-thread state directory",
                      proc.stderr)
        self.assertIn("is a symlink", proc.stderr)
        self.assertNotIn("staging OK", proc.stdout)
        self.assertEqual(os.listdir(elsewhere), [])
        self.assertEqual(stat.S_IMODE(os.stat(elsewhere).st_mode), 0o755)

    def test_a_launch_under_a_permissive_umask_keeps_state_private(self):
        state_root = os.path.join(self.statedir.name, "codex-council")
        os.mkdir(state_root)
        os.chmod(state_root, 0o755)  # as an umask-dependent release left it
        previous = os.umask(0o000)
        try:
            proc = self._launch([
                _role("architect", "Architect",
                      _instruction("Review architecture")),
            ])
        finally:
            os.umask(previous)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(stat.S_IMODE(os.lstat(state_root).st_mode), 0o700)
        names = os.listdir(state_root)
        self.assertTrue(any(n.endswith(".lock") for n in names), names)
        for name in names:
            self.assertEqual(
                stat.S_IMODE(os.lstat(os.path.join(state_root, name)).st_mode),
                0o600, name)

    def test_happy_path_report_and_progress(self):
        proc = self._launch([
            _role("architect", "Architect",
                  _instruction("Review architecture")),
            _role("security", "Security",
                  _instruction("Review security")),
        ])
        self.assertEqual(proc.returncode, 0, proc.stderr)

        # STDOUT is the report only — no progress lines leak into it.
        self.assertIn("# Codex Council", proc.stdout)
        self.assertIn("Architect", proc.stdout)
        self.assertIn("Security", proc.stdout)
        self.assertNotIn("[codex-council]", proc.stdout)
        self.assertNotIn("CODEX_COUNCIL_DONE", proc.stdout)

        # STDERR carries the dispatch line, per-role progress, and the
        # final sentinel as the LAST non-empty line.
        self.assertIn("[codex-council] dispatching 2 roles", proc.stderr)
        self.assertRegex(proc.stderr, r"\[codex-council\] \d+/2 .+: ok \(")
        # The completion line ends in the absolute reply-file path.
        replies = os.path.join(self.workdir.name, "replies")
        for rid in ("architect", "security"):
            self.assertRegex(
                proc.stderr,
                r"(?m)^\[codex-council\] \d+/2 " + rid
                + r": ok \([\d.]+s\) reply="
                + re.escape(os.path.join(replies, rid + ".md")) + r"$",
            )
        self.assertRegex(
            self._last_nonempty_stderr_line(proc.stderr),
            r"\[codex-council\] CODEX_COUNCIL_DONE ok=2 total=2 "
            r"elapsed=[\d.]+s exit=0 version=\S+$",
        )

    def test_check_staging_dir_cli(self):
        roles_path = self._write_roles([
            _role("architect", "Architect",
                  _instruction("Review architecture")),
        ])
        self.assertEqual(os.path.basename(roles_path), "roles.json")
        self._write_context("please review this change\n")
        self._prepare()
        proc = self._run(
            input="",
            args=("--check-staging-dir", self.workdir.name),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("staging OK", proc.stdout)
        self.assertIn("(1 roles; max parallel 6)", proc.stdout)
        self.assertNotIn("[codex-council] dispatching", proc.stderr)

    def _strip_codex_from_path(self):
        """Point PATH at one empty dir so no codex binary is findable."""
        emptybin = tempfile.TemporaryDirectory()
        self.addCleanup(emptybin.cleanup)
        self.env["PATH"] = emptybin.name

    def test_check_staging_dir_fails_without_codex(self):
        """Preflight must not say 'staging OK' when the launch would die
        on a missing codex binary inside a background process."""
        self._write_roles([
            _role("architect", "Architect",
                  _instruction("Review architecture")),
        ])
        self._write_context("please review this change\n")
        self._prepare()
        self._strip_codex_from_path()
        proc = self._run(
            input="",
            args=("--check-staging-dir", self.workdir.name),
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("Codex CLI not found on PATH", proc.stderr)
        self.assertNotIn("staging OK", proc.stdout)
        # Install hint must be install-method-neutral, with PATH diagnostics.
        self.assertNotIn("npm i -g @openai/codex", proc.stderr)
        self.assertIn("Current PATH:", proc.stderr)

    def test_launch_without_codex_is_usage_error(self):
        self._write_roles([
            _role("architect", "Architect",
                  _instruction("Review architecture")),
        ])
        self._write_context("please review this change\n")
        self._prepare()
        # The child still needs ps to record its own identity.
        self._strip_codex_from_path()
        self.env["PATH"] += os.pathsep + os.path.dirname(shutil.which("ps"))
        proc = council_testlib.run_supervised(self.workdir.name, self.env,
                                              self.workdir.name)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("Codex CLI not found on PATH", proc.stderr)
        self.assertNotIn("[codex-council] dispatching", proc.stderr)
        # The launch's redirections already claimed this directory, so the
        # recovery starts over elsewhere instead of re-running the preflight.
        self.assertIn("then start over in a new directory", proc.stderr)
        self.assertNotIn("re-run --check-staging-dir", proc.stderr)

    def test_report_precedes_sentinel_in_combined_stream(self):
        # Recovery contract (R2): CODEX_COUNCIL_DONE must appear only AFTER the
        # full report is written, so "tail err.log shows the sentinel" reliably
        # means "out.md is complete." Merge stdout+stderr into one stream so the
        # relative byte order of report vs sentinel is observable, then assert it.
        proc = self._launch([
            _role("architect", "Architect",
                  _instruction("Review architecture")),
            _role("security", "Security",
                  _instruction("Review security")),
        ], merged=True)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        merged = proc.stdout
        self.assertIn("# Codex Council", merged)
        self.assertIn("CODEX_COUNCIL_DONE", merged)
        self.assertLess(
            merged.index("# Codex Council"),
            merged.index("CODEX_COUNCIL_DONE"),
            "report must be fully written before the completion sentinel",
        )
        # Stronger: the END of the report (the last role's reply) must also
        # precede the sentinel — not just the report header.
        self.assertLess(
            merged.rindex("fake reply from codex"),
            merged.index("CODEX_COUNCIL_DONE"),
            "the report tail must precede the completion sentinel",
        )


class LocaleRobustnessTests(CouncilCLITestCase):
    def test_report_writes_under_strict_c_locale(self):
        """Under LC_ALL=C with UTF-8 mode and C-locale coercion both disabled,
        stdout defaults to ASCII, so the em dash in the report header ("# Codex
        Council — N/M") would raise UnicodeEncodeError and lose BOTH the report
        and the CODEX_COUNCIL_DONE sentinel. _force_utf8_streams prevents that.
        The role reply itself is ASCII, so this isolates the header em dash."""
        env = dict(self.env)
        env["LC_ALL"] = "C"
        env["LANG"] = "C"
        env["PYTHONUTF8"] = "0"
        env["PYTHONCOERCECLOCALE"] = "0"
        env.pop("PYTHONIOENCODING", None)
        # Decode the child's output as UTF-8 on the parent side regardless of
        # the parent's own locale, so the test asserts the child's behavior.
        proc = self._launch(
            [_role("architect", "Architect", _instruction("Review architecture"))],
            env=env, encoding="utf-8")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("# Codex Council", proc.stdout)
        self.assertIn("—", proc.stdout)  # the em dash survived the write
        self.assertRegex(
            self._last_nonempty_stderr_line(proc.stderr),
            r"\[codex-council\] CODEX_COUNCIL_DONE ok=1 total=1 "
            r"elapsed=[\d.]+s exit=0 version=\S+$",
        )


class ContextGuardTests(CouncilCLITestCase):
    def test_large_context_reaches_codex_without_a_plugin_cap(self):
        proc = self._launch([_role("architect", "Architect", _instruction("Review"))],
                            context="a" * (10 * 1024 * 1024 + 1))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("CODEX_COUNCIL_DONE ok=1 total=1", proc.stderr)
        self.assertIn("# Codex Council", proc.stdout)

    def test_context_file_missing_exits_2_before_codex(self):
        self._write_roles([_role("architect", "Architect", _instruction("Review"))])
        missing = os.path.join(self.workdir.name, "context.md")
        lock = council_testlib.claim_supervisor_lock(self.workdir.name)
        self.addCleanup(os.close, lock)
        proc = self._child(os.path.join(self.workdir.name, "roles.json"), missing, lock)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("--context-file", proc.stderr)
        self.assertIn("cannot read", proc.stderr)
        self.assertIn("Staging hint", proc.stderr)
        self.assertNotIn("[codex-council] dispatching", proc.stderr)
        self.assertNotIn("CODEX_COUNCIL_DONE", proc.stderr)
        self.assertEqual(proc.stdout, "")


class FailureReportingTests(CouncilCLITestCase):
    def test_mixed_failure_reports_and_exits_0(self):
        optional = _role("security", "Security",
                         _instruction(f"Review security {FAIL_SENTINEL}"))
        optional["execution"] = {"required": False}
        proc = self._launch([
            _role("architect", "Architect",
                  _instruction("Review architecture")),
            optional,
        ])
        # The required role succeeded and only an optional one failed → exit 0.
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # The failing role shows FAILED in the report — anchored to the
        # summary-line shape so a regression in that line is caught.
        self.assertRegex(proc.stdout, r"(?m)^- \*\*Security\*\* \[security\]: FAILED\b")
        self.assertRegex(
            self._last_nonempty_stderr_line(proc.stderr),
            r"\[codex-council\] CODEX_COUNCIL_DONE ok=1 total=2 "
            r"elapsed=[\d.]+s exit=0 version=\S+$",
        )

    def test_all_roles_fail_exits_1(self):
        proc = self._launch([
            _role("architect", "Architect",
                  _instruction(f"Review {FAIL_SENTINEL}")),
            _role("security", "Security",
                  _instruction(f"Review {FAIL_SENTINEL}")),
        ])
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertRegex(
            self._last_nonempty_stderr_line(proc.stderr),
            r"\[codex-council\] CODEX_COUNCIL_DONE ok=0 total=2 "
            r"elapsed=[\d.]+s exit=1 version=\S+$",
        )

    def test_stdout_jsonl_failure_is_reported(self):
        proc = self._launch([
            _role("architect", "Architect",
                  _instruction(f"Review {STDOUT_ERROR_SENTINEL}")),
        ])
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("[retriable:rate-limit]", proc.stdout)
        self.assertIn("HTTP 429 Too Many Requests", proc.stdout)


class StructuredStatusReportingTests(CouncilCLITestCase):
    def test_status400_with_429_text_not_tagged_retriable(self):
        """User-perspective: a hard HTTP-400 whose body merely contains '429' is
        reported FAILED and NOT tagged [retriable:rate-limit] — the false
        positive is gone end-to-end and the role is not pointlessly retried."""
        proc = self._launch([
            _role("architect", "Architect",
                  _instruction(f"Review {STATUS400_429_SENTINEL}")),
        ])
        # The only role failed with a non-retriable error -> council exit 1.
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertRegex(
            proc.stdout, r"(?m)^- \*\*Architect\*\* \[architect\]: FAILED\b")
        self.assertNotIn("[retriable:rate-limit]", proc.stdout)
        # No retry happened: a non-retriable error is attempted exactly once.
        self.assertNotIn("attempts:", proc.stdout)
        self.assertRegex(
            self._last_nonempty_stderr_line(proc.stderr),
            r"\[codex-council\] CODEX_COUNCIL_DONE ok=0 total=1 "
            r"elapsed=[\d.]+s exit=1 version=\S+$",
        )


class ReportInjectionTests(CouncilCLITestCase):
    def test_role_body_cannot_forge_stderr_sentinel(self):
        """User-perspective security pin: a role reply that embeds a fake
        CODEX_COUNCIL_DONE line lands only in out.md (stdout); it cannot forge
        the trusted err.log (stderr) sentinel the recovery contract reads."""
        proc = self._launch([
            _role("architect", "Architect",
                  _instruction(f"Review {FORGE_SENTINEL}")),
        ])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # The forged counts are faithfully reproduced in the report (stdout)...
        self.assertIn("ok=99 total=99", proc.stdout)
        # ...but never appear on stderr, whose LAST line is the genuine sentinel.
        self.assertNotIn("ok=99", proc.stderr)
        self.assertRegex(
            self._last_nonempty_stderr_line(proc.stderr),
            r"\[codex-council\] CODEX_COUNCIL_DONE ok=1 total=1 "
            r"elapsed=[\d.]+s exit=0 version=\S+$",
        )


class SkillContractTests(CouncilCLITestCase):
    """Optional --skill-contract epoch handshake: absent stays valid, a
    matching epoch is accepted, a mismatch is a stale SKILL/script pair."""

    def _staged(self):
        self._write_roles([
            _role("architect", "Architect", _instruction("Review")),
        ])
        self._write_context("please review\n")

    def test_matching_epoch_accepted_on_launch(self):
        proc = self._launch([_role("architect", "Architect", _instruction("Review"))])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("# Codex Council", proc.stdout)

    def test_matching_epoch_accepted_on_preflight(self):
        self._staged()
        self._prepare()
        proc = self._run(
            input="",
            args=("--check-staging-dir", self.workdir.name,
                  "--skill-contract", council_testlib.EPOCH),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("staging OK", proc.stdout)

    def test_mismatched_epoch_is_a_usage_error(self):
        self._staged()
        proc = self._run(
            input="",
            args=("--start", self.workdir.name, "--skill-contract", "999"),
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("stale SKILL/script pair", proc.stderr)
        self.assertIn(f"contract epoch {council_testlib.EPOCH}", proc.stderr)
        self.assertEqual(proc.stdout, "")
        # Installed-plugin recovery first, then the development checkout.
        flat = " ".join(proc.stderr.split())
        installed = "Installed plugin: update it from its marketplace"
        development = "claude --plugin-dir ./plugins/codex-council"
        self.assertIn(installed, flat)
        self.assertIn(development, flat)
        self.assertLess(flat.index(installed), flat.index(development))
        self.assertIn("then start a fresh session", flat)
        self.assertIn("Never change the epoch", flat)
        self.assertNotIn("[codex-council] dispatching", proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_any_other_epoch_is_a_stale_pair_on_every_path(self):
        """Any supplied epoch different from SKILL_CONTRACT_EPOCH is refused
        with the stale-pair recovery on every command alike."""
        other = str(int(council_testlib.EPOCH) - 1)
        self._staged()
        run_dir = self.workdir.name
        for args in (
            ("--prepare", run_dir), ("--discover", run_dir),
            ("--check-staging-dir", run_dir), ("--freshness", run_dir),
            ("--start", run_dir), ("--follow", run_dir),
            ("--status", run_dir), ("--cancel", run_dir),
            ("--reap", run_dir),
        ):
            with self.subTest(command=args[0]):
                proc = self._run(input="", args=(*args, "--skill-contract",
                                                  other))
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn(f"--skill-contract {other} does not match this "
                              f"script's contract epoch {council_testlib.EPOCH}: "
                              "stale SKILL/script pair", proc.stderr)
                self.assertEqual(proc.stdout, "")
        self.assertEqual(sorted(os.listdir(run_dir)),
                         ["context.md", "roles.json"])

    def test_mismatched_epoch_rejected_on_preflight_too(self):
        self._staged()
        self._prepare()
        proc = self._run(
            input="",
            args=("--check-staging-dir", self.workdir.name,
                  "--skill-contract", "999"),
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("stale SKILL/script pair", proc.stderr)
        self.assertNotIn("staging OK", proc.stdout)


class VersionVisibilityTests(CouncilCLITestCase):
    def test_preflight_and_dispatch_carry_version(self):
        roles_path = os.path.join(self.workdir.name, "roles.json")
        with open(roles_path, "w", encoding="utf-8") as f:
            json.dump([
                _role("architect", "Architect", _instruction("Review")),
            ], f)
        with open(os.path.join(self.workdir.name, "context.md"), "w",
                  encoding="utf-8") as f:
            f.write("please review\n")
        self._prepare()
        preflight = self._run(
            input="", args=("--check-staging-dir", self.workdir.name))
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        self.assertRegex(preflight.stdout, r"staging OK: .+ version=\S+")
        launch = council_testlib.run_supervised(self.workdir.name, self.env,
                                                self.workdir.name)
        self.assertEqual(launch.returncode, 0, launch.stderr)
        self.assertRegex(
            launch.stderr, r"\[codex-council\] dispatching 1 roles .*version=\S+")
        self.assertRegex(launch.stderr, r"CODEX_COUNCIL_DONE .*version=\S+")


class LaunchPrivacyGateTests(CouncilCLITestCase):
    """The supervised child re-validates each staged input's LEXICAL parent
    for privacy before any content read or trust in its descriptor —
    mirroring the preflight gate."""

    def _write_inputs(self, dirname, roles_content=None):
        roles_path = os.path.join(dirname, "roles.json")
        with open(roles_path, "w", encoding="utf-8") as f:
            if roles_content is None:
                json.dump([
                    _role("architect", "Architect", _instruction("Review")),
                ], f)
            else:
                f.write(roles_content)
        context_path = os.path.join(dirname, "context.md")
        with open(context_path, "w", encoding="utf-8") as f:
            f.write("please review\n")
        return roles_path, context_path

    def test_public_parent_direct_launch_exits_2_with_abandon_recovery(self):
        public = os.path.join(self.workdir.name, "public")
        os.mkdir(public, mode=0o755)
        os.chmod(public, 0o755)
        roles_path, context_path = self._write_inputs(public)
        proc = self._child(roles_path, context_path, self._dummy_fd())
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("not private 0700", proc.stderr)
        self.assertIn("abandon this directory", proc.stderr)
        self.assertIn("--roles-file: ", proc.stderr)
        self.assertNotIn("--check-staging-dir:", proc.stderr)
        self.assertNotIn("[codex-council] dispatching", proc.stderr)

    def test_symlink_parent_to_private_target_is_rejected(self):
        real = os.path.join(self.workdir.name, "real")
        os.mkdir(real, mode=0o700)
        self._write_inputs(real)
        link = os.path.join(self.workdir.name, "link")
        os.symlink(real, link)
        proc = self._child(os.path.join(link, "roles.json"),
                           os.path.join(link, "context.md"), self._dummy_fd())
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("is a symlink", proc.stderr)
        self.assertNotIn("--check-staging-dir:", proc.stderr)

    def test_privacy_gate_precedes_content_validation(self):
        public = os.path.join(self.workdir.name, "public")
        os.mkdir(public, mode=0o755)
        os.chmod(public, 0o755)
        roles_path, context_path = self._write_inputs(
            public, roles_content="{definitely not json")
        proc = self._child(roles_path, context_path, self._dummy_fd())
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("abandon this directory", proc.stderr)
        self.assertNotIn("invalid JSON", proc.stderr)


class StallWatchdogCliTests(CouncilCLITestCase):
    def _write_role(self, instruction):
        path = os.path.join(self.workdir.name, "roles.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump([_role("architect", "Architect",
                             _instruction(instruction))], f)
        return path

    def test_invalid_stall_env_is_a_usage_error_before_dispatch(self):
        env = dict(self.env)
        env["CODEX_COUNCIL_STALL_SECS"] = "soon"
        proc = self._launch([_role("architect", "Architect", _instruction("Review"))],
                            env=env)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("CODEX_COUNCIL_STALL_SECS", proc.stderr)
        self.assertNotIn("[codex-council] dispatching", proc.stderr)

    def test_invalid_stall_env_fails_the_preflight_too(self):
        """A value the background launch would refuse (exit 2, error only
        in err.log) must not pass the pre-flight as "staging OK"."""
        self._write_role("Review")
        self._write_context("please review\n")
        self._prepare()
        self.env["CODEX_COUNCIL_STALL_SECS"] = "30m"
        proc = self._run(input="", args=(
            "--check-staging-dir", self.workdir.name, "--skill-contract", council_testlib.EPOCH))
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("CODEX_COUNCIL_STALL_SECS must be a positive integer "
                      "(or 0 to disable the watchdog); got '30m'",
                      proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_silent_hang_is_stall_killed_retried_then_terminal(self):
        """A codex that emits no bytes reaches the watchdog: it is terminated
        at the threshold, retried once (no side-effect-capable work was
        observed under a read-only policy), then reported terminally."""
        env = dict(self.env)
        env["CODEX_COUNCIL_STALL_SECS"] = "1"
        proc = self._launch(
            [_role("architect", "Architect", _instruction(f"Review {HANG_SENTINEL}"))],
            env=env, timeout=60)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("stall threshold reached", proc.stderr)
        self.assertIn("retriable error on attempt 1/2", proc.stderr)
        self.assertIn("[retriable:stall]", proc.stdout)
        self.assertIn("no side-effect-capable work was observed and the "
                      "role's policy permits replay", proc.stdout)
        self.assertNotIn("retrying", proc.stdout)
        self.assertRegex(
            self._last_nonempty_stderr_line(proc.stderr),
            r"CODEX_COUNCIL_DONE ok=0 total=1 elapsed=[\d.]+s exit=1 "
            r"version=\S+$",
        )

    def test_unreadable_lines_then_silence_is_a_terminal_stall(self):
        """A stdout line that does not decode could have been tool work,
        so a stall after it is terminal: one attempt, never a replay."""
        pid_dir = os.path.join(self.workdir.name, "pids")
        os.mkdir(pid_dir)
        env = dict(self.env, CODEX_COUNCIL_STALL_SECS="1",
                   FAKE_CODEX_PID_DIR=pid_dir)
        proc = self._launch(
            [_role("architect", "Architect", _instruction(
                f"Review {MALFORMED_LINES_SENTINEL} {SLEEP_SENTINEL}300"))],
            env=env, timeout=60)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(proc.stderr.count("stall threshold reached"), 1,
                         proc.stderr)
        self.assertNotIn("retriable error on attempt", proc.stderr)
        self.assertIn("[stall]", proc.stdout)
        self.assertNotIn("[retriable:stall]", proc.stdout)
        execs = [n for n in os.listdir(pid_dir) if n.startswith("exec-")]
        self.assertEqual(len(execs), 1, execs)


class ReadOnlyStateDirTests(CouncilCLITestCase):
    def test_reply_survives_unwritable_state_dir_with_warning(self):
        """The fake codex flips the state dir read-only mid-run, so the
        post-success save fails: the reply must still land in the report,
        the role stays ok, and the council exits 0."""
        state_root = os.path.join(self.statedir.name, "codex-council")
        self.addCleanup(lambda: os.path.isdir(state_root)
                        and os.chmod(state_root, 0o700))
        proc = self._launch([_role("architect", "Architect",
                                   _instruction(f"Review {CHMOD_STATE_SENTINEL}"))])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("fake reply from codex", proc.stdout)
        self.assertIn("_Warning:", proc.stdout)
        self.assertIn("could not be persisted", proc.stdout)
        self.assertRegex(
            self._last_nonempty_stderr_line(proc.stderr),
            r"CODEX_COUNCIL_DONE ok=1 total=1 elapsed=[\d.]+s exit=0 "
            r"version=\S+$",
        )


class ClosedStderrTests(CouncilCLITestCase):
    """stderr is advisory: its death must never change retries, results, or
    the exit code (never exit 120)."""

    def _write_role(self, instruction):
        path = os.path.join(self.workdir.name, "roles.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump([_role("architect", "Architect",
                             _instruction(instruction))], f)
        return path

    def _popen(self, roles_path, extra_env=None):
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        self._write_context("please review\n")
        self._prepare()
        lock = council_testlib.claim_supervisor_lock(self.workdir.name)
        try:
            return subprocess.Popen(
                council_testlib.supervised_argv(self.workdir.name, lock),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, env=env,
                cwd=self.workdir.name, pass_fds=(lock,),
            )
        finally:
            os.close(lock)

    @staticmethod
    def _close_pipes(proc):
        for stream in (proc.stdout, proc.stderr):
            with contextlib.suppress(Exception):
                stream.close()

    def _read_stderr_until(self, proc, pattern):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            line = proc.stderr.readline()
            if not line:
                break
            if re.search(pattern, line):
                return line
        self.fail(f"never saw {pattern!r} on stderr")

    def test_stderr_death_before_retry_notice_does_not_kill_the_retry(self):
        roles_path = self._write_role(f"Review {RETRY_ONCE_SENTINEL}")
        proc = self._popen(roles_path)
        try:
            self._read_stderr_until(proc, r"\[codex-council\] dispatching")
            proc.stderr.close()
            stdout = proc.stdout.read()
            self.assertEqual(proc.wait(timeout=60), 0)
        finally:
            proc.kill()
            self._close_pipes(proc)
        # The retry ran and succeeded even though its notice hit a dead pipe.
        self.assertIn("fake reply from codex", stdout)
        self.assertIn("(attempts: 2)", stdout)

    def test_stderr_death_after_progress_preserves_exit_zero(self):
        roles_path = self._write_role("Review")
        proc = self._popen(roles_path)
        try:
            self._read_stderr_until(proc, r"\[codex-council\] \d+/1 .*: ok")
            proc.stderr.close()
            stdout = proc.stdout.read()
            rc = proc.wait(timeout=60)
        finally:
            proc.kill()
            self._close_pipes(proc)
        self.assertEqual(rc, 0)  # never 120
        self.assertIn("# Codex Council", stdout)
        self.assertIn("fake reply from codex", stdout)

    def test_stderr_death_during_signal_teardown_preserves_exit_143(self):
        roles_path = self._write_role(f"Review {HANG_SENTINEL}")
        proc = self._popen(roles_path)
        try:
            self._read_stderr_until(proc, r"\[codex-council\] dispatching")
            proc.stderr.close()
            time.sleep(0.3)  # let the hanging role actually start
            proc.send_signal(signal.SIGTERM)
            rc = proc.wait(timeout=60)
        finally:
            proc.kill()
            self._close_pipes(proc)
        self.assertEqual(rc, 143)  # 128 + SIGTERM, not 120


if __name__ == "__main__":
    unittest.main()
