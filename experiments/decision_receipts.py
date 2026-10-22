#!/usr/bin/env python3
"""Optional records of Claude's revisable working position over Codex Council evidence.

Checkout-only experiment; the installed plugin never imports it. Claude, as
host, decides. This helper never dispatches, auto-recalls, approves or gates.

exit codes:
  0  status: binding_status "match" and recorded_host_position "use_for_now";
     declare and record: done
  1  status: anything else (stale, unknown, another position, or none)
  2  invalid input, refused record, or lock conflict
Exit 0 is not approval; read recorded_host_position.

evidence limits: only fresh, read-only contributions with external tools and
network off. Hashes bind record identity; they are not semantic proof.

run storage: runs made with the runner's --new-run persist; a deliberate
--prune-runs --delete removes runs, and receipts bound to them then read unknown.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/codex-council/skills/codex-council/scripts"
_previous_bytecode = sys.dont_write_bytecode
sys.dont_write_bytecode = True  # never write bytecode into the plugin
sys.path.insert(0, str(SCRIPTS))
try:
    from council_common import SUPERVISOR_FILENAME
    from council_liveness import lock_state, read_supervisor
    from council_workflow import load_plan
    from council_workspace import (WorkspaceError, atomic_json, canonical, digest,
                                   freshness, private_dir, read_json, regular,
                                   verify_tree)
finally:
    sys.dont_write_bytecode = _previous_bytecode

SCHEMA = "codex-council/decision-receipt@2"
CONTRACT = "codex-council/decision-contract@1"
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
MAX_STATE_BYTES = 1024 * 1024
HOST_POSITIONS = ("use_for_now", "revisit", "needs_evidence", "set_aside")
OUTCOMES = ("supported", "contradicted", "blocked", "unassessed")
RAW = argparse.RawDescriptionHelpFormatter
TEMP_AREAS = ("/tmp", "/private/tmp", "/var/folders")  # plus $TMPDIR and tempfile.gettempdir()
DECLARE_HELP = """\
contract JSON: {"schema": "codex-council/decision-contract@1", "task_id",
  "decision_id", "question", "assumptions": [text], "criteria": [{"id",
  "expectation", "kind": "command", "expected_exit": int} or {"id",
  "expectation", "kind": "judgment"}]}
Bind 1-16 prepared, unlaunched runs of the same task and captured subject.
Output "warnings" names a run or store in a temporary area (it can vanish at
reboot; status then reads unknown). A warning never refuses."""
RECORD_HELP = """\
decision JSON: {"expected_revision": int, "recorded_by", "rationale",
  "host_position": "use_for_now"|"revisit"|"needs_evidence"|"set_aside",
  "outcomes": [{"criterion_id", "outcome": "supported"|"contradicted"|
  "blocked"|"unassessed", "evidence": {"run_id", "role_id", "attempt_file",
  "command_index"}}]}
