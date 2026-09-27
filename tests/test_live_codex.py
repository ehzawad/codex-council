"""Opt-in live smoke tests: the council against the REAL installed `codex`.

Every live class is skipped unless CODEX_COUNCIL_LIVE_TESTS=1, so the
default suite never touches the network, the signed-in account, or real
Codex (LiveGateTests always runs and proves the skip). Opted in, the tests
spend a handful of real turns on trivial prompts:

    CODEX_COUNCIL_LIVE_TESTS=1 python3 -m unittest tests.test_live_codex -v

Each test drives the runner's real CLI (--discover, --check-staging-dir,
and a launch with --roles-file/--context-file/--skill-contract) with a
temporary git repository as the project root, an isolated XDG_STATE_HOME,
and the real CODEX_HOME, and redirects stdout and stderr into files inside
a private run directory, the way the skill does. A `codex` shim first on
PATH appends each invocation's argv to a log and then execs the real
binary, so the runner still manages the real process while the tests see
which overrides were sent. Models and efforts come only from the live
discovery snapshot; the one explicit model is a synthetic id no catalog
carries. What Codex recorded is read back from the thread's rollout
($CODEX_HOME/sessions/**/rollout-*<thread>.jsonl turn_context entries).
That format is internal to Codex, so when no entry is found only that
check is skipped, with the reason. The smoke threads stay in
$CODEX_HOME/sessions like any other codex exec run. The codex --version
line, the account type, the chosen models, and the report lines Codex's
own messages produced are printed as `[live]` notes, never asserted; no
email address or token is ever printed.

Lives outside the plugin subtree so end-user installs don't bundle it.
"""

import collections
import contextlib
import glob
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.abspath(os.path.join(
    TESTS_DIR, "..", "plugins", "codex-council", "skills", "codex-council",
    "scripts",
))
sys.path.insert(0, SCRIPTS_DIR)

import codex_council  # noqa: E402

SCRIPT = os.path.join(SCRIPTS_DIR, "codex_council.py")
EPOCH = str(codex_council.SKILL_CONTRACT_EPOCH)
LIVE_ENV = "CODEX_COUNCIL_LIVE_TESTS"
LIVE_SKIP_REASON = (
    f"live Codex smoke tests are opt-in: set {LIVE_ENV}=1 to run them "
    "against the installed codex CLI and the signed-in account"
)
PROMPT = "Reply with exactly: OK. Do not run any commands or read any files."
# The role contract still applies: the scope phrase, then the cadence
# sentence last.
INSTRUCTION = [
    PROMPT,
    "This is a connectivity check, so there is nothing material to review.",
    "Thoroughness beats speed.",
]
# Synthetic, so no live catalog advertises it and Codex must reject it.
REJECTED_MODEL = "future-orion-2032"
ROUTED_REASON = "live smoke: a discovered model at its catalog default effort"
NATIVE_EFFORT_REASON = (
    "live smoke: the proven native model at its catalog default effort"
)
PIN_REASON = "live smoke: an explicit id no catalog advertises"
# Bounds one runner invocation; how long a real turn takes depends on the
# account's native model and effort.
INVOCATION_TIMEOUT_SECS = 900
# Where each CLI step's stdout and stderr land inside its run directory
# (the launch uses the names the skill uses).
STEP_OUTPUTS = {
    "discover": ("discover.out", "discover.err"),
    "preflight": ("preflight.out", "preflight.err"),
    "launch": ("out.md", "err.log"),
}
SELECTION_LINE = "[codex-council] model selection: "
NATIVE_ONLY_COUNTS = "native=1 user=0 routed=0 native_effort=0 fallback=0"
NOT_RUN = "discovery=not-run (no runtime-grounded selections)"
NATIVE_SECTION = (
    "_Model selection: native inheritance (no model or effort override "
    "sent)_"
)
# An address-shaped token (name@domain.tld). The snapshot's schema id
# "codex-council/model-snapshot@1" has no dotted domain, so it never matches.
EMAIL_RE = re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)+")
# Key fragments that would mean account identity reached the snapshot.
IDENTITY_KEY_FRAGMENTS = ("email", "plan", "token", "accountid", "account_id")
ROLLOUT_SKIP = (
    "no turn_context entry matches {pattern}; Codex's rollout format is "
    "internal, so only the recorded-model check is skipped"
)
ARGV_LOGGER = (
    "import json, sys\n"
    "with open(sys.argv[1], 'a', encoding='utf-8') as f:\n"
    "    f.write(json.dumps(sys.argv[2:]) + '\\n')\n"
)
# Runs in a fresh interpreter (see LiveGateTests): every live class through
# a real unittest suite, reported as JSON.
SKIP_PROBE = """\
import json
import unittest

import test_live_codex as live

suite = unittest.TestSuite(
    unittest.defaultTestLoader.loadTestsFromTestCase(cls)
    for cls in live._live_classes()
)
result = unittest.TestResult()
suite.run(result)
print(json.dumps({
    "tests_run": result.testsRun,
    "skipped": [[str(test), reason] for test, reason in result.skipped],
    "problems": [str(test) for test, _ in result.errors + result.failures],
}))
"""

