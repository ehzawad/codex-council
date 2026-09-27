"""Reusable fake `codex` CLI for codex-council tests (no network, no Codex).

Not a test module itself (the name does not match test_*.py); test files
import it (unittest discover puts tests/ on sys.path). install(bin_dir)
writes an executable ``codex`` into bin_dir — a /bin/sh shim that execs the
interpreter running the tests on the fake's Python source, so the fake is
the process-group leader exactly like a real binary. Prepend bin_dir to
PATH in the environment the code under test sees.

Subcommands:

* ``codex --version`` prints ``codex-cli 9.9.9`` (a scenario "version"
  object can change the output or exit code, or make it hang).
* ``codex app-server --listen stdio://`` runs a newline-delimited JSON-RPC
  loop driven by the scenario JSON file named by FAKE_CODEX_SCENARIO (an
  unset or not-yet-written scenario counts as ``{}``). Every received
  method name is appended to the file named by FAKE_CODEX_METHOD_LOG (a
  client's reply to a server request is logged as ``response:<code>``),
  full requests go to FAKE_CODEX_REQUEST_LOG as JSON lines, and
  FAKE_CODEX_PID_DIR receives ``server.pid`` (and ``grandchild.pid``) so
  tests can prove teardown left nothing behind.
* ``codex exec ...`` behaves like the older inline fakes: records its argv
  as JSON into FAKE_CODEX_ARGV_DIR (file names sort in invocation order),
  reads the prompt on stdin, and replies with thread.started /
  agent_message / turn.completed. A resumed thread keeps the REQUESTED
  thread id. Prompt sentinels (EXEC_SENTINELS) make it fail the way
  current codex-cli does — a structured model_not_found, the ChatGPT
  "model is not supported" sentence, a quota error carrying HTTP 429, a
  model rejection whose text also looks like a stale thread — or emit the
  resume advisory Codex prints when a thread resumes on a different
  model. A rejection names the -m value the runner sent (NATIVE_MODEL when
  none was sent).

Scenario format (every key optional)::

    {
      "version": {"stdout": "codex-cli 9.9.9\\n", "exit": 0, "hang": false},
      "server": {
        "startup_stderr": "...",      # written to stderr at startup
        "startup_exit": 3,            # exit at startup without reading
        "ignore_sigterm": false,      # SIGTERM is ignored (needs SIGKILL)
        "ignore_eof": false,          # keep running after stdin EOF
        "grandchild": false           # true / "ignore_sigterm": spawn a
                                      # sleeper that holds stdout/stderr
      },
      "methods": {
        "<method>": {
          "result": ...,                          # the JSON-RPC result, or
          "error": {"code": -32601, "message": "..."},
          "pages": {"": <result>, "<cursor>": <result>},  # by params.cursor
          "before": [<message>, ...],   # notifications/server requests first
          "delay": 0.1,                 # seconds before responding
          "hang": true,                 # never respond
          "notify_forever": 0.05,       # notify at this interval, no reply
          "raw": "<line>",              # write this line instead of a reply
          "raw_hex": "fffe",            # write these bytes (+ newline)
          "oversize": 100000,           # write one line of this many bytes
          "exit": 3                     # exit with this code, no reply
        }
      }
    }

A request whose method has no spec gets JSON-RPC -32601, like a server
that does not implement it. default_scenario() is a complete, well-formed
happy path with synthetic model ids and account/config LEAK_SENTINELS that
must never reach any council output.
"""

import json
import os
import shlex
import sys