supported/contradicted need evidence (judgment: run_id and role_id only;
command_index is zero-based). use_for_now needs every criterion supported and
unchanged inputs; set_aside takes "outcomes": [] and survives changed criteria."""
STATUS_HELP = """\
binding_status: "match"; "stale" (a known change to the contract or captured
inputs); "unknown" (no --contract, or run, evidence or context unreadable or
changed: a changed context.md fails closed as unknown). Stale outranks unknown,
as in the plugin's --freshness: no --contract plus stale inputs reads "stale"
with both gaps. Gaps also flag "no-recorded-position" and
"criteria-without-evidence". Exit 0 only for "match" with "use_for_now".
Exit 0 is not approval; read recorded_host_position."""


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
        # Nonblocking: a competing writer is a conflict, not a wait.
        fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        named = path.lstat()
        require((named.st_dev, named.st_ino) == (st.st_dev, st.st_ino), "receipt lock replaced")
        yield root
    finally:
        os.close(fd)


def save(root: Path, value: dict[str, Any]) -> None:
    data = {key: item for key, item in value.items() if key != "digest"}
    data["digest"] = digest(data)
    # + 1: atomic_json appends a newline and read() bounds the file size.
    require(len(canonical(data)) + 1 <= MAX_STATE_BYTES, "receipt history full; start a new decision")
    atomic_json(root / "receipt.json", data)


def read(root: Path) -> dict[str, Any]:
    with regular(root, "receipt.json") as stream:
        require(os.fstat(stream.fileno()).st_size <= MAX_STATE_BYTES, "receipt too large")
    value = read_json(root / "receipt.json")
    fields(value, {"schema", "contract", "runs", "history", "digest"})
    require(value["schema"] == SCHEMA and value["digest"] == digest(
        {key: item for key, item in value.items() if key != "digest"}), "receipt checksum/schema mismatch")
    contract(value["contract"])
    runs = value["runs"]
    require(isinstance(runs, dict) and 0 < len(runs) <= 16 and all(
        isinstance(item, dict) and isinstance(item.get("directory"), str) for item in runs.values()),
        "invalid run bindings")
    require(isinstance(value["history"], list), "invalid history")
    for number, event in enumerate(value["history"], 1):
        require(isinstance(event, dict) and type(event.get("revision")) is int
                and event["revision"] == number, "invalid revision sequence")
        require(event.get("host_position") in HOST_POSITIONS
                and text(event.get("recorded_by"), 200), "invalid recorded host position")
        outcomes, cited = event.get("outcomes"), event.get("evidence")
        require(isinstance(outcomes, list) and all(
            isinstance(item, dict) and isinstance(item.get("criterion_id"), str)
            and item.get("outcome") in OUTCOMES for item in outcomes)
            and isinstance(cited, list) and all(
                isinstance(item, dict) and isinstance(item.get("files"), dict) for item in cited),
            "invalid recorded outcomes or evidence")
    return value


def owned(root: Path, name: str, *, private: bool = True) -> bytes:
    """One read of an owned (private) regular file through pinned directories."""
    with regular(root, name) as stream:
        before = os.fstat(stream.fileno())
        require(before.st_uid == os.geteuid() and not (private and before.st_mode & 0o077),
                f"not an owned private regular file: {name}")
        data = stream.read()
        after = os.fstat(stream.fileno())
    require(len(data) == before.st_size and (before.st_mtime_ns, before.st_ctime_ns)
            == (after.st_mtime_ns, after.st_ctime_ns), "evidence changed while reading")
    return data


def strict(data: bytes) -> Any:
    """JSON as read_json parses it: no duplicate keys or non-finite numbers."""
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            require(key not in result, f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def finite(literal: str) -> float:
        value = float(literal)
        require(math.isfinite(value), f"non-finite JSON number: {literal}")
        return value

    def constant(literal: str) -> Any:
        raise WorkspaceError(f"non-finite JSON number: {literal}")

    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=constant, parse_float=finite)
    except RecursionError:
        raise WorkspaceError("JSON nesting too deep") from None


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def checked_plan(directory: str | Path, *, pristine: bool = False) -> tuple[Path, dict, dict]:
    """The run, its plan, and hashes of the exact run.json and context.md bytes checked."""
    root = private_dir(directory)
    plan = load_plan(root, verify_inputs=pristine)
    require(plan is not None, "needs a prepared Council run")
    run, context = owned(root, "run.json"), owned(root, "context.md", private=False)
    require(strict(run) == plan, "run contract changed")
    require(sha(context) == plan["context_sha256"], "captured context changed")
    return root, plan, {"run.json": sha(run), "context.md": sha(context)}


def temporary_areas() -> set[Path]:
    """Resolved areas whose contents can vanish at reboot."""
    spellings = [os.environ.get("TMPDIR"), *TEMP_AREAS]
    with contextlib.suppress(OSError):  # no usable temp dir: nothing more to compare
        spellings.append(tempfile.gettempdir())
    areas = set()
    for item in filter(None, spellings):
        with contextlib.suppress(OSError):  # unresolvable spelling: warn less, never refuse
            areas.add(Path(os.path.realpath(item)))
    return {area for area in areas if area.parent != area}  # never flag all of "/"


def declare(directory: str | Path, spec: dict, runs: list[str | Path]) -> dict:
    spec = contract(spec)
    require(0 < len(runs) <= 16, "declare 1 to 16 prepared runs")
    with locked(directory, create=True) as root:
        require(not os.path.lexists(root / "receipt.json"), "decision already declared")
        bindings, subject = {}, None
        for run in runs:
            path, plan, _ = checked_plan(run, pristine=True)
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
        result = {"revision": 0, "decision_id": spec["decision_id"], "state": "declared"}
        areas = temporary_areas()
        named = [("receipt store", root)] + [(f"run {key}", Path(item["directory"]))
                                             for key, item in bindings.items()]
        warnings = [f"{label} {path} is in a temporary area: it can vanish at reboot and "
                    "the receipt then reads unknown" for label, path in named
                    if any(path.is_relative_to(area) for area in areas)]
        if warnings:  # warn, never refuse
            result["warnings"] = warnings
        return result


def idle(root: Path) -> None:
    # The lock supervisor.json names (inode and token) must still be there, and free.
    supervisor = read_supervisor(root / SUPERVISOR_FILENAME)
    require(supervisor is not None and lock_state(root, supervisor) == "free",
            "run supervisor active, replaced, or unverifiable")


def evidence(state: dict, rule: dict, outcome: dict) -> dict:
    ref = outcome["evidence"]
    fields(ref, {"run_id", "role_id"}, {"attempt_file", "command_index"})
    require(isinstance(ref["run_id"], str) and ref["run_id"] in state["runs"], "undeclared run")
    binding = state["runs"][ref["run_id"]]
    root, plan, files = checked_plan(binding["directory"])
    require(digest(plan) == binding["plan_digest"], "run contract changed")
    idle(root)
    data = owned(root, "result.json")
    files["result.json"] = sha(data)
    result = strict(data)
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
    roles = result.get("roles")
    role = roles.get(role_id) if isinstance(roles, dict) else None
    require(isinstance(role, dict) and role.get("execution_ok") is True
            and role.get("coverage_gaps") == [], "contribution/evidence incomplete")
    policy = role_binding["execution"]
    require(policy["sandbox"] == "read-only" and policy["external_tools"] == "deny"
            and policy["network"] is False, "writable/external work needs a captured final-state check")
    require(policy["continuity"] == "fresh", "a resumed thread can depend on earlier history; use a fresh run")
    require(role.get("requested_execution") == policy, "result policy mismatch")
    snap = plan["snapshot"]
    verify_tree(root / "baseline", snap["files"], snap["dirs"])
    verify_tree(root / role_binding["workspace"], snap["files"], snap["dirs"])
    attempts = role.get("attempts")
    require(isinstance(attempts, list) and bool(attempts)
            and all(isinstance(item, dict) for item in attempts)
            and attempts[-1].get("turn_completed_observed") is True, "no completed turn")
    logs = {}  # stdout file -> the bytes hashed, scanned below
    for attempt in attempts:
        require(attempt.get("evidence_complete") is True and attempt.get("coverage_gaps") == [],
                "incomplete raw evidence")
        sums = attempt.get("sha256")
        require(isinstance(sums, dict), "invalid raw evidence checksums")
        for channel in ("stdout", "stderr"):
            name = attempt.get(f"{channel}_file")
            require(isinstance(name, str) and Path(name).name == name, "invalid evidence filename")
            relative = f"{role_binding['evidence']}/{name}"
            raw = owned(root, relative)
            files[relative] = sha(raw)
            require(files[relative] == sums.get(channel), "raw evidence checksum mismatch")
            if channel == "stdout":
                logs[name] = raw
    if rule["kind"] == "command":
        name, index = ref.get("attempt_file"), ref.get("command_index")
        require(isinstance(name, str) and re.fullmatch(r"attempt-[A-Za-z0-9-]+\.json", name) is not None
                and type(index) is int and index >= 0, "command evidence needs attempt_file and command_index")
        relative = f"{role_binding['evidence']}/{name}"
        data = owned(root, relative)
        files[relative] = sha(data)
        attempt = strict(data)
        commands = attempt.get("commands") if attempt in attempts else None
        require(isinstance(commands, list) and index < len(commands)
                and isinstance(commands[index], dict), "command not in recorded attempt")
        command = commands[index]
        code, line = command.get("reported_exit_code"), command.get("line")
        require(type(code) is int and type(line) is int and line > 0, "unknown command outcome")
        # Tie the summary to its stored JSONL record, not just its index.
        records = logs[attempt["stdout_file"]].split(b"\n")
        event = strict(records[line - 1]) if line <= len(records) else None
        require(isinstance(event, dict) and event.get("type") == "item.completed", "missing command event")
        item = event.get("item")
        require(isinstance(item, dict) and item.get("type") == "command_execution"
                and type(item.get("exit_code")) is int and item["exit_code"] == code
                and item.get("command") == command.get("command"), "command summary differs from raw event")
        supported = code == rule["expected_exit"]
        require(supported == (outcome["outcome"] == "supported"), "outcome contradicts declared exit predicate")
    else:
        require(set(ref) == {"run_id", "role_id"}, "judgment evidence uses the contribution, not a command")
        interpretation = role.get("interpretation")
        require(isinstance(interpretation, dict) and text(interpretation.get("text"), 10**9),
                "no contribution text")
    return {"run_id": ref["run_id"], "role_id": role_id, "files": files,
            "kind": rule["kind"], "provenance": "codex-protocol" if rule["kind"] == "command"
            else "host-judgment-of-collaborator-text"}


def record(directory: str | Path, decision: dict, current: dict) -> dict:
    """Validate and append a host-authored position; it never gates the host."""
    fields(decision, {"expected_revision", "recorded_by", "host_position", "rationale", "outcomes"})
    require(type(decision["expected_revision"]) is int and decision["expected_revision"] >= 0,
            "expected_revision must be a nonnegative integer")
    require(text(decision["recorded_by"], 200) and text(decision["rationale"]), "recorded_by and rationale required")
    require(decision["host_position"] in HOST_POSITIONS, "invalid host_position")
    with locked(directory) as root:
        state = read(root)
        current = contract(current)
        position = decision["host_position"]
        if position == "set_aside":  # claims no new evidence; survives changed criteria or lost runs
            require(all(current[key] == state["contract"][key]
                        for key in ("task_id", "decision_id")), "decision identity changed")
        else:
            require(current == state["contract"], "decision contract changed; declare a new receipt")
        require(decision["expected_revision"] == len(state["history"]), "revision conflict; reread before updating")
        rules = {rule["id"]: rule for rule in state["contract"]["criteria"]}
        outcomes = decision["outcomes"]
        require(isinstance(outcomes, list), "outcomes must be an array")
        seen, observations = {}, []
        for outcome in outcomes:
            fields(outcome, {"criterion_id", "outcome"}, {"evidence"})
            key = outcome["criterion_id"]
            require(isinstance(key, str) and key in rules and key not in seen, "unknown/duplicate criterion")
            require(outcome["outcome"] in OUTCOMES, "invalid outcome")
            seen[key] = outcome["outcome"]
            if outcome["outcome"] in ("supported", "contradicted"):
                require("evidence" in outcome, "supported/contradicted outcomes need evidence")
                observations.append(evidence(state, rules[key], outcome))
            else:
                require("evidence" not in outcome, "blocked/unassessed outcomes must not claim usable evidence")
        if position == "set_aside":
            require(not outcomes, "set_aside has no new evidence; explain it in rationale")
        else:
            require(set(seen) == set(rules), "account for every declared criterion")
            if position == "use_for_now":
                require(set(seen.values()) == {"supported"}, "use_for_now requires every criterion supported")
                for stored in state["runs"].values():
                    _, plan, _ = checked_plan(stored["directory"])
                    require(digest(plan) == stored["plan_digest"], "run contract changed")
                    require(freshness(plan["snapshot"])["state"] == "unchanged",
                            "current subject is stale or unknown")
            elif position == "needs_evidence":
                require(any(v in ("blocked", "unassessed") for v in seen.values()),
                        "needs_evidence requires a gap")
            # revisit needs no failed criterion: the host may change course.
        event = {**decision, "revision": len(state["history"]) + 1,
                 "evidence": observations, "current_contract_sha256": digest(current),
                 "authority": "self-attributed-host-position-not-approval"}
        state["history"].append(event)  # append-only; applicability is computed by status
        save(root, state)
        return {"revision": event["revision"], "host_position": position,
                "decision_owner": "host", "authority": "advisory-only"}


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
        if latest is None:
            gaps.append("no-recorded-position")
        elif any(item["outcome"] in ("blocked", "unassessed") for item in latest["outcomes"]):
            gaps.append("criteria-without-evidence")
        for stored in state["runs"].values():
            try:
                run, plan, _ = checked_plan(stored["directory"])
                require(digest(plan) == stored["plan_digest"], "run contract changed")
                verify_tree(run / "baseline", plan["snapshot"]["files"], plan["snapshot"]["dirs"])
                observed = freshness(plan["snapshot"])["state"]
                if observed != "unchanged":
                    gaps.append("captured-inputs-" + observed)
                    if binding != "stale":  # a known change outranks unknown
                        binding = "stale" if observed == "stale" else "unknown"
            except (OSError, ValueError, KeyError, TypeError):
                gaps.append("capture-unavailable-or-changed")
                if binding != "stale":
                    binding = "unknown"
        for observed in latest["evidence"] if latest else ():
            try:
                run = private_dir(state["runs"][observed["run_id"]]["directory"])
                for path, expected in observed["files"].items():
                    require(sha(owned(run, path, private=False)) == expected, "evidence changed")
                _, plan, _ = checked_plan(run)
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
                  "recorded_host_position": latest["host_position"] if latest else "not-recorded",
                  "binding_status": binding,
                  "decision_owner": "host", "authority": "advisory-only",
                  "gaps": sorted(set(gaps)), "details_omitted": False}
        details = {"question": state["contract"]["question"],
                   "rationale": latest["rationale"] if latest else None,
                   "recorded_by": latest["recorded_by"] if latest else None,
                   "criterion_outcomes": {item["criterion_id"]: item["outcome"]
                                          for item in latest["outcomes"]} if latest else {},
                   "record": str(root / "receipt.json")}
        for key, value in details.items():
            if len(canonical({**result, key: value})) + 1 <= max_bytes:
                result[key] = value
            else:
                result["details_omitted"] = True
        # Never truncate the record or drop its top-level binding status.
        require(len(canonical(result)) + 1 <= max_bytes, "summary budget too small for status fields")
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=RAW)
    commands = parser.add_subparsers(dest="command", required=True)
    summary = "freeze a decision contract before its prepared runs launch"
    begin = commands.add_parser("declare", help=summary, description=summary,
                                epilog=DECLARE_HELP, formatter_class=RAW)
    begin.add_argument("--store", required=True, help="private directory outside the source and run trees")
    begin.add_argument("--contract", required=True, help="decision contract JSON")
    begin.add_argument("--run", action="append", required=True, help="prepared run directory; repeatable")
    summary = "append the host's position after validating its cited evidence"
    note = commands.add_parser("record", help=summary, description=summary,
                               epilog=RECORD_HELP, formatter_class=RAW)
    note.add_argument("--store", required=True, help="the declared receipt directory")
    note.add_argument("--decision", required=True, help="host decision JSON")
    note.add_argument("--contract", required=True, help="current decision contract JSON")
    summary = "recall one explicitly selected record against current inputs"
    recall = commands.add_parser("status", help=summary, description=summary,
                                 epilog=STATUS_HELP, formatter_class=RAW)
    recall.add_argument("--store", required=True, help="the declared receipt directory")
    recall.add_argument("--contract", help="current decision contract JSON; omitted reads unknown")
    recall.add_argument("--max-bytes", type=int, default=2048, help="summary budget, 512-16384 (default 2048)")
    args = parser.parse_args(argv)
    try:
        if args.command == "declare":
            result = declare(args.store, read_json(Path(args.contract), private=False), args.run)
        elif args.command == "record":
            result = record(args.store, read_json(Path(args.decision), private=False),
                            read_json(Path(args.contract), private=False))
        else:
            current = read_json(Path(args.contract), private=False) if args.contract else None
            result = status(args.store, current, max_bytes=args.max_bytes)
        sys.stdout.buffer.write(canonical(result) + b"\n")
        if args.command != "status":
            return 0
        return 0 if (result["binding_status"], result["recorded_host_position"]) == ("match", "use_for_now") else 1
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        print(f"decision-receipts: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