CliRun = collections.namedtuple(
    "CliRun", "returncode stdout stderr codex_calls"
)


def _live_skip_reason(environ):
    """None when live tests are opted in (exactly "1"), else the reason."""
    return None if environ.get(LIVE_ENV) == "1" else LIVE_SKIP_REASON


def _live_classes():
    """Every live test class in this module; the gate covers them all."""
    return [
        value for value in globals().values()
        if isinstance(value, type) and issubclass(value, LiveCouncilCase)
        and value is not LiveCouncilCase
    ]


# ---------- pure helpers ----------

def _role(role_id, **fields):
    """One roles.json object with the smoke instruction; `fields` adds
    model, effort, and selection (none of them means native inheritance)."""
    return {"id": role_id, "label": "Live smoke", "instruction": INSTRUCTION,
            **fields}


def _automatic(mode, snapshot, reason):
    """A runtime-grounded selection bound to this run's snapshot."""
    return {"mode": mode, "snapshot_id": snapshot["snapshot_id"],
            "reason": reason}


def _tail(text, lines=40):
    return "\n".join(text.splitlines()[-lines:])


def _transcript(run):
    """Failure-message context for one CLI run: its output tails."""
    return (f"\n--- stderr tail ---\n{_tail(run.stderr)}"
            f"\n--- stdout tail ---\n{_tail(run.stdout)}")


def _line_starting(text, prefix):
    """The first line of text that starts with prefix, or None."""
    return next(
        (line for line in text.splitlines() if line.startswith(prefix)), None
    )


def _exec_calls(run):
    """The runner's `codex exec` worker argvs (it always puts -C first)."""
    return [argv for argv in run.codex_calls if argv[:2] == ["exec", "-C"]]


def _dispatchable(value):
    return (isinstance(value, str)
            and bool(codex_council.SELECTION_VALUE_PATTERN.match(value)))


def _catalog_default_effort(entry):
    """The entry's catalog default effort when it is also advertised in
    the entry's efforts and fits the roles grammar, else None."""
    effort = entry["default_effort"]
    advertised = any(option["effort"] == effort for option in entry["efforts"])
    return effort if advertised and _dispatchable(effort) else None


def _routed_choice(snapshot):
    """(model, effort) for a routed role, taken only from the snapshot: the
    first visible catalog entry that is not the proven native model and
    carries no upgrade or retirement notice, at its catalog default effort.
    None when the snapshot cannot ground a routed choice."""
    if snapshot["status"] != "ok" or not snapshot["routing"]["eligible"]:
        return None
    native = snapshot["native"]["model"]
    for entry in snapshot["catalog"]["models"]:
        if (entry["hidden"] or entry["upgrade"] is not None
                or entry["model"] == native
                or not _dispatchable(entry["model"])):
            continue
        effort = _catalog_default_effort(entry)
        if effort is not None:
            return entry["model"], effort
    return None


def _native_effort_choice(snapshot):
    """(native model, effort): the proven native model at its catalog
    default effort, or None when the snapshot proves no usable one."""
    native = snapshot["native"]["model"]
    if (snapshot["status"] != "ok"
            or snapshot["native"]["resolution"] != "proven"
            or not _dispatchable(native)):
        return None
    entry = next((entry for entry in snapshot["catalog"]["models"]
                  if entry["model"] == native), None)
    effort = _catalog_default_effort(entry) if entry else None
    return (native, effort) if effort else None


