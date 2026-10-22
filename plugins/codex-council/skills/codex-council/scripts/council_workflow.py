"""Versioned invocation contracts for the council supervisor.

The orchestrator owns task interpretation and whether to invoke. This module
owns capture, task identity, per-role workspace bindings and metadata discovery.
There is deliberately no turn counter, predefined role set, ranking, or debate.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any

from council_policy import inspect_policy, inspect_service_tier, parse_policy, policy_dict
from council_workspace import (WorkspaceError, atomic_json, capture, copy_workspace,
                               digest, freshness, private_dir, private_mkdirs,
                               read_json,
                               relative_path, snapshot_digest, verify_tree)

REQUEST_SCHEMA = "codex-council/request@1"
PLAN_SCHEMA = "codex-council/run@1"
PLAN_FILE = "run.json"
REQUEST_KEYS = frozenset({"schema", "task_id", "reason", "source_root", "include", "exclude"})
TASK_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]*\Z")


def role_key(role_id: str) -> str:
    if len(role_id) <= 32 and re.fullmatch(r"[a-z0-9_-]+", role_id):
        return role_id
    return "role-sha256-" + hashlib.sha256(role_id.encode("utf-8")).hexdigest()


def _request(run_dir: Path, *, strict_source: bool = True) -> dict[str, Any]:
    value = read_json(run_dir / "request.json", private=False)
    if not isinstance(value, dict) or set(value) - REQUEST_KEYS:
        raise WorkspaceError("request.json contains an unknown field or is not an object")
    if value.get("schema") != REQUEST_SCHEMA:
        raise WorkspaceError(f"request.json needs schema {REQUEST_SCHEMA}")
    if not isinstance(value.get("task_id"), str) or not TASK_ID.fullmatch(value["task_id"]):
        raise WorkspaceError("task_id must be a stable task-specific ASCII identifier")
    if not isinstance(value.get("reason"), str) or not value["reason"].strip():
        raise WorkspaceError("reason must explain how this invocation can advance the task")
    source = value.get("source_root")
    if not isinstance(source, str) or not os.path.isabs(source):
        raise WorkspaceError("source_root must be an absolute directory path")
    try:
        value["source_root"] = str(Path(source).resolve(strict=True))
    except RuntimeError:  # a symlink loop, before Python 3.13
        if strict_source:
            raise WorkspaceError(f"source_root is a symlink loop: {source}") from None
    except OSError:
        if strict_source:
            raise
        # --freshness: a source that no longer resolves stays as spelled, so
        # it reads as missing; never resolved lexically ("gone-link/..").
    for key in ("include", "exclude"):
        entries = value.get(key)
        if entries is not None and (not isinstance(entries, list)
                                    or not all(isinstance(item, str) and item for item in entries)):
            raise WorkspaceError(f"{key} must be an array of nonempty strings")
    if value.get("include") is not None:
        for item in value["include"]:
            relative_path(item, allow_dot=True)
    value.setdefault("include", None)
    if value.get("exclude") is None:
        value["exclude"] = []  # null means no exclusions, as capture records it
    return value


def role_contract(role: Any) -> dict[str, Any]:
    return {"id": role.id, "label": role.label, "instruction": role.instruction,
            "execution": policy_dict(role.execution)}


def _context_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def prepare(run_dir: str, roles: list[Any], body: str) -> dict[str, Any]:
    directory = private_dir(run_dir)
    request = _request(directory)
    source = Path(request["source_root"])
    if directory.is_relative_to(source) or source.is_relative_to(directory):
        raise WorkspaceError("source and run directory must be separate, non-overlapping trees")
    if not body.strip():
        raise WorkspaceError("context must contain the current goal and contribution request")
    if not roles or len({role.id for role in roles}) != len(roles):
        raise WorkspaceError("prepare needs a nonempty panel with unique ids")
    contracts = {role.id: role_contract(role) for role in roles}
    for name in ("baseline", "workspaces", "evidence", "catalogs", PLAN_FILE,
                 "out.md", "err.log", "replies", "supervisor.lock", "supervisor.json"):
        if os.path.lexists(directory / name):
            raise WorkspaceError(f"run directory is already prepared or launched ({name})")
    # A failed capture consumes this directory, even if it never wrote run.json.
    claim = os.open(directory / "prepare.lock", os.O_WRONLY | os.O_CREAT
                    | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(claim, 0o600)  # 0600 whatever the umask
    finally:
        os.close(claim)
    snapshot = capture(source, directory / "baseline", include=request["include"],
                       exclude=request["exclude"])
    for name in ("workspaces", "evidence", "catalogs"):
        private_mkdirs(directory / name)
    bindings = {}
    for role in roles:
        key = role_key(role.id)
        relative = f"workspaces/{key}"
        copy_workspace(directory / "baseline", directory / relative, snapshot)
        private_mkdirs(directory / "evidence" / key)
        private_mkdirs(directory / "catalogs" / key)
        bindings[role.id] = {
            "workspace": relative, "evidence": f"evidence/{key}",
            "catalog": f"catalogs/{key}", "contract_sha256": digest(contracts[role.id]),
            "execution": contracts[role.id]["execution"],
        }
    plan = {"schema": PLAN_SCHEMA, "run_id": secrets.token_hex(16),
            "task_id": request["task_id"], "reason": request["reason"],
            "request_sha256": digest(request), "context_sha256": _context_hash(body),
            "context_bytes": len(body.encode("utf-8")), "snapshot": snapshot,
            "roles": bindings, "backend": "private-copy",
            "limitations": ["observed-stable capture is not an atomic filesystem snapshot",
                            "agent config directories, agent instruction files "
                            "(AGENTS.md, CLAUDE.md and their override/local variants), "
                            "credentials and caches are not copied",
                            "sandbox restricts command writes, not all host reads",
                            "remote data and dependencies are outside the capture"]}
    atomic_json(directory / PLAN_FILE, plan)
    return plan


def load_plan(run_dir: str | Path, roles: list[Any] | None = None,
              body: str | None = None, *, verify_inputs: bool = False,
              strict_source: bool = True) -> dict[str, Any] | None:
    directory = private_dir(run_dir)
    path = directory / PLAN_FILE
    if not os.path.lexists(path):
        if os.path.lexists(directory / "prepare.lock"):
            raise WorkspaceError("capture did not finish; start over in a new directory")
        return None
    plan = read_json(path)
    if not isinstance(plan, dict) or plan.get("schema") != PLAN_SCHEMA:
        raise WorkspaceError("unsupported or invalid run.json")
    request = _request(directory, strict_source=strict_source)
    recorded = plan.get("snapshot")
    if (not strict_source and isinstance(recorded, dict)
            and not os.path.isdir(request["source_root"])):
        # --freshness only: a source gone with a link it was spelled
        # through no longer resolves to the recorded root, so the rest of
        # the request is checked against that root (freshness: unknown).
        request["source_root"] = recorded.get("source_root")
    if plan.get("request_sha256") != digest(request):
        raise WorkspaceError("request changed after capture; prepare a fresh invocation")
    snapshot, bindings = plan.get("snapshot"), plan.get("roles")
    if not isinstance(snapshot, dict) or not isinstance(bindings, dict):
        raise WorkspaceError("invalid captured inputs or role bindings")
    if (plan.get("task_id") != request["task_id"]
            or plan.get("reason") != request["reason"]
            or any(snapshot.get(key) != request[key]
                   for key in ("source_root", "include", "exclude"))):
        raise WorkspaceError("captured task or source binding differs from request.json")
    if (not isinstance(snapshot.get("dirs"), list)
            or snapshot.get("snapshot_id") != snapshot_digest(snapshot)):
        raise WorkspaceError("captured input manifest checksum does not match")
    for role_id, binding in bindings.items():
        if not isinstance(role_id, str) or not isinstance(binding, dict):
            raise WorkspaceError("invalid role binding")
        key = role_key(role_id)
        for field, expected in (("workspace", f"workspaces/{key}"),
                                ("evidence", f"evidence/{key}"),
                                ("catalog", f"catalogs/{key}")):
            if binding.get(field) != expected:
                raise WorkspaceError("role path is not this run's deterministic private path")
            private_dir(directory / expected)
        parse_policy(binding.get("execution"))
    if roles is not None:
        if set(bindings) != {role.id for role in roles}:
            raise WorkspaceError("panel changed after capture; prepare a new invocation")
        for role in roles:
            contract = role_contract(role)
            binding = bindings[role.id]
            if (binding.get("contract_sha256") != digest(contract)
                    or binding.get("execution") != contract["execution"]):
                raise WorkspaceError(f"{role.id}: task or permissions changed after capture")
    if body is not None and plan.get("context_sha256") != _context_hash(body):
        raise WorkspaceError("context changed after capture; prepare a fresh invocation")
    if verify_inputs:
        verify_tree(directory / "baseline", snapshot["files"], snapshot["dirs"])
        for binding in bindings.values():
            verify_tree(directory / binding["workspace"], snapshot["files"], snapshot["dirs"])
    return plan


def _require_plan(run_dir: str, roles: list[Any], body: str) -> dict[str, Any]:
    plan = load_plan(run_dir, roles, body, verify_inputs=True)
    if plan is None:
        raise WorkspaceError("this directory has no completed capture; write "
                             "request.json and run --prepare first")
    return plan


def bind_roles(run_dir: str, roles: list[Any], body: str, *, at_launch: bool,
               probe_policy: bool = True, on_progress=None
               ) -> tuple[list[Any], dict[str, Any]]:
    plan = _require_plan(run_dir, roles, body)
    directory = private_dir(run_dir)
    bound = []
    for role in roles:
        binding = plan["roles"][role.id]
        policy = parse_policy(binding["execution"])
        workspace = str(directory / binding["workspace"])
        if on_progress is not None:
            on_progress()
        checked = inspect_policy(workspace, policy) if probe_policy else {"overrides": []}
        if on_progress is not None:
            on_progress()
        # Reobserved at launch; a preflight approval is not carried blindly
        # across a config or installation change.
        if at_launch:
            atomic_json(directory / binding["evidence"] / "execution-policy.json", checked)
        bound.append(dataclasses.replace(
            role, execution=binding["execution"], workdir=workspace,
            evidence_dir=str(directory / binding["evidence"]),
            execution_overrides=tuple(checked["overrides"]),
            task_id=plan["task_id"], input_snapshot=plan["snapshot"]["snapshot_id"],
        ))
    return bound, plan


def discover_prepared(run_dir: str, roles: list[Any], body: str) -> None:
    """Each role of a prepared run owns its model snapshot, discovered in the
    same resolved workspace path it executes in. Each summary ends with the
    snapshot path under the role's catalog directory recorded in run.json
    (a hashed role key for ids longer than 32 characters)."""
    plan = _require_plan(run_dir, roles, body)
    directory = private_dir(run_dir)
    from council_discovery import _discover_command

    for role in roles:
        binding = plan["roles"][role.id]
        _discover_command(str(directory / binding["catalog"]),
                          project_root=str(directory / binding["workspace"]))


def resolve_selections(roles: list[Any], run_dir: str, routing_mode: str,
                       at_launch: bool, body: str, *, probe_policy: bool = True,
                       on_progress=None) -> tuple[list[Any], Any, Any]:
    from council_selection import _resolve_run_selections

    bound, plan = bind_roles(run_dir, roles, body, at_launch=at_launch,
                            probe_policy=probe_policy, on_progress=on_progress)
    resolved, snapshots = [], []
    for role in bound:
        binding = plan["roles"][role.id]
        if on_progress is not None:
            on_progress()
        selected, snapshot = _resolve_run_selections(
            [role], str(Path(run_dir) / binding["catalog"]), routing_mode,
            at_launch, project_root=role.workdir)
        if role.service_tier is not None and probe_policy:
            decision = selected[0].decision
            checked = inspect_service_tier(
                role.workdir, role.execution_overrides,
                model=decision.dispatch_model, effort=decision.dispatch_effort,
                tier=decision.dispatch_service_tier, native_model=decision.native_model)
            if at_launch:
                atomic_json(Path(role.evidence_dir) / "service-tier.json", checked)
        if on_progress is not None:
            on_progress()
        resolved.extend(selected)
        if snapshot is not None:
            snapshots.append(snapshot)
            atomic_json(Path(role.evidence_dir) / "launch-model-snapshot.json", snapshot)
    # The report renderer takes one discovery summary. Never imply a single
    # catalog governed all workers: per-role evidence stays in each directory.
    launch = None
    if snapshots:
        versions = {s["context"]["codex_cli_version"] for s in snapshots}
        problems = list(dict.fromkeys(p for s in snapshots for p in s["problems"]))
        eligible = all(s["routing"]["eligible"] for s in snapshots)
        launch = {"status": "ok" if all(s["status"] == "ok" for s in snapshots)
                  else "unavailable", "problems": problems,
                  "context": {"codex_cli_version": next(iter(versions)) if len(versions) == 1
                              else "mixed; see per-role evidence"},
                  "routing": {"eligible": eligible,
                              "reasons": [] if eligible else ["per-role discovery; see evidence"]}}
    return resolved, launch, plan


def plan_lines(plan: dict[str, Any], roles: list[Any], stall_secs: int) -> list[str]:
    watchdog = f"{stall_secs}s" if stall_secs else "off"
    unrestricted = any(parse_policy(role.execution).sandbox == "bypass" for role in roles)
    write_scope = "UNRESTRICTED" if unrestricted else "scratch-command-writes-only"
    lines = [f"[codex-council] execution plan: roles={len(roles)} "
             f"context_bytes={plan['context_bytes']} watchdog={watchdog} "
             f"workspace=private-copy-per-role; write_scope={write_scope}; "
             "permissions=observed-before-launch-not-runtime-attested",
             "[codex-council] configuration context: scratch cwd; source .codex/.claude/"
             ".agents directories and AGENTS.md/CLAUDE.md instruction files are not "
             "copied; inspect the actual native-model discovery"]
    for role in roles:
        policy = parse_policy(role.execution)
        lines.append(f"[codex-council] execution plan: {role.id}: "
                     f"sandbox={policy.sandbox} network={policy.network} "
                     f"external_tools={policy.external_tools} continuity={policy.continuity} "
                     f"required={policy.required}")
    return lines


def freshness_command(run_dir: str) -> int:
    directory = private_dir(run_dir)
    # A moved or deleted source is a freshness answer (unknown), not a
    # contract error.
    plan = load_plan(directory, strict_source=False)
    if plan is None:
        raise WorkspaceError("this run has no captured inputs")
    snapshot = plan["snapshot"]
    source = _request(directory, strict_source=False)["source_root"]
    if not os.path.isdir(source):
        # The source request.json names is gone, though the recorded root
        # may outlive a link it was reached through: unknown, named as
        # spelled.
        snapshot = {**snapshot, "source_root": source}
    result = freshness(snapshot)
    try:
        context = (directory / "context.md").read_bytes()
        if hashlib.sha256(context).hexdigest() != plan["context_sha256"]:
            result.update(state="stale", reason="staged context changed")
    except OSError:
        result["reason"] = "context unavailable"
        if result["state"] != "stale":
            result["state"] = "unknown"
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result["state"] == "unchanged" else 1
