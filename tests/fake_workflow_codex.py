#!/usr/bin/env python3
"""Scripted protocol fixture. It checks argv, NOT actual sandbox enforcement."""
import json
import os
import subprocess
import sys
import time
import tomllib
import uuid
from pathlib import Path


def emit(value):
    print(json.dumps(value), flush=True)


def apply_codex_override(config, setting):
    """Codex's -c semantics (config_override.rs, overrides.rs): split once on
    '=', trim, parse the value as TOML or fall back to the raw string, then
    split the key on EVERY '.' with no quote handling. Not Python TOML keys."""
    key, separator, raw = setting.partition("=")
    key, raw = key.strip(), raw.strip()
    if not separator or not key:
        raise ValueError(f"invalid override: {setting}")
    try:
        value = tomllib.loads(f"_x_ = {raw}")["_x_"]
    except tomllib.TOMLDecodeError:
        value = raw.strip("\"'")
    current = config
    segments = key.split(".")
    for segment in segments[:-1]:
        if not isinstance(current.get(segment), dict):
            current[segment] = {}
        current = current[segment]
    current[segments[-1]] = value
    return config


def config(argv):
    value = {"model": "fixture-collaborator", "model_reasoning_effort": "brisk",
             "model_provider": None,
             "mcp_servers": {os.environ.get("FAKE_WORKFLOW_MCP", "fixture-server"):
                             {"enabled": True}}}
    configured = set(value["mcp_servers"])
    for index, arg in enumerate(argv[:-1]):
        if arg == "-c":
            apply_codex_override(value, argv[index + 1])
    if set(value["mcp_servers"]) - configured:
        # Real Codex: an override-created server has no transport.
        print("Error: invalid transport", file=sys.stderr)
        sys.exit(1)
    if os.environ.get("FAKE_WORKFLOW_POLICY_REFUSE"):
        value["sandbox_mode"] = "danger-full-access"
    return value


def log(value):
    with open(os.environ["FAKE_WORKFLOW_LOG"], "a", encoding="utf-8") as stream:
        stream.write(json.dumps(value) + "\n")


def app_server(argv):
    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        log({"method": method, "cwd": os.getcwd(), "argv": argv})
        if "id" not in request:
            continue
        if method == "initialize":
            result = {"codexHome": os.environ["CODEX_HOME"]}
        elif method == "account/read":
            result = {"account": {"type": "chatgpt"}, "requiresOpenaiAuth": True}
        elif method == "config/read":
            result = {"config": config(argv), "origins": {}}
        elif method == "configRequirements/read":
            result = {"requirements": None}
        elif method == "model/list":
            result = {"data": [{"id": "fixture-collaborator", "model": "fixture-collaborator",
                "displayName": "Fixture", "description": "A synthetic collaborator.",
                "hidden": False, "isDefault": True, "defaultReasoningEffort": "brisk",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "brisk", "description": "Bounded checks."}]}],
                "nextCursor": None}
        else:
            emit({"id": request["id"], "error": {"code": -32601, "message": "unsupported"}})
            continue
        emit({"id": request["id"], "result": result})


def command(code, cwd):
    argv = [sys.executable, "-c", code]
    emit({"type": "item.started", "item": {"type": "command_execution", "command": repr(argv)}})
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=10)
    emit({"type": "item.completed", "item": {"type": "command_execution",
          "command": repr(argv), "exit_code": proc.returncode,
          "aggregated_output": proc.stdout + proc.stderr}})
    return proc.returncode


def execute(argv):
    body = sys.stdin.read()
    cwd = Path(argv[argv.index("-C") + 1])
    sid = argv[argv.index("resume") + 1] if "resume" in argv else str(uuid.uuid4())
    log({"method": "exec", "cwd": os.getcwd(), "root": str(cwd),
         "argv": argv, "thread": sid})
    emit({"type": "thread.started", "thread_id": sid})
    emit({"type": "turn.started"})
    if "HANG_FIXTURE" in body:
        emit({"type": "item.started", "item": {"type": "command_execution", "command": "sleep"}})
        time.sleep(120)
    if "SLOW_FIXTURE" in body:
        time.sleep(1)
    if any(marker in body for marker in ("SIDE_EFFECT_503", "SIDE_EFFECT_429", "SIDE_EFFECT_STALE")):
        command("from pathlib import Path; p=Path('effects.txt'); "
                "p.write_text((p.read_text() if p.exists() else '')+'effect\\n')", cwd)
        message = ("HTTP 429 Too Many Requests" if "SIDE_EFFECT_429" in body else
                   "Error: no rollout found for thread id " + sid if "SIDE_EFFECT_STALE" in body
                   else "HTTP 503 Service Unavailable")
        emit({"type": "turn.failed", "error": {"message": message}})
        return 1
    if "TRANSIENT_BEFORE_TOOLS" in body:
        marker = Path(os.environ["CODEX_HOME"]) / "retried"
        if not marker.exists():
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("1")
            emit({"type": "turn.failed", "error": {"message": "HTTP 503 Service Unavailable"}})
            return 1
    if "AUTH_FAILURE" in body:
        emit({"type": "turn.failed", "error": {"message": "HTTP 401 Unauthorized"}})
        return 1
    if "EXPERIMENT_FIXTURE" in body:
        if config(argv).get("sandbox_mode") != "workspace-write":
            return 2
        before = command("from subject import value; assert value == 2", cwd)
        (cwd / "subject.py").write_text("value = 2\n")
        # Avoid Python's timestamp/size-based .pyc cache masking this tiny edit.
        import shutil
        shutil.rmtree(cwd / "__pycache__", ignore_errors=True)
        after = command("from subject import value; assert value == 2", cwd)
        message = f"Baseline exit {before}; candidate exit {after}. Inspect the evidence."
    else:
        message = "Fixture contribution completed; no substantive verdict is inferred."
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": message}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 10}})
    return 0


def main():
    argv = sys.argv[1:]
    if argv == ["--version"]:
        print("codex-cli 9.9.9")
        return 0
    if argv and argv[0] == "app-server":
        app_server(argv)
        return 0
    if argv and argv[0] == "exec":
        return execute(argv)
    return 2


if __name__ == "__main__":
    sys.exit(main())