def _discovery_field(snapshot):
    """The model-selection line's discovery field for a launch discovery
    that agrees with this snapshot."""
    routing = snapshot["routing"]
    return "ok" if routing["eligible"] else f"ok ({routing['reasons'][0]})"


def _verdicts(snapshot):
    """A snapshot's verdicts in one line, for skip messages."""
    routing, native = snapshot["routing"], snapshot["native"]
    problems = ", ".join(snapshot["problems"]) or "none"
    reasons = "; ".join(routing["reasons"])
    return (f"status {snapshot['status']} (problems: {problems}); routing "
            f"{'eligible' if routing['eligible'] else reasons}; native "
            f"model {native['resolution']} ({native['reason'] or 'proven'})")


def _identity_keys(value, path="snapshot"):
    """Dotted paths of every key in value that names account identity."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            where = f"{path}.{key}"
            if any(part in key.lower() for part in IDENTITY_KEY_FRAGMENTS):
                found.append(where)
            found += _identity_keys(item, where)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += _identity_keys(item, f"{path}[{index}]")
    return found


# ---------- live environment (I/O) ----------

_NOTED = set()


def _note_once(label, value):
    """Print one fact about the live environment once per test run; never
    asserted. A callable value is evaluated only when printed, and any
    address-shaped text is redacted first."""
    if label in _NOTED:
        return
    _NOTED.add(label)
    if callable(value):
        value = value()
    value = EMAIL_RE.sub("<redacted>", str(value))
    print(f"\n[live] {label}: {value}", file=sys.stderr, flush=True)


def _read_text(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _read_argv_log(path):
    """The argvs the codex shim logged, in invocation order."""
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def _version_line(codex):
    """The first line `codex --version` prints, or why there is none."""
    try:
        proc = subprocess.run(
            [codex, "--version"], capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"unavailable ({type(e).__name__})"
    lines = proc.stdout.strip().splitlines()
    return lines[0] if lines else f"no output (exit {proc.returncode})"


def _install_codex_shim(bin_dir, real_codex, argv_log):
    """Write a `codex` into bin_dir that appends its argv (one JSON list
    per line) to argv_log and then execs the real binary, which keeps the
    pid, process group, and stdio the runner gave the shim."""
    logger = os.path.join(bin_dir, "log_argv.py")
    with open(logger, "w", encoding="utf-8") as f:
        f.write(ARGV_LOGGER)
    shim = os.path.join(bin_dir, "codex")
    with open(shim, "w", encoding="utf-8") as f:
        f.write(
            "#!/bin/sh\n"
            f"{shlex.quote(sys.executable)} {shlex.quote(logger)} "
            f'{shlex.quote(argv_log)} "$@" || exit 97\n'
            f'exec {shlex.quote(real_codex)} "$@"\n'
        )
    os.chmod(shim, 0o755)


def _codex_home():
    """The CODEX_HOME the runner's codex processes inherit."""
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def _rollout_pattern(thread_id):
    sessions = glob.escape(os.path.join(_codex_home(), "sessions"))
    return os.path.join(
        sessions, "**", f"rollout-*{glob.escape(thread_id)}.jsonl"
    )


def _recorded_turns(thread_id):
    """(model, effort) of each turn_context Codex recorded for a thread,
    oldest first (a resumed thread appends to its rollout). The rollout is
    Codex's internal format, so a missing file or entry gives []."""
    turns = []
    paths = sorted(glob.glob(_rollout_pattern(thread_id), recursive=True))
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if (not isinstance(record, dict)
                        or record.get("type") != "turn_context"):
                    continue
                payload = record.get("payload")
                if isinstance(payload, dict):
                    turns.append((payload.get("model"), payload.get("effort")))
    return turns


