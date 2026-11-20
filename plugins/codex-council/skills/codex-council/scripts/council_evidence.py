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
import io
import json
import os
import platform
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Any

from council_workspace import (WorkspaceError, atomic_json, digest, freshness,
                               inventory, private_dir, read_json, regular,
                               verify_tree, worktree_state)

MAX_SUMMARY_LINE = 8 * 1024 * 1024
MAX_COMMAND_SUMMARIES = 10000
PATCH_FILE_LIMIT = 1024 * 1024
LIVE_BEFORE_LAUNCH = "evidence/live-before-launch.json"  # council_workflow's constant

# The brief's closing line, markdown emphasis tolerated: the role's own claim.
_STATUS_LINE = re.compile(r"[*_`\s]*status[*_`\s]*:[*_`\s]*(ready|blocked|failed)(?![a-z])"
                          r"[*_`]*\s*(?:[:\-\u2013\u2014]\s*(.*?))?[*_`.!\s]*", re.IGNORECASE)


def reported_status(text: str | None) -> dict[str, Any] | None:
    """{state, detail} from a reply's last nonblank line, else None."""
    lines = [line for line in (text or "").splitlines() if line.strip()]
    match = _STATUS_LINE.fullmatch(lines[-1]) if lines else None
    if match is None:
        return None
    return {"state": match[1].lower(), "detail": match[2] or None}


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
                os.fchmod(fd, 0o600)  # 0600 whatever the umask
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
                remaining = memoryview(block)
                while remaining:
                    written = stream.write(remaining)
                    if written is None or written <= 0:
                        raise OSError("evidence stream made no write progress")
                    remaining = remaining[written:]
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


def _bytes(root: Path, name: str, expected: dict[str, Any]) -> bytes | None:
    """The file's bytes, or None when it no longer matches its inventory entry."""
    try:
        with regular(root, name) as stream:
            data = stream.read(PATCH_FILE_LIMIT + 1)
            executable = bool(os.fstat(stream.fileno()).st_mode & 0o111)
    except (OSError, WorkspaceError):  # gone, replaced or made unreadable
        return None
    if (len(data) != expected["bytes"] or executable != expected["executable"]
            or hashlib.sha256(data).hexdigest() != expected["sha256"]):
        return None
    return data


def collect_artifacts(baseline: Path, workspace: Path, snapshot: dict[str, Any],
                      evidence_dir: Path, *, mode: str = "copy") -> dict[str, Any]:
    """Inspect changed files without following links or executing artifacts.

    The manifest covers added, removed and changed non-omitted regular files,
    executable-flag changes included; other permission changes and
    empty-directory changes are not recorded. Large, binary and
    executable-flag-only changes stay in the manifest, not a lossy text patch.
    A symlink, special file, nested repository, submodule or unreadable entry
    the role left is listed by path and kind, with the captured entry it
    replaced; it is never opened, followed or diffed. A file that changes or
    vanishes while it is hashed or read is flagged changed_during_inspection,
    without a patch; one gone before it is examined is a deletion. Live and
    worktree output uses the capture's selection. Output inspection permits
    names that would collide in a fresh portable capture.
    """
    special: dict[str, str] = {}
    include, exclude = ((["."], []) if mode == "copy"
                        else (snapshot["include"], snapshot["exclude"]))
    files, _, omitted, _ = inventory(workspace, include, exclude, special=special,
                                     portable=False, hints=False)
    # What lies below an unreadable directory is unknown, not deleted.
    unknown = tuple(f"{name}/" for name, kind in special.items() if kind == "unreadable")
    changed = sorted({*special, *(name for name in set(files) | set(snapshot["files"])
                                  if files.get(name) != snapshot["files"].get(name)
                                  and not name.startswith(unknown))})
    artifacts, patches = [], []
    for name in changed:
        before, after = snapshot["files"].get(name), files.get(name)
        unstable = special.get(name) == "changed-during-inspection"
        if name in special and not unstable:
            artifacts.append({"path": name, "kind": special[name], "before": before,
                              "after": None, "text_patch": False})
            continue
        entry = {"path": name, "before": before, "after": after,
                 "kind": "added" if before is None else "deleted" if after is None
                 and not unstable else "modified", "text_patch": False}
        if unstable:
            entry["changed_during_inspection"] = True
            artifacts.append(entry)
            continue
        if (any(ord(ch) < 32 for ch in name)
                or max((before or {}).get("bytes", 0), (after or {}).get("bytes", 0))
                > PATCH_FILE_LIMIT):
            artifacts.append(entry)
            continue
        old = _bytes(baseline, name, before) if before else b""
        new = _bytes(workspace, name, after) if after else b""
        if old is None or new is None:
            entry["changed_during_inspection"] = True
            artifacts.append(entry)
            continue
        try:
            if b"\0" in old or b"\0" in new:
                raise UnicodeError("binary artifact")
            a, b = old.decode("utf-8"), new.decode("utf-8")
        except UnicodeError:
            artifacts.append(entry)
            continue
        if a != b:
            for line in difflib.unified_diff(
                    io.StringIO(a).readlines(), io.StringIO(b).readlines(),
                    fromfile=f"a/{name}" if before else "/dev/null",
                    tofile=f"b/{name}" if after else "/dev/null"):
                patches.append(line)
                if not line.endswith("\n"):
                    patches.append("\n\\ No newline at end of file\n")
            entry["text_patch"] = True
        artifacts.append(entry)
    path = evidence_dir / ("live.diff" if mode == "live" else "candidate.diff")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
        os.fchmod(fd, 0o600)  # 0600 whatever the umask
        stream.writelines(patches)
    result = {"files": artifacts, "omitted": omitted}
    if mode == "live":
        result.update(live_diff=path.name, applied_to_live_tree=True,
                      application="observed in the working tree since capture; "
                                  "includes changes by other writers")
    else:
        result.update(candidate_diff=path.name,
                      application="never automatic; inspect exact workspace bytes")
    return result


