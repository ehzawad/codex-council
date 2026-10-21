"""Runner observations and durable tool transcripts, separate from model claims.

JSONL command records are observations of Codex's protocol, not independently
attested OS executions. A successful transport, a well-formed response, and a
completed verification are different facts. Nothing here judges substantive
claims or automatically applies a candidate patch.
"""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import json
import os
import platform
import secrets
import sys
import time
from pathlib import Path
from typing import Any

from council_workspace import (atomic_json, digest, freshness,
                               inventory, private_dir, read_json, regular,
                               verify_tree)

MAX_SUMMARY_LINE = 8 * 1024 * 1024
MAX_COMMAND_SUMMARIES = 10000
PATCH_FILE_LIMIT = 1024 * 1024


class AttemptEvidence:
    """Spool raw bytes while a worker is running, including interrupted runs.

    Summary bounds do not truncate the raw logs. Disk failure is reported as a
    coverage gap; the caller must never claim complete evidence after it.
    """

    def __init__(self, directory: Path, argv: list[str], prompt: str):
        private_dir(directory)
        self.directory = directory
        self.key = f"attempt-{time.time_ns()}-{secrets.token_hex(4)}"
        self.started = time.monotonic()
        self.streams: dict[str, Any] = {}
        self.hashes = {name: hashlib.sha256() for name in ("stdout", "stderr")}
        self.sizes = dict.fromkeys(("stdout", "stderr"), 0)
        self.errors: list[str] = []
        self.pending = bytearray()
        self.dropping = False
        self.line = 0
        self.event_count = 0
        self.commands: list[dict[str, Any]] = []
        self.completed = False
        self.closed = False
        self.meta: dict[str, Any] = {
            "schema": "codex-council/attempt@1", "argv": argv,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "started_at": time.time(), "observation_source": "runner-and-codex-jsonl",
        }
        try:
            for name in ("stdout", "stderr"):
                suffix = "jsonl" if name == "stdout" else "log"
                path = directory / f"{self.key}.{name}.{suffix}"
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600)
                self.streams[name] = os.fdopen(fd, "wb", buffering=0)
                self.meta[f"{name}_file"] = path.name
            atomic_json(directory / f"{self.key}.json", {**self.meta, "state": "running"})
        except BaseException:
            for stream in self.streams.values():
                stream.close()
            raise

    def spawned(self, pid: int, pgid: int) -> None:
        self.meta.update(pid=pid, pgid=pgid)

    def feed(self, name: str, block: bytes) -> None:
        self.hashes[name].update(block)
        self.sizes[name] += len(block)
        stream = self.streams.get(name)
        if stream is not None:
            try:
                stream.write(block)
            except OSError:
                self.errors.append(f"{name}-write-failed")
                with contextlib.suppress(OSError):
                    stream.close()
                self.streams[name] = None
        if name == "stdout":
            # Split only the new chunk; never repeatedly rescan an unbounded
            # prefix. Drop oversized records from the summary, not raw storage.
            parts = block.split(b"\n")
            for index, part in enumerate(parts):
                if not self.dropping:
                    if len(self.pending) + len(part) > MAX_SUMMARY_LINE:
                        self.pending.clear()
                        self.dropping = True
                        self.errors.append("summary-record-too-large")
                    else:
                        self.pending.extend(part)
                if index < len(parts) - 1:
                    self.line += 1
                    if not self.dropping:
                        self._event(bytes(self.pending), self.line)
                    self.pending.clear()
                    self.dropping = False

    def _event(self, raw: bytes, line: int) -> None:
        if not raw.strip():
            return
        try:
            event = json.loads(raw)
        except (ValueError, RecursionError):
            self.errors.append("unparsed-protocol-record")
            return
        if not isinstance(event, dict):
            self.errors.append("unparsed-protocol-record")
            return
        self.event_count += 1
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
            self.meta.setdefault("thread_id", event["thread_id"])
        if event.get("type") == "turn.completed":
            self.completed = True
            usage = event.get("usage")
            if isinstance(usage, dict):
                self.meta["reported_usage"] = {
                    key: value for key, value in usage.items()
                    if isinstance(key, str) and type(value) is int and value >= 0
                }
        if event.get("type") != "item.completed":
            return
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "command_execution":
            return
        if len(self.commands) >= MAX_COMMAND_SUMMARIES:
            self.errors.append("command-summary-limit")
            return
        output = item.get("aggregated_output")
        command = item.get("command")
        code = item.get("exit_code")
        output_hash = None
        if isinstance(output, str):
            try:
                output_hash = hashlib.sha256(output.encode("utf-8")).hexdigest()
            except UnicodeEncodeError:
                self.errors.append("command-output-not-utf8")
        self.commands.append({
            "source": "codex-jsonl", "line": line,
            "command": command if isinstance(command, str) else None,
            "reported_exit_code": code if type(code) is int else None,
            "output_sha256": output_hash,
        })

    def finish(self, *, returncode: int | None, state: str,
               stalled: bool = False, unsafe_to_replay: bool = True) -> dict[str, Any]:
        if self.closed:
            return self.meta
        self.closed = True
        if self.pending and not self.dropping:
            self.line += 1
            self._event(bytes(self.pending), self.line)
        for name, stream in self.streams.items():
            if stream is None:
                continue
            try:
                stream.flush()
                os.fsync(stream.fileno())
            except OSError:
                self.errors.append(f"{name}-flush-failed")
            finally:
                with contextlib.suppress(OSError):
                    stream.close()
        self.meta.update(
            state=state, transport_exit=returncode,
            elapsed_seconds=time.monotonic() - self.started,
            turn_completed_observed=self.completed,
            unsafe_to_replay=unsafe_to_replay, stalled=stalled,
            event_count=self.event_count, commands=self.commands,
            bytes=self.sizes, sha256={name: h.hexdigest() for name, h in self.hashes.items()},
            coverage_gaps=sorted(set(self.errors)), evidence_complete=not self.errors,
        )
        try:
            atomic_json(self.directory / f"{self.key}.json", self.meta)
        except OSError:
            self.meta["evidence_complete"] = False
            self.meta["coverage_gaps"].append("attempt-metadata-write-failed")
        return self.meta