class LiveGateTests(unittest.TestCase):
    """Always runs: the default suite never reaches real Codex."""

    def test_only_an_exact_1_opts_in(self):
        self.assertIsNone(_live_skip_reason({LIVE_ENV: "1"}))
        for value in (None, "", "0", "true", "yes", " 1", "1 "):
            environ = {} if value is None else {LIVE_ENV: value}
            with self.subTest(value=value):
                self.assertEqual(_live_skip_reason(environ), LIVE_SKIP_REASON)

    def test_live_classes_skip_without_starting_codex(self):
        """Every live class, run in a fresh interpreter without the opt-in
        and with a tripwire `codex` first on PATH, is skipped with the
        reason before any test body runs, and codex never starts."""
        classes = sorted(cls.__name__ for cls in _live_classes())
        self.assertTrue(classes)
        with tempfile.TemporaryDirectory() as tmp:
            tripped = os.path.join(tmp, "codex-was-started")
            tripwire = os.path.join(tmp, "codex")
            with open(tripwire, "w", encoding="utf-8") as f:
                f.write(f"#!/bin/sh\n: > {shlex.quote(tripped)}\nexit 97\n")
            os.chmod(tripwire, 0o755)
            env = {key: value for key, value in os.environ.items()
                   if key != LIVE_ENV}
            env["PATH"] = tmp + os.pathsep + env.get("PATH", "")
            proc = subprocess.run(
                [sys.executable, "-c", SKIP_PROBE], cwd=TESTS_DIR, env=env,
                capture_output=True, text=True, stdin=subprocess.DEVNULL,
                timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(os.path.exists(tripped))
        outcome = json.loads(proc.stdout)
        self.assertEqual(outcome["tests_run"], 0)
        self.assertEqual(outcome["problems"], [])
        # A setUpClass skip reads "setUpClass (test_live_codex.<Class>)".
        skipped = sorted(description.rpartition(".")[2].rstrip(")")
                         for description, _ in outcome["skipped"])
        self.assertEqual(skipped, classes)
        self.assertEqual({reason for _, reason in outcome["skipped"]},
                         {LIVE_SKIP_REASON})


class LiveCouncilCase(unittest.TestCase):
    """Base for the live classes. The opt-in is checked in setUpClass, so
    a skipped class creates no fixture and starts no process; each test
    then gets its own temporary git repository, XDG_STATE_HOME, codex
    shim, and run directories."""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        reason = _live_skip_reason(os.environ)
        if reason:
            raise unittest.SkipTest(reason)
        real_codex = shutil.which("codex")
        if real_codex is None:
            raise RuntimeError(f"{LIVE_ENV}=1 but no `codex` is on PATH")
        cls.real_codex = os.path.abspath(real_codex)
        _note_once("codex --version", lambda: _version_line(cls.real_codex))

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="codex-council-live-")
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        # The project root, never this repository.
        self.repo = os.path.join(self.tmp, "repo")
        subprocess.run(["git", "init", "-q", self.repo], check=True,
                       capture_output=True, timeout=60)
        self.state_home = os.path.join(self.tmp, "state")
        os.mkdir(self.state_home, 0o700)
        bin_dir = os.path.join(self.tmp, "bin")
        os.mkdir(bin_dir)
        self.argv_log = os.path.join(self.tmp, "codex-argv.jsonl")
        _install_codex_shim(bin_dir, self.real_codex, self.argv_log)
        # The real environment (CODEX_HOME included) minus every council
        # override, so routing runs in its default "auto" mode.
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("CODEX_COUNCIL_")}
        self.env["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
        self.env["XDG_STATE_HOME"] = self.state_home

    # ----- the runner's real CLI -----

    def new_run_dir(self):
        """A private run directory, as `mktemp -d` makes one."""
        return tempfile.mkdtemp(prefix="run.", dir=self.tmp)

    def invoke(self, run_dir, step, *args):
        """Run the real runner once from the temporary repository, with
        stdout and stderr redirected into run_dir (see STEP_OUTPUTS);
        return a CliRun carrying the codex argvs logged meanwhile."""
        with contextlib.suppress(FileNotFoundError):
            os.remove(self.argv_log)
        out_path, err_path = (
            os.path.join(run_dir, name) for name in STEP_OUTPUTS[step]
        )
        with open(out_path, "wb") as out, open(err_path, "wb") as err:
            proc = subprocess.Popen(
                [sys.executable, SCRIPT, *args], stdin=subprocess.DEVNULL,
                stdout=out, stderr=err, env=self.env, cwd=self.repo,
            )
            try:
                returncode = proc.wait(timeout=INVOCATION_TIMEOUT_SECS)
            except subprocess.TimeoutExpired:
                # SIGTERM makes the runner tear down every codex process
                # group it started; a bare SIGKILL would orphan them.
                proc.terminate()
                try:
                    proc.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                returncode = None
        run = CliRun(returncode, _read_text(out_path), _read_text(err_path),
                     _read_argv_log(self.argv_log))
        if returncode is None:
            self.fail(f"{step} did not finish within "
                      f"{INVOCATION_TIMEOUT_SECS}s{_transcript(run)}")
        return run

    def discover(self, run_dir):
        """--discover run_dir; return (the planning snapshot, the CliRun)."""
        run = self.invoke(run_dir, "discover", "--discover", run_dir,
                          "--skill-contract", EPOCH)
        self.assertEqual(run.returncode, 0, _transcript(run))
        snapshot, problem = codex_council._read_snapshot(run_dir)
        self.assertIsNone(problem, _transcript(run))
        _note_once("account type",
                   snapshot["account"]["type"] or "none (signed out)")
        return snapshot, run

    def council(self, run_dir, *roles):
        """Stage roles and the smoke context in run_dir, pre-flight them,
        then launch; return (the preflight stdout, the launch CliRun)."""
        roles_path = os.path.join(run_dir, "roles.json")
        context_path = os.path.join(run_dir, "context.md")
        with open(roles_path, "w", encoding="utf-8") as f:
            json.dump(list(roles), f, indent=2)
        with open(context_path, "w", encoding="utf-8") as f:
            f.write(PROMPT + "\n")
        preflight = self.invoke(run_dir, "preflight", "--check-staging-dir",
                                run_dir, "--skill-contract", EPOCH)
        self.assertEqual(preflight.returncode, 0, _transcript(preflight))
        launch = self.invoke(run_dir, "launch", "--roles-file", roles_path,
                             "--context-file", context_path,
                             "--skill-contract", EPOCH)
        return preflight.stdout, launch

    # ----- assertions -----

    def assert_settled(self, run, role_id, ok=True):
        """The one-role launch finished with this outcome, and its report
        and CODEX_COUNCIL_DONE sentinel agree."""
        exit_code = 0 if ok else 1
        self.assertEqual(run.returncode, exit_code, _transcript(run))
        self.assertRegex(
            run.stderr,
            rf"\[codex-council\] CODEX_COUNCIL_DONE ok={int(ok)} total=1 "
            rf"elapsed=[\d.]+s exit={exit_code} ",
        )
        status = "ok" if ok else "FAILED"
        self.assertIn(f" [{role_id}]: {status}", run.stdout)

    def assert_selection_line(self, run, expected):
        self.assertEqual(_line_starting(run.stderr, SELECTION_LINE),
                         SELECTION_LINE + expected, _transcript(run))

    def only_exec_call(self, run):
        """The launch's single `codex exec` argv (no retry, no substitute)."""
        calls = _exec_calls(run)
        self.assertEqual(len(calls), 1, calls)
        return calls[0]

    def assert_no_overrides(self, argv):
        self.assertNotIn("-m", argv)
        self.assertNotIn("-c", argv)
        self.assertFalse(any("model_reasoning_effort" in arg for arg in argv),
                         argv)

    def assert_sent(self, argv, model, effort):
        self.assertEqual(argv.count("-m"), 1, argv)
        self.assertEqual(argv[argv.index("-m") + 1], model, argv)
        self.assertEqual(argv.count("-c"), 1, argv)
        self.assertEqual(argv[argv.index("-c") + 1],
                         f'model_reasoning_effort="{effort}"', argv)

    def assert_resumed(self, run, argv, role_id, thread_id):
        self.assertIn(f"[codex-council] {role_id}: started (resume)",
                      run.stderr)
        self.assertEqual(argv[argv.index("resume") + 1], thread_id, argv)

    def saved_threads(self, role_id):
        """Thread ids of every continuity state file saved for role_id."""
        pattern = os.path.join(glob.escape(self.state_home), "codex-council",
                               f"*__{role_id}.json")
        threads = []
        for path in sorted(glob.glob(pattern)):
            with open(path, encoding="utf-8") as f:
                threads.append(json.load(f)["session_id"])
        return threads

    def only_saved_thread(self, role_id):
        threads = self.saved_threads(role_id)
        self.assertEqual(len(threads), 1, threads)
        return threads[0]

    def skip_check(self, check, reason):
        """Skip one named check of the running test, not the whole test."""
        with self.subTest(check=check):
            self.skipTest(reason)

    def assert_recorded(self, thread_id, model, effort=None, since=0,
                        turns=1):
        """Codex recorded at least `turns` turn contexts for thread_id after
        its first `since`, all on `model` (and on `effort` unless None).
        Returns how many it has recorded in all. Only this check is skipped,
        with the reason, when the rollout has no turn_context entry."""
        recorded = _recorded_turns(thread_id)
        with self.subTest(check="rollout turn_context", thread=thread_id):
            if not recorded:
                self.skipTest(ROLLOUT_SKIP.format(
                    pattern=_rollout_pattern(thread_id)))
            new = recorded[since:]
            self.assertGreaterEqual(len(new), turns, recorded)
            self.assertEqual({m for m, _ in new}, {model}, recorded)
            if effort is not None:
                self.assertEqual({e for _, e in new}, {effort}, recorded)
        return len(recorded)

    def assert_native_recorded(self, snapshot, thread_id, since=0, turns=1):
        """assert_recorded against the snapshot's proven native model and
        configured effort (no effort check when none is configured)."""
        native = snapshot["native"]
        if native["resolution"] != "proven":
            self.skip_check(
                "rollout turn_context",
                f"discovery proved no native model ({native['reason']}), so "
                "no recorded model can be expected",
            )
            return since
        return self.assert_recorded(
            thread_id, native["model"], snapshot["configured"]["effort"],
            since, turns,
        )


class LiveDiscoveryTests(LiveCouncilCase):
    """--discover against the real app-server: metadata only, no identity."""

    def test_real_snapshot_keeps_account_identity_out(self):
        run_dir = self.new_run_dir()
        snapshot, run = self.discover(run_dir)
        path = os.path.join(run_dir, codex_council.SNAPSHOT_FILENAME)
        self.assertEqual(snapshot["status"], "ok", snapshot["problems"])
        self.assertTrue(run.stdout.startswith(
            "[codex-council] discovery ok: snapshot_id="
            f"{snapshot['snapshot_id']} "), _transcript(run))
        self.assertEqual(run.stdout.splitlines()[-1], f"snapshot: {path}")
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o600)
        # The temporary repository, never this one, is the project root.
        self.assertEqual(snapshot["context"]["project_root"],
                         os.path.realpath(self.repo))
        self.assertEqual(sorted(snapshot["account"]),
                         ["requires_openai_auth", "type"])
        self.assertEqual(_identity_keys(snapshot), [])
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        for name, text in (("snapshot", raw), ("stdout", run.stdout),
                           ("stderr", run.stderr)):
            with self.subTest(output=name):
                # Booleans only: a failure message must never echo the text.
                self.assertFalse(bool(EMAIL_RE.search(text)),
                                 f"an email address reached the {name}")
                self.assertFalse("planType" in text,
                                 f"the plan type reached the {name}")


