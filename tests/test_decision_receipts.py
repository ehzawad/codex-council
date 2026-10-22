"""Checkout-only receipt helper: real files and processes, no paid inference.

Unit fixtures are synthetic protocol records; the integration case runs real
local checks with fake Codex. Neither proves sandbox enforcement or model quality.
"""
import contextlib
import copy
import dataclasses
import fcntl
import hashlib
import io
import json
import os
import secrets
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments"))
sys.path.insert(0, str(ROOT / "tests"))
import decision_receipts as receipts  # noqa: E402
from council_evidence import AttemptEvidence, write_result  # noqa: E402
from council_workflow import prepare  # noqa: E402
from council_workspace import WorkspaceError, atomic_json, canonical  # noqa: E402
from test_invocation_workflow import RoleFixture  # noqa: E402


def write(path, value):
    atomic_json(path, value)


@dataclasses.dataclass
class Result:
    role: object
    ok: bool = True
    text: str = "The expected behaviors were observed in the captured inputs."
    error: str | None = None


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source"
        self.run = self.root / "run"
        self.store = self.root / "receipt"
        for path in (self.source, self.run, self.store):
            path.mkdir(mode=0o700)
        (self.source / "subject.py").write_text("value = 1\n")
        (self.source / "requirements.txt").write_text("Keep the current value.\n")
        self.spec = {"schema": receipts.CONTRACT, "task_id": "boundary-task",
                     "decision_id": "value-contract", "question": "Does this candidate meet the value contract?",
                     "assumptions": ["No external service is part of this check."],
                     "criteria": [{"id": "positive", "expectation": "The value remains 1.",
                                   "kind": "command", "expected_exit": 0},
                                  {"id": "negative", "expectation": "The contradictory assertion fails.",
                                   "kind": "command", "expected_exit": 1}]}
        self.roles = [RoleFixture(id="batch", execution={"sandbox": "read-only"})]
        self.plan = self.stage(self.run)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for directory, dirs, files in os.walk(self.root):
            for name in dirs:
                path = Path(directory) / name
                if not path.is_symlink():
                    path.chmod(0o700)
            for name in files:
                path = Path(directory) / name
                if not path.is_symlink() and path.is_file():
                    path.chmod(0o600)
        self.temp.cleanup()

    def stage(self, run):
        write(run / "request.json", {"schema": "codex-council/request@1", "task_id": "boundary-task",
                                    "reason": "Check the value contract.", "source_root": str(self.source),
                                    "include": ["."]})
        (run / "context.md").write_text("Check two properties in one contribution.")
        return prepare(str(run), self.roles, (run / "context.md").read_text())

    def declare(self, runs=None):
        return receipts.declare(self.store, self.spec, runs or [self.run])

    def finish(self, run=None, plan=None, codes=(0, 1), reported=None):
        run, plan = run or self.run, plan or self.plan
        spool = AttemptEvidence(run / "evidence/batch", ["fixture"], "two checks")
        for code in codes:
            # A pure protocol consumer; ReceiptRunnerIntegrationTests runs real commands.
            event = {"type": "item.completed", "item": {"type": "command_execution",
                     "command": f"fixture-check-{code}", "exit_code": code,
                     "aggregated_output": ""}}
            spool.feed("stdout", canonical(event) + b"\n")
        spool.feed("stdout", b'{"type":"turn.completed"}\n')
        meta = spool.finish(returncode=0, state="completed", unsafe_to_replay=True)
        if reported:  # a summary that disagrees with its raw events
            for command, code in zip(meta["commands"], reported):
                command["reported_exit_code"] = code
            write(run / "evidence/batch" / f"{spool.key}.json", meta)
        self.supervise(run)
        (run / "execution.claim").touch(mode=0o600)
        write_result(run, plan, [Result(role=self.roles[0])])
        return spool.key + ".json"

    def supervise(self, run):
        """A finished runner's tokened supervisor.lock and the supervisor.json naming it."""
        token = secrets.token_hex(16)
        fd = os.open(run / "supervisor.lock", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, token.encode())
        st = os.fstat(fd)
        os.close(fd)
        write(run / "supervisor.json", {"pid": os.getpid(), "lock": {
            "dev": st.st_dev, "ino": st.st_ino, "token": token}})

    def decision(self, attempt, host_position="use_for_now", run=None, plan=None):
        plan = plan or self.plan
        return {"expected_revision": 0, "recorded_by": "test-host", "host_position": host_position,
                "rationale": "Both declared predicates match the observed exits.",
                "outcomes": [{"criterion_id": name, "outcome": "supported",
                              "evidence": {"run_id": plan["run_id"], "role_id": "batch",
                                           "attempt_file": attempt, "command_index": index}}
                             for index, name in enumerate(("positive", "negative"))]}

    def use_for_now(self):
        self.declare()
        decision = self.decision(self.finish())
        receipts.record(self.store, decision, self.spec)
        return decision

    def one_check(self, **finish):
        """Declare only the positive criterion; return its decision."""
        self.spec["criteria"] = self.spec["criteria"][:1]
        self.declare()
        decision = self.decision(self.finish(**finish))
        decision["outcomes"] = decision["outcomes"][:1]
        return decision

    def cli_args(self, command, decision=None):
        spec = self.root / "contract.json"
        write(spec, self.spec)
        argv = [command, "--store", self.store, "--contract", spec]
        if decision is not None:
            write(self.root / "decision.json", decision)
            argv += ["--decision", self.root / "decision.json"]
        return argv

    def main(self, *argv):
        """In-process main(): (exit code, stdout bytes, stderr text)."""
        out, err = io.BytesIO(), io.StringIO()
        with patch.object(sys, "stdout", SimpleNamespace(buffer=out)), contextlib.redirect_stderr(err):
            code = receipts.main([str(item) for item in argv])
        return code, out.getvalue(), err.getvalue()

    @contextlib.contextmanager
    def racing(self, name, forged, at=1):
        """A concurrent writer swaps `name` in just before the helper's at-th open of it."""
        real, opens, swapped = receipts.regular, [], {}

        def regular(root, relative):
            if PurePosixPath(relative).name == name:
                opens.append(relative)
                if len(opens) == at:
                    path = Path(root) / relative
                    swapped[path] = path.read_bytes()
                    temp = path.with_name(name + ".race")
                    temp.write_bytes(forged)
                    temp.chmod(0o600)
                    os.replace(temp, path)
            return real(root, relative)

        try:
            with patch.object(receipts, "regular", regular):
                yield
        finally:
            for path, data in swapped.items():
                path.write_bytes(data)

    @contextlib.contextmanager
    def temporary_areas(self, tmpdir=None, gettempdir=None, fixed=()):
        """Only these spellings count as temporary areas; an exception means no usable temp dir."""
        found = gettempdir if isinstance(gettempdir, Exception) else (
            lambda: str(gettempdir or self.root / "elsewhere"))
        with (patch.dict(os.environ), patch.object(receipts, "TEMP_AREAS", tuple(map(str, fixed))),
              patch.object(receipts.tempfile, "gettempdir", side_effect=found)):
            os.environ.pop("TMPDIR", None)
            if tmpdir is not None:
                os.environ["TMPDIR"] = str(tmpdir)
            yield

    def test_declare_warns_about_temporary_run_and_store_without_refusing(self):
        argv = self.cli_args("declare") + ["--run", self.run]  # fixtures live under gettempdir()
        code, out, err = self.main(*argv)
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["state"], "declared")
        self.assertEqual([line.split(" is in a temporary area")[0] for line in result["warnings"]],
                         [f"receipt store {self.store}", f"run {self.plan['run_id']} {self.run}"])
        self.assertTrue(all("reboot" in line for line in result["warnings"]))
        self.assertEqual(list(receipts.read(self.store)["runs"]), [self.plan["run_id"]])

    def test_declare_warnings_use_resolved_temporary_areas(self):
        alias = self.root / "run-alias"
        alias.symlink_to(self.run, target_is_directory=True)
        run = f"run {self.plan['run_id']} {self.run}"
        cases = {"none": ({}, False, False),
                 "tmpdir-symlink-spelling": ({"tmpdir": alias}, False, True),
                 "gettempdir": ({"gettempdir": self.root / "receipt-gettempdir"}, True, False),
                 "fixed-area": ({"fixed": [self.root]}, True, True),
                 "filesystem-root-ignored": ({"tmpdir": "/", "fixed": ["/"]}, False, False),
                 "no-usable-tempdir": ({"gettempdir": FileNotFoundError("none")}, False, False)}
        for label, (areas, store_warns, run_warns) in cases.items():
            with self.subTest(label):
                self.store = self.root / f"receipt-{label}"
                self.store.mkdir(mode=0o700)
                with self.temporary_areas(**areas):
                    result = self.declare()
                expected = [f"receipt store {self.store}"] * store_warns + [run] * run_warns
                self.assertEqual([line.split(" is in a temporary area")[0]
                                  for line in result.get("warnings", [])], expected)
                self.assertEqual("warnings" in result, bool(expected))
                self.assertEqual(result["state"], "declared")

    def test_one_worker_supports_multiple_chunks_and_expected_failure(self):
        self.use_for_now()
        summary = receipts.status(self.store, self.spec)
        self.assertEqual(summary["recorded_host_position"], "use_for_now")
        self.assertEqual(summary["binding_status"], "match")
        self.assertEqual(len(summary["criterion_outcomes"]), 2)
        self.assertLessEqual(len(canonical(summary)) + 1, 2048)

    def test_nongit_data_question_uses_same_protocol(self):
        (self.source / "measurements.csv").write_text("x,y\n1,1\n2,2\n")
        self.run = self.root / "data-run"
        self.run.mkdir(mode=0o700)
        self.plan = self.stage(self.run)
        self.spec["question"] = "Does the preserved table support the comparison?"
        self.spec["criteria"] = [{"id": "comparison", "kind": "judgment",
                                  "expectation": "The two columns agree in this captured table."}]
        self.declare()
        self.finish()
        decision = {"expected_revision": 0, "recorded_by": "host", "host_position": "use_for_now",
                    "rationale": "Reviewed the comparison; no extrapolation beyond these rows.",
                    "outcomes": [{"criterion_id": "comparison", "outcome": "supported",
                                  "evidence": {"run_id": self.plan["run_id"], "role_id": "batch"}}]}
        receipts.record(self.store, decision, self.spec)
        state = receipts.read(self.store)
        self.assertEqual(state["history"][0]["evidence"][0]["provenance"],
                         "host-judgment-of-collaborator-text")
        self.assertFalse((self.source / ".git").exists())

    def test_multiple_runs_one_decision(self):
        other = self.root / "other"
        other.mkdir(mode=0o700)
        second = self.stage(other)
        self.declare([self.run, other])
        a, b = self.finish(), self.finish(other, second)
        decision = self.decision(a)
        decision["outcomes"][1]["evidence"].update(run_id=second["run_id"], attempt_file=b)
        receipts.record(self.store, decision, self.spec)
        self.assertEqual(receipts.status(self.store, self.spec)["binding_status"], "match")

    def test_no_current_contract_is_unknown_and_stale_inputs_outrank_it(self):
        self.use_for_now()
        self.assertEqual(receipts.status(self.store)["binding_status"], "unknown")
        (self.source / "subject.py").write_text("value = 2\n")
        summary = receipts.status(self.store)
        self.assertEqual(summary["binding_status"], "stale")
        self.assertLessEqual({"current-contract-not-supplied", "captured-inputs-stale"}, set(summary["gaps"]))

    def test_status_exits_0_only_for_matching_use_for_now(self):
        self.declare()
        attempt = self.finish()
        argv = self.cli_args("status")
        code, out, _ = self.main(*argv)
        summary = json.loads(out)
        self.assertEqual((code, summary["binding_status"], summary["gaps"]), (1, "match", ["no-recorded-position"]))
        partial = self.decision(attempt, "needs_evidence")
        partial["outcomes"][1] = {"criterion_id": "negative", "outcome": "unassessed"}
        receipts.record(self.store, partial, self.spec)
        code, out, _ = self.main(*argv)
        self.assertEqual((code, json.loads(out)["gaps"]), (1, ["criteria-without-evidence"]))
        for revision, position in enumerate(("revisit", "use_for_now", "set_aside"), 1):
            decision = self.decision(attempt, position)
            decision["expected_revision"] = revision
            if position == "set_aside":
                decision["outcomes"] = []
            receipts.record(self.store, decision, self.spec)
            code, out, _ = self.main(*argv)
            summary = json.loads(out)
            self.assertEqual((code, summary["binding_status"], summary["recorded_host_position"]),
                             (0 if position == "use_for_now" else 1, "match", position))

    def test_current_requirements_override_old_position(self):
        self.use_for_now()
        changed = copy.deepcopy(self.spec)
        changed["criteria"][0]["expected_exit"] = 1
        self.assertEqual(receipts.status(self.store, changed)["binding_status"], "stale")
        self.assertEqual(receipts.status(self.store, changed)["recorded_host_position"], "use_for_now")

    def test_source_and_fixture_edits_invalidate_receipt(self):
        self.use_for_now()
        for name in ("subject.py", "requirements.txt"):
            path = self.source / name
            old = path.read_bytes()
            path.write_text("changed\n")
            self.assertEqual(receipts.status(self.store, self.spec)["binding_status"], "stale")
            path.write_bytes(old)

    def test_old_revisit_position_is_not_a_permanent_veto(self):
        self.declare()
        decision = self.decision(self.finish(), "revisit")
        decision["outcomes"][0]["outcome"] = "contradicted"
        decision["outcomes"][0]["evidence"]["command_index"] = 1
        receipts.record(self.store, decision, self.spec)
        (self.source / "subject.py").write_text("value = 2\n")
        summary = receipts.status(self.store, self.spec)
        self.assertEqual((summary["recorded_host_position"], summary["binding_status"]), ("revisit", "stale"))

    def test_refuses_stale_candidate_at_recording(self):
        self.declare()
        decision = self.decision(self.finish())
        (self.source / "subject.py").write_text("value = 2\n")
        with self.assertRaisesRegex(WorkspaceError, "stale or unknown"):
            receipts.record(self.store, decision, self.spec)

    def test_candidate_changed_after_checks_cannot_be_used_for_now(self):
        self.declare()
        decision = self.decision(self.finish())
        (self.run / "workspaces/batch/subject.py").write_text("value = 2\n")
        with self.assertRaisesRegex(WorkspaceError, "captured input changed"):
            receipts.record(self.store, decision, self.spec)

    def test_writable_experiment_is_not_final_candidate_proof(self):
        self.roles = [RoleFixture(id="batch", execution={"sandbox": "workspace-write"})]
        self.run = self.root / "writable"
        self.run.mkdir(mode=0o700)
        self.plan = self.stage(self.run)
        self.declare()
        with self.assertRaisesRegex(WorkspaceError, "captured final-state"):
            receipts.record(self.store, self.decision(self.finish()), self.spec)

    def test_resumed_contribution_is_not_reusable_evidence(self):
        self.roles = [RoleFixture(id="batch", execution={"sandbox": "read-only", "continuity": "resume"})]
        self.run = self.root / "resumed"
        self.run.mkdir(mode=0o700)
        self.plan = self.stage(self.run)
        self.declare()
        with self.assertRaisesRegex(WorkspaceError, "fresh"):
            receipts.record(self.store, self.decision(self.finish()), self.spec)

    def test_no_posthoc_criteria_declaration(self):
        self.finish()
        with self.assertRaisesRegex(WorkspaceError, "before launch"):
            self.declare()

    def test_declaration_is_one_use(self):
        self.declare()
        with self.assertRaisesRegex(WorkspaceError, "already declared"):
            self.declare()

    def test_changed_criteria_require_new_declaration(self):
        self.declare()
        decision = self.decision(self.finish())
        changed = copy.deepcopy(self.spec)
        changed["question"] = "New question"
        with self.assertRaisesRegex(WorkspaceError, "contract changed"):
            receipts.record(self.store, decision, changed)

    def test_blocked_check_cannot_be_used_for_now(self):
        self.declare()
        decision = self.decision("missing")
        decision["outcomes"] = [{"criterion_id": key, "outcome": "blocked"} for key in ("positive", "negative")]
        with self.assertRaisesRegex(WorkspaceError, "every criterion supported"):
            receipts.record(self.store, decision, self.spec)
        decision["host_position"] = "needs_evidence"
        receipts.record(self.store, decision, self.spec)

    def test_wrong_expected_exit_does_not_pass(self):
        self.declare()
        decision = self.decision(self.finish())
        decision["outcomes"][0]["evidence"]["command_index"] = 1
        with self.assertRaisesRegex(WorkspaceError, "contradicts"):
            receipts.record(self.store, decision, self.spec)

    def test_unknown_or_missing_criterion_does_not_pass(self):
        self.declare()
        decision = self.decision(self.finish())
        decision["outcomes"].pop()
        with self.assertRaisesRegex(WorkspaceError, "every declared"):
            receipts.record(self.store, decision, self.spec)

    def test_unknown_schema_or_position_is_refused(self):
        self.declare()
        decision = self.decision(self.finish(), "approved")
        with self.assertRaisesRegex(WorkspaceError, "invalid host_position"):
            receipts.record(self.store, decision, self.spec)
        state = receipts.read(self.store)
        state["schema"] = "unknown"
        receipts.save(self.store, state)
        with self.assertRaisesRegex(WorkspaceError, "schema mismatch"):
            receipts.status(self.store, self.spec)

    def test_stale_revision_cannot_overwrite_and_setting_aside_preserves_history(self):
        decision = self.use_for_now()
        with self.assertRaisesRegex(WorkspaceError, "revision conflict"):
            receipts.record(self.store, decision, self.spec)
        withdraw = {"expected_revision": 1, "recorded_by": "host", "host_position": "set_aside",
                    "rationale": "The previous conclusion used an incomplete requirement.", "outcomes": []}
        receipts.record(self.store, withdraw, self.spec)
        state = receipts.read(self.store)
        self.assertEqual([item["host_position"] for item in state["history"]], ["use_for_now", "set_aside"])

    def test_failed_atomic_write_keeps_previous_record(self):
        self.use_for_now()
        before = (self.store / "receipt.json").read_bytes()
        withdraw = {"expected_revision": 1, "recorded_by": "host", "host_position": "set_aside",
                    "rationale": "Set aside for review", "outcomes": []}
        with patch("council_workspace.os.replace", side_effect=OSError("fixture disk failure")):
            with self.assertRaises(OSError):
                receipts.record(self.store, withdraw, self.spec)
        self.assertEqual((self.store / "receipt.json").read_bytes(), before)

    def test_accepted_save_stays_readable(self):
        self.declare()
        state = receipts.read(self.store)
        size = len(canonical(state))
        with patch.object(receipts, "MAX_STATE_BYTES", size):
            with self.assertRaisesRegex(WorkspaceError, "history full"):
                receipts.save(self.store, state)
        with patch.object(receipts, "MAX_STATE_BYTES", size + 1):
            receipts.save(self.store, state)
            self.assertEqual(receipts.read(self.store), state)

    def test_supervisor_lock_must_be_the_recorded_free_lock(self):
        self.declare()
        decision = self.decision(self.finish())
        lock, record = self.run / "supervisor.lock", self.run / "supervisor.json"
        token, saved = lock.read_bytes(), record.read_bytes()

        def refused():
            with self.assertRaisesRegex(WorkspaceError, "supervisor"):
                receipts.record(self.store, decision, self.spec)

        with open(lock, "rb") as stream:  # still running
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            refused()
        lock.write_bytes(b"0" * 32)  # same file, another token
        refused()
        lock.write_bytes(token)
        record.unlink()  # no supervisor record
        refused()
        record.write_bytes(saved)
        record.chmod(0o600)
        temp = self.run / "lock.new"  # same token, another file (a new inode)
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, token)
        os.close(fd)
        os.replace(temp, lock)
        refused()

    def test_receipt_lock_conflict_is_bounded(self):
        self.declare()
        with receipts.locked(self.store):
            with self.assertRaises(BlockingIOError):
                receipts.status(self.store, self.spec)

    def test_changed_or_missing_evidence_is_unknown(self):
        self.use_for_now()
        log = next((self.run / "evidence/batch").glob("*.stdout.jsonl"))
        log.write_text("changed")
        self.assertEqual(receipts.status(self.store, self.spec)["binding_status"], "unknown")
        log.unlink()
        self.assertEqual(receipts.status(self.store, self.spec)["binding_status"], "unknown")

    def test_raw_evidence_change_blocks_recording(self):
        self.declare()
        decision = self.decision(self.finish())
        next((self.run / "evidence/batch").glob("*.stdout.jsonl")).write_text("forged")
        with self.assertRaisesRegex(WorkspaceError, "checksum mismatch"):
            receipts.record(self.store, decision, self.spec)

    def test_recorded_hashes_are_of_the_validated_bytes(self):
        decision = self.one_check()
        attempt = decision["outcomes"][0]["evidence"]["attempt_file"]
        for name, message in (("result.json", "invalid result"), (attempt, "not in recorded attempt")):
            with self.subTest(name), self.racing(name, b"{}\n"):
                with self.assertRaisesRegex(WorkspaceError, message):
                    receipts.record(self.store, decision, self.spec)

    def test_raw_event_is_scanned_from_the_hashed_bytes(self):
        decision = self.one_check(codes=(1, 1), reported=(0, 1))  # raw exit 1; summary claims 0
        log = next((self.run / "evidence/batch").glob("*.stdout.jsonl"))
        forged = log.read_bytes().replace(b'"exit_code":1', b'"exit_code":0', 1)
        with self.racing(log.name, forged, at=2):
            with self.assertRaisesRegex(WorkspaceError, "differs from raw event"):
                receipts.record(self.store, decision, self.spec)

    def test_malformed_evidence_or_history_exits_2(self):
        self.declare()
        attempt = self.finish()
        evidence = self.run / "evidence/batch"
        log = next(evidence.glob("*.stdout.jsonl"))
        paths = (self.run / "result.json", evidence / attempt, log)
        saved = [path.read_bytes() for path in paths]
        argv = self.cli_args("record", self.decision(attempt))

        def raw_item(result, mirror):
            lines = saved[2].split(b"\n")
            lines[0] = b'{"type":"item.completed","item":"x"}'
            log.write_bytes(b"\n".join(lines))
            mirror["sha256"]["stdout"] = hashlib.sha256(b"\n".join(lines)).hexdigest()

        cases = {"roles-array": lambda result, mirror: result.update(roles=[]),
                 "null-attempt": lambda result, mirror: result["roles"]["batch"]["attempts"].insert(0, None),
                 "non-object-command": lambda result, mirror: mirror["commands"].__setitem__(0, "x"),
                 "non-object-raw-item": raw_item}
        for label, change in cases.items():
            with self.subTest(label):
                for path, data in zip(paths, saved):
                    path.write_bytes(data)
                result = json.loads(saved[0])
                mirror = result["roles"]["batch"]["attempts"][0]  # written back as the attempt file
                change(result, mirror)
                write(paths[1], mirror)
                result["result_digest"] = receipts.digest(
                    {key: value for key, value in result.items() if key != "result_digest"})
                write(paths[0], result)
                code, _, err = self.main(*argv)
                self.assertEqual(code, 2, err)
        for path, data in zip(paths, saved):
            path.write_bytes(data)
        self.assertEqual(self.main(*argv)[0], 0)
        state = receipts.read(self.store)
        state["history"][0]["evidence"][0]["files"] = []
        receipts.save(self.store, state)
        self.assertEqual(self.main(*self.cli_args("status"))[0], 2)

    def test_changed_plan_or_context_is_unknown(self):
        self.use_for_now()
        (self.run / "context.md").write_text("other context")
        self.assertEqual(receipts.status(self.store, self.spec)["binding_status"], "unknown")

    def test_summary_bounds_never_hide_staleness(self):
        decision = self.use_for_now()
        decision.update(expected_revision=1, rationale="Long scoped explanation. " * 60)
        receipts.record(self.store, decision, self.spec)
        changed = copy.deepcopy(self.spec)
        changed["question"] = "changed"
        summary = receipts.status(self.store, changed, max_bytes=512)
        self.assertEqual(summary["binding_status"], "stale")
        self.assertTrue(summary["details_omitted"])
        self.assertLessEqual(len(canonical(summary)) + 1, 512)

    def test_status_is_read_only(self):
        self.use_for_now()
        before = {p.name: p.read_bytes() for p in self.store.iterdir() if p.is_file()}
        receipts.status(self.store, self.spec)
        self.assertEqual({p.name: p.read_bytes() for p in self.store.iterdir() if p.is_file()}, before)

    def test_store_symlink_and_public_directory_refused(self):
        link = self.root / "link"
        link.symlink_to(self.store, target_is_directory=True)
        with self.assertRaises(WorkspaceError):
            receipts.declare(link, self.spec, [self.run])
        self.store.chmod(0o755)
        with self.assertRaises(WorkspaceError):
            self.declare()

    def test_symlinked_evidence_never_followed(self):
        self.declare()
        decision = self.decision(self.finish())
        log = next((self.run / "evidence/batch").glob("*.stdout.jsonl"))
        log.unlink()
        log.symlink_to(self.source / "subject.py")
        with self.assertRaises((OSError, WorkspaceError)):
            receipts.record(self.store, decision, self.spec)

    def test_no_receipts_in_plugin_and_no_production_import(self):
        for path in (ROOT / "plugins").rglob("*.py"):
            self.assertNotIn("decision_receipts", path.read_text())
        skill = ROOT / "plugins/codex-council/skills/codex-council/SKILL.md"
        self.assertNotIn("decision_receipts", skill.read_text())

    def test_unknown_fields_cannot_grant_permissions(self):
        changed = copy.deepcopy(self.spec)
        changed["sandbox"] = "bypass"
        with self.assertRaises(WorkspaceError):
            receipts.contract(changed)

    def test_duplicate_criteria_refused(self):
        changed = copy.deepcopy(self.spec)
        changed["criteria"].append(changed["criteria"][0])
        with self.assertRaises(WorkspaceError):
            receipts.contract(changed)

    def test_strict_json_refuses_duplicate_keys_and_non_finite_numbers(self):
        path = self.root / "spec.json"
        path.write_text('{"schema":"a","schema":"b"}')
        with self.assertRaises(WorkspaceError):
            receipts.read_json(path, private=False)
        for data in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1e999}'):
            with self.subTest(data), self.assertRaises(WorkspaceError):
                receipts.strict(data)

    def test_cli_exit_codes_and_no_automatic_inference(self):
        spec = self.root / "contract.json"
        write(spec, self.spec)
        command = [sys.executable, "-P", str(ROOT / "experiments/decision_receipts.py")]
        declared = subprocess.run(command + ["declare", "--store", str(self.store), "--contract", str(spec),
                                            "--run", str(self.run)], capture_output=True, timeout=20)
        self.assertEqual(declared.returncode, 0, declared.stderr)
        unknown = subprocess.run(command + ["status", "--store", str(self.store)], capture_output=True, timeout=20)
        self.assertEqual(unknown.returncode, 1, unknown.stderr)
        self.assertFalse((self.run / "execution.claim").exists())

    def test_two_processes_cannot_overwrite_same_revision(self):
        self.use_for_now()
        spec = self.root / "contract.json"
        instruction = self.root / "decision.json"
        write(spec, self.spec)
        write(instruction, {"expected_revision": 1, "recorded_by": "host", "host_position": "set_aside",
                            "rationale": "Set aside pending inspection.", "outcomes": []})
        argv = [sys.executable, "-B", str(ROOT / "experiments/decision_receipts.py"),
                "record", "--store", str(self.store), "--contract", str(spec),
                "--decision", str(instruction)]
        processes = [subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
        try:
            for process in processes:
                process.communicate(timeout=30)
            self.assertEqual(sorted(process.returncode for process in processes), [0, 2])
            self.assertEqual(len(receipts.read(self.store)["history"]), 2)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_late_scratch_change_does_not_keep_matching_bindings(self):
        self.use_for_now()
        (self.run / "workspaces/batch/subject.py").write_text("value = 99\n")
        self.assertEqual(receipts.status(self.store, self.spec)["binding_status"], "unknown")

    def test_source_loss_is_unknown_not_current(self):
        self.use_for_now()
        self.source.rename(self.root / "gone")
        self.assertEqual(receipts.status(self.store, self.spec)["binding_status"], "unknown")

    def test_replaced_lock_with_link_is_refused(self):
        self.declare()
        lock = self.store / "receipt.lock"
        lock.unlink()
        lock.symlink_to(self.source / "subject.py")
        with self.assertRaises(OSError):
            receipts.status(self.store, self.spec)

    def test_a_completed_contribution_does_not_choose_for_the_host(self):
        self.declare()
        self.finish()
        summary = receipts.status(self.store, self.spec)
        self.assertEqual(summary["recorded_host_position"], "not-recorded")
        self.assertEqual(summary["decision_owner"], "host")
        self.assertEqual(receipts.read(self.store)["history"], [])

    def test_host_can_revisit_even_after_all_checks_pass(self):
        decision = self.use_for_now()
        decision.update(expected_revision=1, host_position="revisit",
                        rationale="The checks pass, but another approach may fit better.")
        receipts.record(self.store, decision, self.spec)
        self.assertEqual(receipts.status(self.store, self.spec)["recorded_host_position"], "revisit")
        self.assertEqual([row["host_position"] for row in receipts.read(self.store)["history"]],
                         ["use_for_now", "revisit"])

    def test_set_aside_survives_changed_requirements_and_missing_runs(self):
        self.use_for_now()
        changed = copy.deepcopy(self.spec)
        changed["question"] = "The goal has changed; inspect a different property."
        self.run.rename(self.root / "removed-run")
        receipts.record(self.store, {
            "expected_revision": 1, "recorded_by": "host", "host_position": "set_aside",
            "rationale": "Course correction; earlier evidence is not being reused.", "outcomes": []}, changed)
        summary = receipts.status(self.store, changed)
        self.assertEqual(summary["recorded_host_position"], "set_aside")
        self.assertEqual(summary["binding_status"], "stale")
        self.assertEqual(len(receipts.read(self.store)["history"]), 2)

    def test_set_aside_cannot_address_another_decision(self):
        self.use_for_now()
        changed = copy.deepcopy(self.spec)
        changed["decision_id"] = "another-decision"
        with self.assertRaisesRegex(WorkspaceError, "identity changed"):
            receipts.record(self.store, {
                "expected_revision": 1, "recorded_by": "host", "host_position": "set_aside",
                "rationale": "Wrong target", "outcomes": []}, changed)

    def test_advisory_authority_survives_the_smallest_summary(self):
        self.use_for_now()
        summary = receipts.status(self.store, self.spec, max_bytes=512)
        self.assertEqual(summary["authority"], "advisory-only")
        self.assertEqual(summary["decision_owner"], "host")
        self.assertLessEqual(len(canonical(summary)) + 1, 512)
        self.assertNotIn("next_action", summary)

    def test_restored_bytes_do_not_restore_a_set_aside_host_position(self):
        self.use_for_now()
        subject = self.source / "subject.py"
        original = subject.read_bytes()
        subject.write_text("value = 99\n")
        receipts.record(self.store, {
            "expected_revision": 1, "recorded_by": "host", "host_position": "set_aside",
            "rationale": "Do not reuse that position after this course correction.", "outcomes": []}, self.spec)
        subject.write_bytes(original)
        summary = receipts.status(self.store, self.spec)
        self.assertEqual(summary["binding_status"], "match")
        self.assertEqual(summary["recorded_host_position"], "set_aside")

    def test_forged_result_digest_is_refused(self):
        self.declare()
        decision = self.decision(self.finish())
        path = self.run / "result.json"
        data = json.loads(path.read_text())
        data["roles"]["batch"]["interpretation"]["text"] = "different"
        write(path, data)
        with self.assertRaisesRegex(WorkspaceError, "result checksum"):
            receipts.record(self.store, decision, self.spec)


class ReceiptRunnerIntegrationTests(unittest.TestCase):
    def test_real_runner_one_worker_multiple_checks(self):
        # Compose the real-runner fixture without importing its TestCase into
        # this module's discovery; the default "runTest" names no test method.
        import test_workflow_cli as workflow_tests
        case = workflow_tests.WorkflowCliTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        executable = case.bin / "codex"
        anchor = '    if "HANG_FIXTURE" in body:'
        probe = ("    if \"RECEIPT_PROBE\" in body:\n"
                 "        command(\"from pathlib import Path; scope={}; exec(Path('subject.py').read_text(),scope); assert scope['value']==1\", cwd)\n"
                 "        command(\"from pathlib import Path; scope={}; exec(Path('subject.py').read_text(),scope); assert scope['value']!=1\", cwd)\n")
        fake = executable.read_text()
        # Isolated child Python (no site hooks or bytecode); fail loudly if the fake drifts.
        for old, new in (('argv = [sys.executable, "-c", code]', 'argv = [sys.executable, "-I", "-S", "-B", "-c", code]'),
                         (anchor, probe + anchor)):
            self.assertIn(old, fake, "tests/fake_workflow_codex.py changed; update this probe")
            fake = fake.replace(old, new)
        executable.write_text(fake)
        run = case.stage("RECEIPT_PROBE", execution={"sandbox": "read-only"})
        plan = json.loads((run / "run.json").read_text())
        store = case.base / "receipt"
        store.mkdir(mode=0o700)
        spec = {"schema": receipts.CONTRACT, "task_id": "task-one", "decision_id": "two-properties",
                "question": "Do both properties hold?", "assumptions": [], "criteria": [
                    {"id": "a", "kind": "command", "expected_exit": 0, "expectation": "Value is 1."},
                    {"id": "b", "kind": "command", "expected_exit": 1, "expectation": "Contrary assertion fails."}]}
        receipts.declare(store, spec, [run])
        launched = case.launch(run)
        self.assertEqual(launched.returncode, 0, launched.stdout + launched.stderr)
        self.assertEqual(len(case.execs()), 1)
        attempt = next((run / "evidence/contribution").glob("attempt-*.json")).name
        decision = {"expected_revision": 0, "recorded_by": "fixture-host", "host_position": "use_for_now",
                    "rationale": "Two checks in the same worker; no separate role per chunk.",
                    "outcomes": [{"criterion_id": name, "outcome": "supported", "evidence": {
                        "run_id": plan["run_id"], "role_id": "contribution", "attempt_file": attempt,
                        "command_index": i}} for i, name in enumerate(("a", "b"))]}
        receipts.record(store, decision, spec)
        self.assertEqual(receipts.status(store, spec)["binding_status"], "match")
        self.assertEqual((case.source / "subject.py").read_text(), "value = 1\n")
        self.assertEqual(len(case.execs()), 1, "recording and recall must not start another worker")


if __name__ == "__main__":
    unittest.main()