def _bytes(root: Path, name: str) -> bytes:
    with regular(root, name) as stream:
        return stream.read(PATCH_FILE_LIMIT + 1)


def collect_artifacts(baseline: Path, workspace: Path, snapshot: dict[str, Any],
                      evidence_dir: Path) -> dict[str, Any]:
    """Inspect changed files without following links or executing artifacts.
    Large/binary/mode-only changes stay in the manifest, not a lossy text patch.
    """
    files, _, omitted = inventory(workspace, ["."], [])
    changed = sorted(name for name in set(files) | set(snapshot["files"])
                     if files.get(name) != snapshot["files"].get(name))
    artifacts, patches = [], []
    for name in changed:
        before, after = snapshot["files"].get(name), files.get(name)
        entry = {"path": name, "before": before, "after": after,
                 "kind": "added" if before is None else "deleted" if after is None
                 else "modified", "text_patch": False}
        if (any(ord(ch) < 32 for ch in name)
                or max((before or {}).get("bytes", 0), (after or {}).get("bytes", 0))
                > PATCH_FILE_LIMIT):
            artifacts.append(entry)
            continue
        old = _bytes(baseline, name) if before else b""
        new = _bytes(workspace, name) if after else b""
        try:
            if b"\0" in old or b"\0" in new:
                raise UnicodeError("binary artifact")
            a, b = old.decode("utf-8"), new.decode("utf-8")
        except UnicodeError:
            artifacts.append(entry)
            continue
        if a != b:
            for line in difflib.unified_diff(
                    a.splitlines(keepends=True), b.splitlines(keepends=True),
                    fromfile=f"a/{name}" if before else "/dev/null",
                    tofile=f"b/{name}" if after else "/dev/null"):
                patches.append(line)
                if not line.endswith("\n"):
                    patches.append("\n\\ No newline at end of file\n")
            entry["text_patch"] = True
        artifacts.append(entry)
    # Human-inspectable candidate only. Exact bytes remain in the workspace;
    # application and integration checks belong to the orchestrator.
    path = evidence_dir / "candidate.diff"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
        stream.writelines(patches)
    return {"files": artifacts, "omitted": omitted, "candidate_diff": path.name,
            "application": "never automatic; inspect exact workspace bytes"}