class LiveInheritanceTests(LiveCouncilCase):
    """A role without model, effort, or selection sends no override."""

    def test_inherited_role_sends_no_override_fresh_then_resume(self):
        role_id = "live-inherit"
        run_dir = self.new_run_dir()
        snapshot, _ = self.discover(run_dir)
        plan, fresh = self.council(run_dir, _role(role_id))
        self.assertIn(
            f"[codex-council] selection plan: {role_id}: native inheritance\n",
            plan,
        )
        self.assert_settled(fresh, role_id)
        self.assertIn(f"[codex-council] {role_id}: started (fresh)",
                      fresh.stderr)
        self.assert_selection_line(
            fresh, f"routing=auto; {NOT_RUN}; {NATIVE_ONLY_COUNTS}")
        self.assertIn(NATIVE_SECTION, fresh.stdout)
        argv = self.only_exec_call(fresh)
        self.assertNotIn("resume", argv)
        self.assert_no_overrides(argv)
        thread = self.only_saved_thread(role_id)

        _, resumed = self.council(self.new_run_dir(), _role(role_id))
        self.assert_settled(resumed, role_id)
        argv = self.only_exec_call(resumed)
        self.assert_resumed(resumed, argv, role_id, thread)
        self.assert_no_overrides(argv)
        self.assertIn(NATIVE_SECTION, resumed.stdout)
        self.assertEqual(self.saved_threads(role_id), [thread])
        # Both turns ran on the native configuration discovery reported.
        self.assert_native_recorded(snapshot, thread, turns=2)


