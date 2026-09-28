"""Shared helpers for the codex-council test modules.

Not a test module itself (the name does not match test*.py); test files
import it (unittest discover puts tests/ on sys.path). It holds what more
than one test module needs: the runner's path and contract epoch, the
usage-exit assertion, one clean environment, the process checks that prove teardown left nothing behind,
the synthetic discovery observations and snapshot builder, the per-module
fake `codex` install, and the discovery-methods check with its independent
FORBIDDEN_METHODS and login guards. The fake itself, and
its DISCOVERY_METHODS allowlist, live in fake_codex.py.
"""

import contextlib
import io
import os
import signal
import subprocess
import sys
import tempfile
import time

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.abspath(os.path.join(
    TESTS_DIR, "..", "plugins", "codex-council", "skills", "codex-council",
    "scripts",
))
for _path in (SCRIPTS_DIR, TESTS_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import codex_council  # noqa: E402
import council_discovery  # noqa: E402
import fake_codex  # noqa: E402

SCRIPT = os.path.join(SCRIPTS_DIR, "codex_council.py")
EPOCH = str(codex_council.SKILL_CONTRACT_EPOCH)
# What unit tests patch _project_root to, so state keys are deterministic.
FIXED_PROJECT_ROOT = "/fixed/project/root"
SNAPSHOT_ID = "0123456789abcdef"


def assert_usage_exit(test, callable_, *, expect_in_stderr):
    """Run callable_; assert SystemExit(2) with expect_in_stderr on stderr.

    Returns the captured stderr.
    """
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        with test.assertRaises(SystemExit) as ctx:
            callable_()
    test.assertEqual(ctx.exception.code, 2)
    test.assertIn(expect_in_stderr, buf.getvalue())
    return buf.getvalue()


# Variables that change discovery or council verdicts; clean_env drops them
# (and every FAKE_CODEX_* setting) so the caller's shell cannot leak in.
_VERDICT_ENV = frozenset({
    "CODEX_API_KEY", council_discovery.MODEL_ROUTING_ENV,
    codex_council.SESSION_KEY_ENV, codex_council.MAX_PARALLEL_ENV,
    codex_council.STALL_SECS_ENV,
})


def clean_env(**extra):
    """os.environ minus anything that changes council or discovery
    verdicts, plus `extra`."""
    env = {
        key: value for key, value in os.environ.items()
        if key not in _VERDICT_ENV and not key.startswith("FAKE_CODEX_")
    }
    env.update(extra)
    return env


# ---------- processes the fake codex leaves behind ----------

def pid_running(pid):
    """False once pid has exited; a zombie awaiting its reaper is dead."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            return f.read().rpartition(")")[2].split()[0] != "Z"
    except OSError:
        pass
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                           capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def pid_gone(pid, timeout=5.0):
    """True once pid is no longer running (polls up to `timeout`)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_running(pid):
            return True
        time.sleep(0.02)
    return False


def kill_quietly(pid):
    """SIGKILL pid if it is still a fake codex process (a test cleanup).

    The command-line check keeps a recycled pid from ever being signalled.
    """
    command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True).stdout
    if "fake_codex_impl.py" in command:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def default_signal_dispositions():
    """preexec_fn: SIGINT, SIGTERM, and SIGHUP back to their defaults in
    the child, so a signal test also works when the suite itself runs with
    one ignored (a background job, nohup)."""
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, signal.SIG_DFL)


# ---------- synthetic snapshots (built by the real snapshot builder) ----------

def catalog(entries):
    """A catalog accumulator built from wire entries by the real helpers."""
    built = council_discovery._new_catalog()
    page, problem = council_discovery._normalize_model_page({"data": entries})
    assert problem is None, problem
    council_discovery._merge_model_page(built, page, [])
    return built


def observed(**overrides):
    """What a conclusive discovery observed (the default catalog and a
    user-configured native model); keyword args replace whole fields."""
    found = {
        "context": dict(
            council_discovery._EMPTY_DISCOVERY_CONTEXT, project_root="/proj",
            launch_cwd="/proj", codex_executable="/bin/codex",
            codex_cli_version="9.9.9", codex_home="/home/.codex",
        ),
        "problems": [],
        "conclusive": True,
        "account": {"type": "chatgpt", "requires_openai_auth": True},
        "configured": {"model": fake_codex.NATIVE_MODEL,
                       "effort": fake_codex.NATIVE_EFFORT,
                       "provider": None, "model_origin": "user",
                       "effort_origin": "user", "endpoint_overrides": [],
                       "catalog_override": False},
        "managed": {"status": "absent", "model": None, "effort": None,
                    "provider_keys": []},
    }
    found.update(overrides)
    if "catalog" not in found:
        found["catalog"] = catalog(fake_codex.default_catalog())
    return found


def snapshot(snapshot_id=SNAPSHOT_ID, routing_mode="auto", **overrides):
    """A discovery snapshot from observed(**overrides), at a fixed time."""
    return council_discovery._build_snapshot(
        snapshot_id=snapshot_id, created_at="2026-09-27T12:00:00Z",
        plugin_version="9.8.7", routing_mode=routing_mode,
        **observed(**overrides),
    )


# ---------- the fake codex, installed once per test module ----------

# The fake is stateless (the scenario and logs come from env vars), and
# macOS charges a noticeable first-exec cost for every newly written
# executable, so a module installs it once: assign setUpModule =
# install_fake_codex and tearDownModule = remove_fake_codex, then put
# fake_bin_dir() first on the PATH the code under test sees.
_FAKE_BIN = {}


def install_fake_codex():
    _FAKE_BIN["tmp"] = tempfile.TemporaryDirectory()
    fake_codex.install(_FAKE_BIN["tmp"].name)


def remove_fake_codex():
    _FAKE_BIN.pop("tmp").cleanup()


def fake_bin_dir():
    """The directory holding the installed fake `codex`."""
    return _FAKE_BIN["tmp"].name


# Methods discovery must never send (inference). Kept here, apart from
# fake_codex.DISCOVERY_METHODS, so that an edit to the allowlist cannot
# quietly admit them.
FORBIDDEN_METHODS = ("thread/start", "thread/resume", "turn/start")


def assert_only_discovery_methods(test, methods):
    """Every method the fake app-server received is a discovery method.

    A client's reply to a server request is logged as response:<code> and
    is exempt from the allowlist only. Two independent guards back the
    allowlist for every logged line: no inference method
    (FORBIDDEN_METHODS) and nothing naming login. Editing
    fake_codex.DISCOVERY_METHODS alone therefore cannot let a discovery
    that starts a thread, runs a turn, or logs in pass.
    """
    for method in methods:
        test.assertNotIn(method, FORBIDDEN_METHODS)
        test.assertNotIn("login", method.lower())
        if not method.startswith("response:"):
            test.assertIn(method, fake_codex.DISCOVERY_METHODS)