def write_result(run_dir: Path, plan: dict[str, Any], results: list[Any],
                 *, execution_state: str = "done") -> dict[str, Any]:
    by_id = {result.role.id: result for result in results}
    manifest = plan["snapshot"]
    gaps: list[str] = []
    try:
        verify_tree(run_dir / "baseline", manifest["files"])
        baseline_intact = True
    except (OSError, ValueError):
        baseline_intact = False
        gaps.append("baseline-integrity-not-established")
    application = freshness(manifest)
    try:
        if hashlib.sha256((run_dir / "context.md").read_bytes()).hexdigest() != plan["context_sha256"]:
            application = {"state": "stale", "reason": "context changed since capture"}
    except OSError:
        application = {"state": "unknown", "reason": "context unavailable"}
        gaps.append("context-unavailable-at-reconciliation")
    roles = {}
    for role_id, binding in plan["roles"].items():
        result = by_id.get(role_id)
        directory = run_dir / binding["evidence"]
        attempts = []
        for path in sorted(directory.glob("attempt-*.json")):
            try:
                attempts.append(read_json(path))
            except (OSError, ValueError):
                gaps.append(f"{role_id}:attempt-metadata-unreadable")
        role_gaps = []
        if not attempts:
            role_gaps.append("no-attempt-evidence")
        if any(not attempt.get("evidence_complete") for attempt in attempts):
            role_gaps.append("incomplete-attempt-evidence")
        artifacts = None
        if execution_state == "done" and baseline_intact:
            try:
                artifacts = collect_artifacts(run_dir / "baseline",
                                              run_dir / binding["workspace"], manifest,
                                              directory)
            except (OSError, ValueError):
                role_gaps.append("artifact-inspection-incomplete")
        role_ok = bool(result is not None and result.ok)
        required = binding["execution"]["required"]
        if required and (not role_ok or role_gaps):
            gaps.append(f"{role_id}:required-contribution-incomplete")
        roles[role_id] = {
            "execution_ok": role_ok, "required": required,
            "requested_execution": binding["execution"],
            "execution_policy_evidence": f"{binding['evidence']}/execution-policy.json",
            "sent_model": getattr(getattr(result.role, "decision", None),
                                  "dispatch_model", None) if result else None,
            "sent_effort": getattr(getattr(result.role, "decision", None),
                                   "dispatch_effort", None) if result else None,
            "model_provenance": "sent overrides, not serving-model attestation",
            "attempts": attempts, "artifacts": artifacts,
            "coverage_gaps": role_gaps,
            "interpretation": {
                "source": "collaborator", "text": result.text if result else None,
                "error": result.error if result else "no settled reply",
                "verification": "requires-orchestrator-reconciliation",
            },
        }
    result = {
        "schema": "codex-council/result@1", "run_id": plan["run_id"],
        "task_id": plan["task_id"], "source_root": manifest["source_root"],
        "snapshot_id": manifest["snapshot_id"], "context_sha256": plan["context_sha256"],
        "execution_state": execution_state, "baseline_intact": baseline_intact,
        "applicability": application, "coverage_gaps": gaps,
        "required_contributions_complete": execution_state == "done" and not gaps,
        "verification": "not-inferred-from-exit-status", "roles": roles,
        "environment": {"system": platform.system(), "release": platform.release(),
                        "python": sys.version.split()[0]},
    }
    result["result_digest"] = digest(result)
    atomic_json(run_dir / "result.json", result)
    return result