class LiveRoutingTests(LiveCouncilCase):
    """Runtime-grounded selections, chosen only from the live snapshot."""

    def routed_choice(self, snapshot):
        choice = _routed_choice(snapshot)
        if choice is None:
            self.skipTest("live discovery grounds no routed choice: "
                          + _verdicts(snapshot))
        _note_once("routed choice",
                   f"model {choice[0]} at its catalog default effort "
                   f"{choice[1]}")
        return choice

    def test_routed_pair_reaches_codex(self):
        role_id = "live-routed"
        run_dir = self.new_run_dir()
        snapshot, _ = self.discover(run_dir)
        model, effort = self.routed_choice(snapshot)
        plan, run = self.council(run_dir, _role(
            role_id, model=model, effort=effort,
            selection=_automatic("routed", snapshot, ROUTED_REASON)))
        self.assertIn(
            f"[codex-council] selection plan: {role_id}: routed (model "
            f"{model}, effort {effort}); revalidated at launch\n", plan)
        self.assert_settled(run, role_id)
        self.assert_selection_line(
            run, "routing=auto; discovery=ok; native=0 user=0 routed=1 "
                 "native_effort=0 fallback=0")
        self.assertIn(f"[{role_id}]: ok (routed: model {model}, effort "
                      f"{effort})", run.stdout)
        self.assertIn(f"_Model selection: routed — sent model {model}, "
                      f"effort {effort}; reason: {ROUTED_REASON}_", run.stdout)
        self.assertIn("Model selection: launch discovery ok (codex-cli ",
                      run.stdout)
        self.assert_sent(self.only_exec_call(run), model, effort)
        self.assert_recorded(self.only_saved_thread(role_id), model, effort)

    def test_native_effort_pins_the_proven_native_model(self):
        role_id = "live-native-effort"
        run_dir = self.new_run_dir()
        snapshot, _ = self.discover(run_dir)
        choice = _native_effort_choice(snapshot)
        if choice is None:
            self.skipTest("live discovery proves no native model to adjust: "
                          + _verdicts(snapshot))
        native, effort = choice
        _note_once("native-effort choice",
                   f"effort {effort} (catalog default) on native model "
                   f"{native}")
        plan, run = self.council(run_dir, _role(
            role_id, effort=effort,
            selection=_automatic("native_effort", snapshot,
                                 NATIVE_EFFORT_REASON)))
        self.assertIn(
            f"[codex-council] selection plan: {role_id}: native-model effort "
            f"(effort {effort} on native model {native}); revalidated at "
            "launch\n", plan)
        self.assert_settled(run, role_id)
        self.assert_selection_line(
            run, f"routing=auto; discovery={_discovery_field(snapshot)}; "
                 "native=0 user=0 routed=0 native_effort=1 fallback=0")
        self.assertIn(f"[{role_id}]: ok (routed effort: {effort} on native "
                      f"model {native})", run.stdout)
        self.assertIn(
            "_Model selection: routed effort on the native model — sent "
            f"model {native} (pinned native model), effort {effort}; "
            f"reason: {NATIVE_EFFORT_REASON}_", run.stdout)
        self.assert_sent(self.only_exec_call(run), native, effort)
        self.assert_recorded(self.only_saved_thread(role_id), native, effort)

    def test_inherited_resume_of_a_routed_thread_warns_with_codex_advisory(
            self):
        role_id = "live-advisory"
        run_dir = self.new_run_dir()
        snapshot, _ = self.discover(run_dir)
        model, effort = self.routed_choice(snapshot)
        native = snapshot["native"]
        if native["resolution"] != "proven":
            self.skipTest("the model a resume without overrides runs on is "
                          f"unknown: {_verdicts(snapshot)}")
        _, routed = self.council(run_dir, _role(
            role_id, model=model, effort=effort,
            selection=_automatic("routed", snapshot, ROUTED_REASON)))
        self.assert_settled(routed, role_id)
        thread = self.only_saved_thread(role_id)
        before = self.assert_recorded(thread, model, effort)

        run_dir = self.new_run_dir()
        _, resumed = self.council(run_dir, _role(role_id))
        self.assert_settled(resumed, role_id)
        argv = self.only_exec_call(resumed)
        self.assert_resumed(resumed, argv, role_id, thread)
        self.assert_no_overrides(argv)
        self.assertEqual(self.saved_threads(role_id), [thread])
        # Codex's own advisory, kept verbatim as the role's warning.
        self.assertIn(f"[{role_id}]: ok — WARNING", resumed.stdout)
        warning = _line_starting(resumed.stdout, "_Warning: ")
        self.assertIsNotNone(warning, _transcript(resumed))
        _note_once("resume advisory in the report", warning)
        for fragment in ("codex reported: ", "recorded with model",
                         f"`{model}`", f"`{native['model']}`"):
            self.assertIn(fragment, warning)
        with open(os.path.join(run_dir, "replies", f"{role_id}.md"),
                  encoding="utf-8") as f:
            self.assertIn(" selection=native warning=yes -->", f.readline())
        self.assert_native_recorded(snapshot, thread, since=before)