FAKE_CODEX_SOURCE = r'''
import json
import os
import signal
import subprocess
import sys
import time
import uuid

# Filled in by install(): the EXEC_SENTINELS map, NATIVE_MODEL, and the
# model the resume advisory says a thread was recorded with.
SENTINELS = __SENTINELS__
NATIVE_MODEL = __NATIVE_MODEL__
RECORDED_MODEL = __RECORDED_MODEL__


def _scenario():
    """The scenario file's content; {} when unset or not written."""
    path = os.environ.get("FAKE_CODEX_SCENARIO")
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _append(env_name, text):
    path = os.environ.get(env_name)
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def _record_pid(name, pid):
    pid_dir = os.environ.get("FAKE_CODEX_PID_DIR")
    if pid_dir:
        with open(os.path.join(pid_dir, name), "w", encoding="utf-8") as f:
            f.write(str(pid))


def _write_line(data):
    sys.stdout.buffer.write(data + b"\n")
    sys.stdout.buffer.flush()


def _send(message):
    _write_line(json.dumps(message).encode("utf-8"))


def run_version(scenario):
    spec = scenario.get("version", {})
    if spec.get("hang"):
        time.sleep(3600)
    sys.stdout.write(spec.get("stdout", "codex-cli 9.9.9\n"))
    sys.stdout.flush()
    return spec.get("exit", 0)


def _answer(message, spec):
    """Answer one request per its spec; return an exit code to stop."""
    request_id = message["id"]
    if spec is None:
        _send({"id": request_id,
               "error": {"code": -32601, "message": "Method not found"}})
        return None
    for extra in spec.get("before", []):
        _send(extra)
    if "exit" in spec:
        return spec["exit"]
    if spec.get("delay"):
        time.sleep(spec["delay"])
    if spec.get("hang"):
        time.sleep(3600)
        return 0
    if spec.get("notify_forever"):
        while True:
            _send({"method": "fake/tick", "params": {}})
            time.sleep(spec["notify_forever"])
    if "raw" in spec:
        _write_line(spec["raw"].encode("utf-8"))
    elif "raw_hex" in spec:
        _write_line(bytes.fromhex(spec["raw_hex"]))
    elif "oversize" in spec:
        _write_line(b"x" * spec["oversize"])
    elif "pages" in spec:
        cursor = (message.get("params") or {}).get("cursor") or ""
        page = spec["pages"].get(cursor)
        if page is None:
            _send({"id": request_id,
                   "error": {"code": -32602, "message": "unknown cursor"}})
        else:
            _send({"id": request_id, "result": page})
    elif "error" in spec:
        _send({"id": request_id, "error": spec["error"]})
    else:
        _send({"id": request_id, "result": spec.get("result")})
    return None


def run_app_server(scenario):
    server = scenario.get("server", {})
    methods = scenario.get("methods", {})
    _record_pid("server.pid", os.getpid())
    if server.get("ignore_sigterm"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if server.get("grandchild"):
        code = "import time; time.sleep(120)"
        if server["grandchild"] == "ignore_sigterm":
            code = ("import signal, time; "
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "time.sleep(120)")
        # Inherits stdout/stderr: holds both pipes open after we exit.
        child = subprocess.Popen([sys.executable, "-c", code])
        _record_pid("grandchild.pid", child.pid)
    if server.get("startup_stderr"):
        sys.stderr.write(server["startup_stderr"] + "\n")
        sys.stderr.flush()
    if server.get("startup_exit") is not None:
        return server["startup_exit"]
    while True:
        raw = sys.stdin.buffer.readline()
        if not raw:
            break
        if not raw.strip():
            continue
        message = json.loads(raw)
        method = message.get("method")
        if method is None:
            error = message.get("error") or {}
            _append("FAKE_CODEX_METHOD_LOG", f"response:{error.get('code')}")
            continue
        _append("FAKE_CODEX_METHOD_LOG", method)
        _append("FAKE_CODEX_REQUEST_LOG", json.dumps(message))
        if "id" not in message:
            continue  # a notification, e.g. "initialized"
        code = _answer(message, methods.get(method))
        if code is not None:
            return code
    if server.get("ignore_eof"):
        while True:
            time.sleep(1)
    return 0


def _emit(events):
    for event in events:
        sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def _api_error(status, error):
    """codex's failure pair: the same JSON-in-message on both events."""
    message = json.dumps({"type": "error", "status": status, "error": error})
    return [{"type": "error", "message": message},
            {"type": "turn.failed", "error": {"message": message}}]


def _failure(prompt, model):
    """The failure events a sentinel asks for, or None."""
    not_found = (f"The model '{model}' does not exist or you do not have "
                 "access to it.")
    if SENTINELS["reject_structured"] in prompt:
        return _api_error(404, {
            "type": "invalid_request_error", "code": "model_not_found",
            "param": "model", "message": not_found})
    if SENTINELS["reject_chatgpt"] in prompt:
        advisory = (f"Model metadata for `{model}` not found. Defaulting to "
                    "fallback metadata; this can degrade performance and "
                    "cause issues.")
        return [{"type": "item.completed",
                 "item": {"type": "error", "message": advisory}}] + _api_error(
            400, {"type": "invalid_request_error",
                  "message": f"The '{model}' model is not supported when "
                             "using Codex with a ChatGPT account."})
    if SENTINELS["reject_with_stale_words"] in prompt:
        return _api_error(400, {
            "type": "invalid_request_error", "code": "model_not_found",
            "param": "model",
            "message": not_found + " Thread not found in the model cache; "
                                   "no rollout found for it."})
    if SENTINELS["quota_429"] in prompt:
        return _api_error(429, {
            "type": "insufficient_quota", "code": "insufficient_quota",
            "param": None,
            "message": "You exceeded your current quota, please check your "
                       "plan and billing details."})
    return None


def run_exec(argv):
    prompt = sys.stdin.read()
    argv_dir = os.environ.get("FAKE_CODEX_ARGV_DIR")
    if argv_dir:
        name = f"{time.time_ns():020d}-{uuid.uuid4().hex}.json"
        with open(os.path.join(argv_dir, name), "w", encoding="utf-8") as f:
            json.dump(argv, f)
    resumed = "resume" in argv
    if resumed:
        thread_id = argv[argv.index("resume") + 1]
    else:
        thread_id = "thread-" + uuid.uuid4().hex[:12]
    model = argv[argv.index("-m") + 1] if "-m" in argv else NATIVE_MODEL
    events = [{"type": "thread.started", "thread_id": thread_id},
              {"type": "turn.started"}]
    failure = _failure(prompt, model)
    if failure is not None:
        _emit(events + failure)
        return 1
    if resumed and SENTINELS["resume_advisory"] in prompt:
        events.append({"type": "item.completed", "item": {
            "type": "error",
            "message": f"This session was recorded with model "
                       f"`{RECORDED_MODEL}` but is resuming with `{model}`. "
                       f"Consider switching back to `{RECORDED_MODEL}` as it "
                       "may affect Codex performance."}})
    events += [
        {"type": "item.completed",
         "item": {"type": "agent_message", "text": "fake reply from codex"}},
        {"type": "turn.completed"},
    ]
    _emit(events)
    return 0


def main():
    argv = sys.argv[1:]
    scenario = _scenario()
    if argv[:1] == ["--version"]:
        return run_version(scenario)
    if argv[:1] == ["app-server"]:
        return run_app_server(scenario)
    if argv[:1] == ["exec"]:
        return run_exec(argv)
    sys.stderr.write("fake codex: unsupported arguments %r\n" % (argv,))
    return 2


sys.exit(main())
'''

