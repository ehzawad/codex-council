#!/usr/bin/env python3
"""Opt-in decision receipts. No worker dispatch, scheduler, or prompt injection.

This checkout-only experiment reads prepared Council runs. Hashes establish
record identity, not semantic truth, independent review, or user authorization.
Only input-preserving read-only contributions can support reusable receipts in
this first version. Writable experiments remain ordinary Council operations.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/codex-council/skills/codex-council/scripts"
# Do not write bytecode into the installed plugin or change its import graph.
_previous_bytecode = sys.dont_write_bytecode
sys.dont_write_bytecode = True
sys.path.insert(0, str(SCRIPTS))
try:
    from council_workflow import load_plan
    from council_workspace import (WorkspaceError, atomic_json, canonical, digest,
                                   freshness, private_dir, read_json, regular,
                                   verify_tree)
finally:
    sys.dont_write_bytecode = _previous_bytecode

SCHEMA = "codex-council/decision-receipt@1"
CONTRACT = "codex-council/decision-contract@1"
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
MAX_STATE_BYTES = 1024 * 1024


def require(condition: bool, message: str) -> None:
    if not condition:
        raise WorkspaceError(message)


def fields(value: Any, required: set[str], optional: set[str] = frozenset()) -> None:
    require(isinstance(value, dict) and required <= value.keys()
            and not (value.keys() - required - optional), "missing or unknown fields")


def text(value: Any, limit: int = 2000) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def identifier(value: Any) -> bool:
    return isinstance(value, str) and ID.fullmatch(value) is not None


def contract(value: Any) -> dict[str, Any]:
    fields(value, {"schema", "task_id", "decision_id", "question", "assumptions", "criteria"})
    require(value["schema"] == CONTRACT, "unsupported decision contract")
    require(identifier(value["task_id"]) and identifier(value["decision_id"]), "invalid task/decision id")
    require(text(value["question"], 500), "question must be short and nonempty")
    require(isinstance(value["assumptions"], list) and len(value["assumptions"]) <= 32
            and all(text(item, 500) for item in value["assumptions"]), "invalid assumptions")
    criteria = value["criteria"]
    require(isinstance(criteria, list) and 0 < len(criteria) <= 64, "declare 1 to 64 criteria")
    ids = set()
    for item in criteria:
        fields(item, {"id", "expectation", "kind"}, {"expected_exit"})
        require(identifier(item["id"]) and item["id"] not in ids, "duplicate or invalid criterion id")
        ids.add(item["id"])
        require(text(item["expectation"]), "invalid expectation")
        require(item["kind"] in ("command", "judgment"), "kind must be command or judgment")
        if item["kind"] == "command":
            require(type(item.get("expected_exit")) is int, "command criterion needs expected_exit")
        else:
            require("expected_exit" not in item, "judgment cannot prescribe a command exit")
    return value


@contextlib.contextmanager
def locked(directory: str | Path, *, create: bool = False, shared: bool = False):
    root = private_dir(directory)
    path = root / "receipt.lock"
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    if create:
        try:
            fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            fd = os.open(path, flags)
        else:
            os.fchmod(fd, 0o600)
    else:
        fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        require(stat.S_ISREG(st.st_mode) and st.st_uid == os.geteuid()
                and not st.st_mode & 0o077 and st.st_nlink == 1, "unsafe receipt lock")
        # Nonblocking: a competing writer is a conflict, not an indefinite wait.
        fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        named = path.lstat()
        require((named.st_dev, named.st_ino) == (st.st_dev, st.st_ino), "receipt lock replaced")
        yield root
    finally:
        os.close(fd)


def save(root: Path, value: dict[str, Any]) -> None:
    data = {key: item for key, item in value.items() if key != "digest"}
    data["digest"] = digest(data)
    require(len(canonical(data)) <= MAX_STATE_BYTES, "receipt history full; start a new decision")
    atomic_json(root / "receipt.json", data)


def read(root: Path) -> dict[str, Any]:
    with regular(root, "receipt.json") as stream:
        require(os.fstat(stream.fileno()).st_size <= MAX_STATE_BYTES, "receipt too large")
    value = read_json(root / "receipt.json")
    fields(value, {"schema", "contract", "runs", "history", "digest"})
    require(value["schema"] == SCHEMA and value["digest"] == digest(
        {key: item for key, item in value.items() if key != "digest"}), "receipt checksum/schema mismatch")
    contract(value["contract"])
    require(isinstance(value["runs"], dict) and 0 < len(value["runs"]) <= 16, "invalid run bindings")
    require(isinstance(value["history"], list), "invalid history")
    for number, event in enumerate(value["history"], 1):
        require(isinstance(event, dict) and event.get("revision") == number, "invalid revision sequence")
    return value


def file_hash(root: Path, name: str) -> str:
    h = hashlib.sha256()
    with regular(root, name) as stream:
        before = os.fstat(stream.fileno())
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
        after = os.fstat(stream.fileno())
        require((before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                == (after.st_size, after.st_mtime_ns, after.st_ctime_ns), "evidence changed while reading")
    return h.hexdigest()


def checked_plan(directory: str | Path, *, pristine: bool = False) -> tuple[Path, dict]:
    root = private_dir(directory)
    plan = load_plan(root, verify_inputs=pristine)
    require(plan is not None, "needs a prepared Council run")
    require(file_hash(root, "context.md") == plan["context_sha256"], "captured context changed")
    return root, plan


def declare(directory: str | Path, spec: dict, runs: list[str | Path]) -> dict:
    spec = contract(spec)
    require(0 < len(runs) <= 16, "declare 1 to 16 prepared runs")
    with locked(directory, create=True) as root:
        require(not os.path.lexists(root / "receipt.json"), "decision already declared")
        bindings, subject = {}, None
        for run in runs:
            path, plan = checked_plan(run, pristine=True)
            require(not any(os.path.lexists(path / name) for name in
                            ("supervisor.lock", "execution.claim", "result.json")),
                    "declare criteria before launch; do not relabel past evidence")
            require(plan["task_id"] == spec["task_id"], "task mismatch")
            source = Path(plan["snapshot"]["source_root"])
            require(not any(root.is_relative_to(p) or p.is_relative_to(root) for p in (path, source)),
                    "receipt storage must be outside source and run trees")
            require(plan["run_id"] not in bindings, "duplicate run")
            candidate = digest(plan["snapshot"])
            require(subject is None or subject == candidate, "runs must examine the same captured subject")
            subject = candidate
            bindings[plan["run_id"]] = {"directory": str(path), "plan_digest": digest(plan)}
        save(root, {"schema": SCHEMA, "contract": spec, "runs": bindings, "history": []})
        return {"revision": 0, "decision_id": spec["decision_id"], "state": "declared"}


def idle(root: Path) -> None:
    path = root / "supervisor.lock"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        require(stat.S_ISREG(os.fstat(fd).st_mode), "invalid supervisor lock")
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    finally:
        os.close(fd)


def evidence(state: dict, rule: dict, outcome: dict) -> dict:
    ref = outcome["evidence"]
    fields(ref, {"run_id", "role_id"}, {"attempt_file", "command_index"})
    require(isinstance(ref["run_id"], str) and ref["run_id"] in state["runs"], "undeclared run")
    binding = state["runs"][ref["run_id"]]
    root, plan = checked_plan(binding["directory"])
    require(digest(plan) == binding["plan_digest"], "run contract changed")
    idle(root)
    result = read_json(root / "result.json")
    require(isinstance(result, dict) and result.get("schema") == "codex-council/result@1", "invalid result")
    require(result.get("result_digest") == digest({k: v for k, v in result.items()
                                                  if k != "result_digest"}), "result checksum mismatch")
    for key, expected in (("run_id", plan["run_id"]), ("task_id", plan["task_id"]),
                          ("snapshot_id", plan["snapshot"]["snapshot_id"]),
                          ("context_sha256", plan["context_sha256"])):
        require(result.get(key) == expected, "result subject mismatch")
    require(result.get("execution_state") == "done" and result.get("baseline_intact") is True,
            "result incomplete or baseline not intact")
    role_id = ref["role_id"]
    require(isinstance(role_id, str) and role_id in plan["roles"], "unknown contribution")
    role_binding = plan["roles"][role_id]
    role = result.get("roles", {}).get(role_id)
    require(isinstance(role, dict) and role.get("execution_ok") is True
            and role.get("coverage_gaps") == [], "contribution/evidence incomplete")
    policy = role_binding["execution"]
    require(policy["sandbox"] == "read-only" and policy["external_tools"] == "deny"
            and policy["network"] is False, "writable/external work needs a captured final-state check")
    require(role.get("requested_execution") == policy, "result policy mismatch")
    snap = plan["snapshot"]
    verify_tree(root / "baseline", snap["files"], snap["dirs"])
    verify_tree(root / role_binding["workspace"], snap["files"], snap["dirs"])
    attempts = role.get("attempts")
    require(isinstance(attempts, list) and bool(attempts)
            and attempts[-1].get("turn_completed_observed") is True, "no completed turn")
    files = {"run.json": file_hash(root, "run.json"),
             "context.md": file_hash(root, "context.md"),
             "result.json": file_hash(root, "result.json")}
    for attempt in attempts:
        require(attempt.get("evidence_complete") is True and attempt.get("coverage_gaps") == [],
                "incomplete raw evidence")
        for channel in ("stdout", "stderr"):
            name = attempt.get(f"{channel}_file")
            require(isinstance(name, str) and Path(name).name == name, "invalid evidence filename")
            relative = f"{role_binding['evidence']}/{name}"
            observed = file_hash(root, relative)
            require(observed == attempt.get("sha256", {}).get(channel), "raw evidence checksum mismatch")
            files[relative] = observed
    if rule["kind"] == "command":
        name, index = ref.get("attempt_file"), ref.get("command_index")
        require(isinstance(name, str) and re.fullmatch(r"attempt-[A-Za-z0-9-]+\.json", name) is not None
                and type(index) is int and index >= 0, "command evidence needs attempt_file and command_index")
        relative = f"{role_binding['evidence']}/{name}"
        attempt = read_json(root / relative)
        require(attempt in attempts and index < len(attempt["commands"]), "command not in recorded attempt")
        command = attempt["commands"][index]
        code = command.get("reported_exit_code")
        require(type(code) is int and type(command.get("line")) is int, "unknown command outcome")
        # Tie the summary to the actual stored JSONL record, not just its index.
        rawname = f"{role_binding['evidence']}/{attempt['stdout_file']}"
        event = None
        with regular(root, rawname) as stream:
            for line, raw in enumerate(stream, 1):
                if line == command["line"]:
                    event = json.loads(raw)
                    break
        require(isinstance(event, dict) and event.get("type") == "item.completed", "missing command event")
        item = event.get("item", {})
        require(item.get("type") == "command_execution" and item.get("exit_code") == code
                and item.get("command") == command.get("command"), "command summary differs from raw event")
        supported = code == rule["expected_exit"]
        require(supported == (outcome["outcome"] == "supported"), "outcome contradicts declared exit predicate")
        files[relative] = file_hash(root, relative)
    else:
        require(set(ref) == {"run_id", "role_id"}, "judgment evidence uses the contribution, not a command")
        require(text(role.get("interpretation", {}).get("text"), 10**9), "no contribution text")
    return {"run_id": ref["run_id"], "role_id": role_id, "files": files,
            "kind": rule["kind"], "provenance": "codex-protocol" if rule["kind"] == "command"
            else "host-judgment-of-collaborator-text"}


def decide(directory: str | Path, decision: dict, current: dict) -> dict:
    fields(decision, {"expected_revision", "actor", "disposition", "rationale", "outcomes"})
    require(type(decision["expected_revision"]) is int and decision["expected_revision"] >= 0,
            "expected_revision must be a nonnegative integer")
    require(text(decision["actor"], 200) and text(decision["rationale"]), "actor and rationale required")
    require(decision["disposition"] in ("accepted", "rejected", "blocked", "withdrawn"), "invalid disposition")
    with locked(directory) as root:
        state = read(root)
        require(contract(current) == state["contract"], "decision contract changed; declare a new receipt")
        require(decision["expected_revision"] == len(state["history"]), "revision conflict; reread before updating")
        rules = {rule["id"]: rule for rule in state["contract"]["criteria"]}
        outcomes = decision["outcomes"]
        require(isinstance(outcomes, list), "outcomes must be an array")
        seen, observations = {}, []
        for outcome in outcomes:
            fields(outcome, {"criterion_id", "outcome"}, {"evidence"})
            key = outcome["criterion_id"]
            require(isinstance(key, str) and key in rules and key not in seen, "unknown/duplicate criterion")
            require(outcome["outcome"] in ("supported", "contradicted", "blocked", "unassessed"), "invalid outcome")
            seen[key] = outcome["outcome"]
            if outcome["outcome"] in ("supported", "contradicted"):
                require("evidence" in outcome, "supported/contradicted outcomes need evidence")
                observations.append(evidence(state, rules[key], outcome))
            else:
                require("evidence" not in outcome, "blocked/unassessed outcomes must not claim usable evidence")
        disposition = decision["disposition"]
        if disposition == "withdrawn":
            require(not outcomes, "withdrawal has no new evidence; explain it in rationale")
        else:
            require(set(seen) == set(rules), "account for every declared criterion")
            if disposition == "accepted":
                require(set(seen.values()) == {"supported"}, "acceptance requires every criterion supported")
                for stored in state["runs"].values():
                    _, plan = checked_plan(stored["directory"])
                    require(digest(plan) == stored["plan_digest"], "run contract changed")
                    require(freshness(plan["snapshot"])["state"] == "unchanged",
                            "current subject is stale or unknown")
            elif disposition == "rejected":
                require("contradicted" in seen.values(), "rejection needs a contradicted criterion")
            else:
                require(any(v in ("blocked", "unassessed") for v in seen.values()), "blocked decision needs a gap")
        event = {**decision, "revision": len(state["history"]) + 1,
                 "evidence": observations, "authority": "self-attributed-host-decision-not-authentication"}
        # Preserve historical outcomes. Current applicability is computed only
        # at explicit recall against a supplied current decision contract.
        state["history"].append(event)
        save(root, state)
        return {"revision": event["revision"], "disposition": disposition}


def status(directory: str | Path, current: dict | None = None, *, max_bytes: int = 2048) -> dict:
    require(type(max_bytes) is int and 512 <= max_bytes <= 16384, "max_bytes must be 512..16384")
    with locked(directory, shared=True) as root:
        state = read(root)
        gaps = []
        binding = "match"
        if current is None:
            binding = "unknown"
            gaps.append("current-contract-not-supplied")
        elif contract(current) != state["contract"]:
            binding = "stale"
            gaps.append("current-decision-contract-changed")
        latest = state["history"][-1] if state["history"] else None
        for stored in state["runs"].values():
            try:
                run, plan = checked_plan(stored["directory"])
                require(digest(plan) == stored["plan_digest"], "run contract changed")
                verify_tree(run / "baseline", plan["snapshot"]["files"], plan["snapshot"]["dirs"])
                observed = freshness(plan["snapshot"])["state"]
                if observed != "unchanged":
                    gaps.append("captured-inputs-" + observed)
                    if binding != "stale":
                        binding = "stale" if observed == "stale" else "unknown"
            except (OSError, ValueError, KeyError, TypeError):
                gaps.append("capture-unavailable-or-changed")
                if binding != "stale":
                    binding = "unknown"
        if latest:
            for observed in latest["evidence"]:
                try:
                    run = private_dir(state["runs"][observed["run_id"]]["directory"])
                    for path, expected in observed["files"].items():
                        require(file_hash(run, path) == expected, "evidence changed")
                    _, plan = checked_plan(run)
                    require(digest(plan) == state["runs"][observed["run_id"]]["plan_digest"],
                            "run contract changed")
                    role = plan["roles"][observed["role_id"]]
                    verify_tree(run / role["workspace"], plan["snapshot"]["files"], plan["snapshot"]["dirs"])
                except (OSError, ValueError, KeyError, TypeError):
                    gaps.append("evidence-unavailable-or-changed")
                    if binding != "stale":
                        binding = "unknown"
        result = {"decision_id": state["contract"]["decision_id"],
                  "revision": len(state["history"]),
                  "recorded_disposition": latest["disposition"] if latest else "undeclared-outcome",
                  "binding_status": binding,
                  "next_action": "reconcile-current-request; never automatic permission or proof",
                  "gaps": sorted(set(gaps)), "details_omitted": False}
        details = {"question": state["contract"]["question"],
                   "rationale": latest["rationale"] if latest else None,
                   "actor": latest["actor"] if latest else None,
                   "criterion_outcomes": {item["criterion_id"]: item["outcome"]
                                          for item in latest["outcomes"]} if latest else {},
                   "record": str(root / "receipt.json")}
        for key, value in details.items():
            if len(canonical({**result, key: value})) + 1 <= max_bytes:
                result[key] = value
            else:
                result["details_omitted"] = True
        # Do not truncate a JSON record or omit its top-level stale/unknown flag.
        require(len(canonical(result)) + 1 <= max_bytes, "summary budget too small for status fields")
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    begin = commands.add_parser("declare", help="freeze decision criteria before prepared runs launch")
    begin.add_argument("--store", required=True)
    begin.add_argument("--contract", required=True)
    begin.add_argument("--run", action="append", required=True)
    record = commands.add_parser("decide", help="record an attributed host decision, not model votes")
    record.add_argument("--store", required=True)
    record.add_argument("--decision", required=True)
    record.add_argument("--contract", required=True)
    recall = commands.add_parser("status", help="read only the explicitly selected decision; no auto retrieval")
    recall.add_argument("--store", required=True)
    recall.add_argument("--contract")
    recall.add_argument("--max-bytes", type=int, default=2048)
    args = parser.parse_args(argv)
    try:
        if args.command == "declare":
            result = declare(args.store, read_json(Path(args.contract), private=False), args.run)
        elif args.command == "decide":
            result = decide(args.store, read_json(Path(args.decision), private=False),
                            read_json(Path(args.contract), private=False))
        else:
            current = read_json(Path(args.contract), private=False) if args.contract else None
            result = status(args.store, current, max_bytes=args.max_bytes)
        sys.stdout.buffer.write(canonical(result) + b"\n")
        return 0 if args.command != "status" or result["binding_status"] == "match" else 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"decision-receipts: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
