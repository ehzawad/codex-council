"""End-to-end CLI tests for codex_council.py.

Black-box: drives the REAL codex_council.py as a subprocess with a FAKE
`codex` executable on PATH (no network, no real Codex). Each test builds an
isolated env — a TemporaryDirectory holding a generated `codex` script
prepended to PATH, and an XDG_STATE_HOME pointed at another temp dir so no
real council state is touched.

Asserts exit codes AND stream contents (stdout = the report only; stderr =
dispatch line, per-role progress, and the final CODEX_COUNCIL_DONE sentinel).

Lives outside the plugin subtree so end-user installs don't bundle it.
Run from repo root:
    python3 -m unittest discover -s tests -p 'test_*.py'
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

SCRIPT = os.path.abspath(os.path.join(
    os.path.dirname(__file__),
    "..",
    "plugins", "codex-council", "skills", "codex-council", "scripts",
    "codex_council.py",
))

# Roles whose instruction contains this token make the fake codex exit
# non-zero with no agent_message, so that role is reported FAILED.
FAIL_SENTINEL = "PLEASE_FAIL"

# A fake `codex` executable. It reads its whole stdin (the bookended prompt),
# and if the FAIL_SENTINEL appears it exits non-zero with a stderr message and
# NO agent_message; otherwise it emits a thread.started + agent_message JSONL
# pair on stdout, exactly what the parser expects.
FAKE_CODEX = textwrap.dedent(
    f"""\
    #!/usr/bin/env python3
    import sys, json, uuid

    prompt = sys.stdin.read()

    if {FAIL_SENTINEL!r} in prompt:
        sys.stderr.write("fake codex: simulated role failure\\n")
        sys.exit(3)

    tid = "thread-" + uuid.uuid4().hex[:12]
    sys.stdout.write(json.dumps({{"type": "thread.started", "thread_id": tid}}) + "\\n")
    sys.stdout.write(json.dumps({{
        "type": "item.completed",
        "item": {{"type": "agent_message", "text": "fake reply from codex"}},
    }}) + "\\n")
    sys.exit(0)
    """
)


def _role(rid, label, instruction):
    return {"id": rid, "label": label, "instruction": instruction}


class CouncilCLITestCase(unittest.TestCase):
    """Base: each test gets a fresh fake-codex-on-PATH env and temp state dir."""

    def setUp(self):
        self.bindir = tempfile.TemporaryDirectory()
        self.addCleanup(self.bindir.cleanup)
        self.statedir = tempfile.TemporaryDirectory()
        self.addCleanup(self.statedir.cleanup)
        self.workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.workdir.cleanup)

        fake = os.path.join(self.bindir.name, "codex")
        with open(fake, "w", encoding="utf-8") as f:
            f.write(FAKE_CODEX)
        os.chmod(fake, 0o755)

        self.env = dict(os.environ)
        self.env["PATH"] = self.bindir.name + os.pathsep + self.env.get("PATH", "")
        self.env["XDG_STATE_HOME"] = self.statedir.name
        # Keep the env free of any session-key leakage from the dev shell.
        self.env.pop("CODEX_COUNCIL_SESSION_KEY", None)

    def _write_roles(self, roles):
        path = os.path.join(self.workdir.name, "roles.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(roles, f)
        return path

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
        # Non-empty context piped, but NO --roles-file → usage error, exit 2.
        proc = self._run(input="some context here\n")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        # Not just the code — the safety-net message must survive too.
        self.assertIn("No roles requested", proc.stderr)


class HappyPathTests(CouncilCLITestCase):
    def test_happy_path_report_and_progress(self):
        roles_path = self._write_roles([
            _role("architect", "Architect",
                  "Review architecture. Thoroughness beats speed."),
            _role("security", "Security",
                  "Review security. Thoroughness beats speed."),
        ])
        proc = self._run(
            input="please review this change\n",
            args=("--roles-file", roles_path),
        )
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
        self.assertRegex(
            self._last_nonempty_stderr_line(proc.stderr),
            r"\[codex-council\] CODEX_COUNCIL_DONE ok=2 total=2 "
            r"elapsed=[\d.]+s exit=0$",
        )

    def test_report_precedes_sentinel_in_combined_stream(self):
        # Recovery contract (R2): CODEX_COUNCIL_DONE must appear only AFTER the
        # full report is written, so "tail err.log shows the sentinel" reliably
        # means "out.md is complete." Merge stdout+stderr into one stream so the
        # relative byte order of report vs sentinel is observable, then assert it.
        roles_path = self._write_roles([
            _role("architect", "Architect",
                  "Review architecture. Thoroughness beats speed."),
            _role("security", "Security",
                  "Review security. Thoroughness beats speed."),
        ])
        proc = self._run_merged(
            input="please review this change\n",
            args=("--roles-file", roles_path),
        )
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


class StdinGuardTests(CouncilCLITestCase):
    def test_over_cap_stdin_exits_1(self):
        roles_path = self._write_roles([
            _role("architect", "Architect",
                  "Review. Thoroughness beats speed."),
        ])
        oversize = "a" * (10 * 1024 * 1024 + 1)
        proc = self._run(input=oversize, args=("--roles-file", roles_path))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("exceeds", proc.stderr)
        # Fake codex never ran, so no report / no sentinel.
        self.assertNotIn("CODEX_COUNCIL_DONE", proc.stderr)
        self.assertNotIn("# Codex Council", proc.stdout)

    def test_empty_stdin_exits_1(self):
        roles_path = self._write_roles([
            _role("architect", "Architect",
                  "Review. Thoroughness beats speed."),
        ])
        proc = self._run(input="   \n ", args=("--roles-file", roles_path))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("Empty input", proc.stderr)


class FailureReportingTests(CouncilCLITestCase):
    def test_mixed_failure_reports_and_exits_0(self):
        roles_path = self._write_roles([
            _role("architect", "Architect",
                  "Review architecture. Thoroughness beats speed."),
            _role("security", "Security",
                  f"Review security {FAIL_SENTINEL}. Thoroughness beats speed."),
        ])
        proc = self._run(
            input="please review this change\n",
            args=("--roles-file", roles_path),
        )
        # One success → exit 0.
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # The failing role shows FAILED in the report — anchored to the
        # summary-line shape so a regression in that line is caught.
        self.assertRegex(proc.stdout, r"(?m)^- \*\*Security\*\* \[security\]: FAILED\b")
        self.assertRegex(
            self._last_nonempty_stderr_line(proc.stderr),
            r"\[codex-council\] CODEX_COUNCIL_DONE ok=1 total=2 "
            r"elapsed=[\d.]+s exit=0$",
        )

    def test_all_roles_fail_exits_1(self):
        roles_path = self._write_roles([
            _role("architect", "Architect",
                  f"Review {FAIL_SENTINEL}. Thoroughness beats speed."),
            _role("security", "Security",
                  f"Review {FAIL_SENTINEL}. Thoroughness beats speed."),
        ])
        proc = self._run(
            input="please review this change\n",
            args=("--roles-file", roles_path),
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertRegex(
            self._last_nonempty_stderr_line(proc.stderr),
            r"\[codex-council\] CODEX_COUNCIL_DONE ok=0 total=2 "
            r"elapsed=[\d.]+s exit=1$",
        )


if __name__ == "__main__":
    unittest.main()