def write_result(run_dir: Path, plan: dict[str, Any], results: list[Any],
                 *, execution_state: str = "done") -> dict[str, Any]:
    by_id = {result.role.id: result for result in results}
    manifest = plan["snapshot"]
    gaps: list[str] = []
    try:
        verify_tree(run_dir / "baseline", manifest["files"], manifest["dirs"])
        baseline_intact = True
    except (OSError, ValueError):
        baseline_intact = False
        gaps.append("baseline-integrity-not-established")
    # The context check refines the input comparison; it never discards its
    # file-level detail.
    modes = {role_id: binding["execution"].get("workspace", "copy")
             for role_id, binding in plan["roles"].items()}
    writers = sorted(role_id for role_id, mode in modes.items() if mode == "live")
    application = freshness(manifest, live=bool(writers))
    try:
        if hashlib.sha256((run_dir / "context.md").read_bytes()).hexdigest() != plan["context_sha256"]:
            application.update(state="stale", reason="context changed since capture")
    except OSError:
        application["reason"] = "context unavailable"
        if application["state"] != "stale":
            application["state"] = "unknown"
        gaps.append("context-unavailable-at-reconciliation")
    observed = {}
    for role_id, binding in plan["roles"].items():
        attempts, role_gaps = [], []
        for path in sorted((run_dir / binding["evidence"]).glob("attempt-*.json")):
            try:
                attempt = read_json(path)
                if not isinstance(attempt, dict) or attempt.get("schema") != "codex-council/attempt@1":
                    raise ValueError("invalid attempt evidence")
                attempts.append(attempt)
            except (OSError, ValueError):
                gaps.append(f"{role_id}:attempt-metadata-unreadable")
                role_gaps.append("attempt-metadata-unreadable")
        observed[role_id] = attempts, role_gaps

    def inspect(role_ids: list[str]) -> bool:
        # After graceful teardown a stopped writer may already have changed
        # its tree; a refusal or cancellation before any spawn left nothing.
        return baseline_intact and (execution_state == "done" or any(
            attempt.get("pid") for role_id in role_ids for attempt in observed[role_id][0]))

    live = None
    if writers and inspect(writers):
        # Live writers share one tree, so its changes are inspected once.
        try:
            live = collect_artifacts(run_dir / "baseline", Path(manifest["source_root"]),
                                     manifest, run_dir / "evidence", mode="live")
        except (OSError, ValueError):
            pass
        else:
            try:
                before = read_json(run_dir / LIVE_BEFORE_LAUNCH)
                changed_before = before.get("changed_paths") if isinstance(before, dict) else None
            except (OSError, ValueError):
                changed_before = None
            live.update(live_diff=f"evidence/{live['live_diff']}", writers=writers,
                        attribution="not per role: live writers share one tree, and "
                                    "others may have edited it too",
                        changed_before_launch=changed_before)
    roles = {}
    for role_id, binding in plan["roles"].items():
        result = by_id.get(role_id)
        mode = modes[role_id]
        directory = run_dir / binding["evidence"]
        attempts, role_gaps = observed[role_id]
        if not attempts:
            role_gaps.append("no-attempt-evidence")
        if any(not attempt.get("evidence_complete") for attempt in attempts):
            role_gaps.append("incomplete-attempt-evidence")
        role_ok = bool(result is not None and result.ok)
        # A clean exit with a message is not a finished turn: the final
        # attempt must have reported turn.completed before coverage counts.
        if role_ok and attempts and attempts[-1].get("turn_completed_observed") is not True:
            role_gaps.append("turn-completion-not-observed")
        artifacts = None
        # A copy is inspected once its council is done; a worktree keeps a
        # stopped writer's commits, so like the live tree once it spawned.
        collect = (inspect([role_id]) if mode == "worktree"
                   else baseline_intact and execution_state == "done")
        if mode == "live":
            if live is not None:  # the shared observation lives in result["live"]
                artifacts = {"live_diff": live["live_diff"], "applied_to_live_tree": True,
                             "writers": writers}
            elif inspect(writers):
                role_gaps.append("artifact-inspection-incomplete")
        elif collect:
            try:
                artifacts = collect_artifacts(run_dir / "baseline",
                                              run_dir / binding["workspace"], manifest,
                                              directory, mode=mode)
            except (OSError, ValueError):
                role_gaps.append("artifact-inspection-incomplete")
            if artifacts is not None and mode == "worktree":
                worktree = binding["worktree"]
                root = run_dir / worktree["root"]
                artifacts.update(
                    applied_to_live_tree=False,
                    application=f"on branch {worktree['branch']} in worktree {root}; "
                                "nothing was applied to your working tree; merge or apply "
                                "what you accept",
                    worktree={"path": str(root), "branch": worktree["branch"],
                              "base": worktree["base"], "repo": worktree["repo"],
                              **worktree_state(root, worktree["base"])})
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
            "sent_service_tier": getattr(getattr(result.role, "decision", None),
                                         "dispatch_service_tier", None) if result else None,
            "model_provenance": "sent overrides, not serving-model attestation",
            "service_tier_provenance": "sent override, not serving-tier attestation",
            "attempts": attempts, "artifacts": artifacts,
            "coverage_gaps": role_gaps,
            "interpretation": {
                "source": "collaborator", "text": result.text if result else None,
                "reported_status": reported_status(result.text) if result else None,
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
    if writers:
        result["live"] = live
    result["result_digest"] = digest(result)
    atomic_json(run_dir / "result.json", result)
    return result