class LiveRejectionTests(LiveCouncilCase):
    """An explicit pin Codex rejects fails once and keeps the thread."""

    def test_rejected_explicit_pin_keeps_the_saved_thread(self):
        role_id = "live-rejected"
        run_dir = self.new_run_dir()
        self.discover(run_dir)
        _, first = self.council(run_dir, _role(role_id))
        self.assert_settled(first, role_id)
        thread = self.only_saved_thread(role_id)

        run_dir = self.new_run_dir()
        snapshot, _ = self.discover(run_dir)
        plan, rejected = self.council(run_dir, _role(
            role_id, model=REJECTED_MODEL,
            selection={"mode": "user", "reason": PIN_REASON}))
        expected_plan = (f"[codex-council] selection plan: {role_id}: "
                         f"explicit override (model {REJECTED_MODEL})")
        if snapshot["status"] == "ok":
            expected_plan += f"; {codex_council.UNVERIFIED_MODEL_ADVISORY}"
        self.assertIn(expected_plan, plan)
        self.assert_settled(rejected, role_id, ok=False)
        self.assert_selection_line(
            rejected, f"routing=auto; {NOT_RUN}; native=0 user=1 routed=0 "
                      "native_effort=0 fallback=0")
        self.assertIn(f"[{role_id}]: FAILED (explicit: model "
                      f"{REJECTED_MODEL})", rejected.stdout)
        failure = _line_starting(rejected.stdout, "_Failed: ")
        self.assertIsNotNone(failure, _transcript(rejected))
        _note_once("rejection in the report", failure)
        self.assertTrue(failure.startswith(
            "_Failed: [model-rejected] Codex rejected the requested model "
            f"'{REJECTED_MODEL}' for this invocation: "), failure)
        self.assertTrue(failure.endswith(
            "No substitute model was tried and the saved thread was kept. "
            "Change or remove the explicit pin._"), failure)
        # One invocation on the saved thread: never retried or replaced.
        argv = self.only_exec_call(rejected)
        self.assert_resumed(rejected, argv, role_id, thread)
        self.assertEqual(argv[argv.index("-m") + 1], REJECTED_MODEL, argv)
        self.assertNotIn("-c", argv)
        self.assertNotIn("retriable error", rejected.stderr)
        self.assertNotIn("is stale", rejected.stderr)
        self.assertEqual(self.saved_threads(role_id), [thread])
        before = len(_recorded_turns(thread))

        _, follow_up = self.council(self.new_run_dir(), _role(role_id))
        self.assert_settled(follow_up, role_id)
        # Codex may record the rejected turn on the thread; whatever it then
        # says on resume is shown, not asserted.
        warning = _line_starting(follow_up.stdout, "_Warning: ")
        if warning:
            _note_once("follow-up warning after the rejection", warning)
        argv = self.only_exec_call(follow_up)
        self.assert_resumed(follow_up, argv, role_id, thread)
        self.assert_no_overrides(argv)
        self.assertEqual(self.saved_threads(role_id), [thread])
        self.assert_native_recorded(snapshot, thread, since=before)


if __name__ == "__main__":
    unittest.main()