# Account/config values the council must never copy into the snapshot, its
# stdout, or its stderr (account identity, plan, ids, tokens, file paths).
EMAIL_SENTINEL = "council-sentinel@example.invalid"
PLAN_SENTINEL = "plan-sentinel-7d1e"
ACCOUNT_ID_SENTINEL = "acct-sentinel-4b9c"
TOKEN_SENTINEL = "token-sentinel-a83f"
CONFIG_PATH_SENTINEL = "/sentinel-home-5e2a/config.toml"
LEAK_SENTINELS = (
    EMAIL_SENTINEL, PLAN_SENTINEL, ACCOUNT_ID_SENTINEL, TOKEN_SENTINEL,
    CONFIG_PATH_SENTINEL,
)

# The only methods discovery may ever send.
DISCOVERY_METHODS = frozenset({
    "initialize", "initialized", "account/read", "config/read",
    "configRequirements/read", "model/list",
})

NATIVE_MODEL = "future-orion-2032"
NATIVE_EFFORT = "deliberate"
RETIREMENT_AT = 1924992000  # 2031-01-01T00:00:00Z
# The model the resume advisory says a thread was recorded with.
RECORDED_MODEL = "future-lyra-2030"

# Prompt sentinels for the fake `exec` (put one in a role instruction or the
# shared context; both reach the prompt).
EXEC_SENTINELS = {
    "reject_structured": "PLEASE_REJECT_MODEL_STRUCTURED",
    "reject_chatgpt": "PLEASE_REJECT_MODEL_CHATGPT",
    "reject_with_stale_words": "PLEASE_REJECT_MODEL_WITH_STALE_WORDS",
    "quota_429": "PLEASE_QUOTA_429",
    "resume_advisory": "PLEASE_RESUME_ADVISORY",
}


def install(bin_dir):
    """Write the fake into bin_dir as ``codex``; return the shim's path."""
    source = os.path.join(bin_dir, "fake_codex_impl.py")
    text = (
        FAKE_CODEX_SOURCE
        .replace("__SENTINELS__", repr(EXEC_SENTINELS))
        .replace("__NATIVE_MODEL__", repr(NATIVE_MODEL))
        .replace("__RECORDED_MODEL__", repr(RECORDED_MODEL))
    )
    with open(source, "w", encoding="utf-8") as f:
        f.write(text)
    shim = os.path.join(bin_dir, "codex")
    with open(shim, "w", encoding="utf-8") as f:
        f.write(
            "#!/bin/sh\n"
            f'exec {shlex.quote(sys.executable)} {shlex.quote(source)} "$@"\n'
        )
    os.chmod(shim, 0o755)
    return shim


def write_scenario(path, scenario):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(scenario, f)


def read_lines(path):
    """Lines of a fake-codex log file ([] when it was never written)."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().splitlines()
    except FileNotFoundError:
        return []


def model_entry(dispatch_id, **overrides):
    """One wire-format model/list record with synthetic defaults.

    Any wire field (including "model" itself) can be overridden, which is
    how tests build malformed or picker-id != dispatch-id records.
    """
    entry = {
        "id": dispatch_id,
        "model": dispatch_id,
        "displayName": f"Synthetic {dispatch_id}",
        "description": f"Synthetic catalog entry for {dispatch_id}.",
        "hidden": False,
        "isDefault": False,
        "defaultReasoningEffort": "brisk",
        "supportedReasoningEfforts": [
            {"reasoningEffort": "brisk",
             "description": "Short bounded checks."},
            {"reasoningEffort": "deliberate",
             "description": "Extended careful analysis."},
        ],
        "upgrade": None,
        "upgradeInfo": None,
        "inputModalities": ["text"],
        "serviceTiers": [],
    }
    entry.update(overrides)
    return entry


def default_catalog():
    """Synthetic catalog: picker id != dispatch id, a recommended entry,
    a retiring entry with an upgrade target, and a hidden entry."""
    return [
        model_entry(
            NATIVE_MODEL, id="picker-orion", displayName="Orion",
            description="For difficult verification judgments.",
            defaultReasoningEffort=NATIVE_EFFORT,
            supportedReasoningEfforts=[
                {"reasoningEffort": "brisk",
                 "description": "Short bounded checks."},
                {"reasoningEffort": "deliberate",
                 "description": "Extended careful analysis."},
                {"reasoningEffort": "adaptive-v2",
                 "description": "Adaptive reasoning depth."},
            ],
        ),
        model_entry("future-vega-2033", isDefault=True,
                    description="Fast checks for narrow questions."),
        model_entry(
            "future-lyra-2030", description="Legacy synthetic model.",
            upgradeInfo={
                "model": "future-vega-2033", "retirementAt": RETIREMENT_AT,
                "migrationMarkdown": None, "modelLink": None,
                "upgradeCopy": None,
            },
        ),
        model_entry("future-hidden-2031", hidden=True,
                    description="Internal synthetic model."),
    ]


def _origin(kind="user"):
    return {"name": {"type": kind, "file": CONFIG_PATH_SENTINEL,
                     "profile": None},
            "version": "sha256:" + TOKEN_SENTINEL}


def default_scenario(catalog=None):
    """A complete, well-formed discovery happy path (status "ok")."""
    return {
        "methods": {
            "initialize": {"result": {
                "userAgent": "codex_council/9.9.9 (fake)",
                "codexHome": "/fake-codex-home",
                "platformFamily": "unix",
                "platformOs": "macos",
            }},
            "account/read": {"result": {
                "account": {
                    "type": "chatgpt",
                    "email": EMAIL_SENTINEL,
                    "planType": PLAN_SENTINEL,
                    "chatgptAccountId": ACCOUNT_ID_SENTINEL,
                },
                "requiresOpenaiAuth": True,
                "workspaceRouting": {
                    "chatgptAccountId": ACCOUNT_ID_SENTINEL,
                    "backendOrigin": "https://sentinel.invalid",
                    "accountRoutingOverride": "NO_CONSTRAINT",
                },
                "accessToken": TOKEN_SENTINEL,
            }},
            "config/read": {"result": {
                "config": {
                    "model": NATIVE_MODEL,
                    "model_reasoning_effort": NATIVE_EFFORT,
                    "model_provider": None,
                    "developer_instructions": TOKEN_SENTINEL,
                    "chatgpt_account": EMAIL_SENTINEL,
                },
                "origins": {
                    "model": _origin(),
                    "model_reasoning_effort": _origin(),
                    "developer_instructions": _origin(),
                },
            }},
            "configRequirements/read": {"result": {"requirements": None}},
            "model/list": {"pages": {"": {
                "data": default_catalog() if catalog is None else catalog,
                "nextCursor": None,
            }}},
        },
    }
