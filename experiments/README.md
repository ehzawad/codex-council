# Optional host-position receipts (issue #22)

**Checkout-only experiment; keep PR #23 open and unmerged for local evaluation.**
Nothing in the installed plugin, skill, permissions, model selection, or normal
invocation path changes. The helper adds no default prompt content.

## Who decides?

Council contributes observations, explanations, proposals, or artifacts for any
task. Claude, as lead host, decides what to do within the user's instructions and
permissions. Other hosts can use the same helper. A collaborator's conclusion is
input to that decision, not an approval or veto.

This helper records the host's **working position**, not a Council verdict:

| `host_position` | Meaning |
|---|---|
| `use_for_now` | The host provisionally relies on this scoped result. All declared criteria need supporting evidence and unchanged captured inputs. |
| `revisit` | The host wants to reconsider. Passing checks do not prevent this. |
| `needs_evidence` | At least one declared check is blocked or unassessed. |
| `set_aside` | Stop relying on this position; retain its history. No new evidence is claimed. |

These labels describe records, not instructions for the host or workflow. Even a
valid `use_for_now` receipt grants no permission to edit, publish, or merge. Host
attribution (`recorded_by`) is self-reported, not authenticated user approval.

One worker can handle many chunks and checks. Several contributions can support
one receipt. There is no worker, role, rule, or receipt per chunk. Use a receipt
only to retain a consequential reason or experiment that the current brief does
not already preserve. Otherwise skip it. Current intent and evidence outrank
history; there is no automatic lookup or context injection.

## Working with Claude's existing loop

Claude or its workflow continues to plan, test, revise, and recover. This helper
starts no worker, retry, rollback, or follow-up and blocks no unrelated work. It
only validates explicit record operations. Reconsideration need not wait for it.

When changing course, the host may record `revisit` even after successful checks,
or `set_aside` after requirements change or old run evidence disappears. A different
question or criteria needs a new declaration. Restoring file bytes does not undo a
recorded change of position; reread current intent before reuse.

Claude Code's [dynamic workflows](https://code.claude.com/docs/en/workflows) own
their scripted execution loops. Its [rewind](https://code.claude.com/docs/en/checkpointing)
is separate: Bash changes, external changes, and many subagent edits are not
reliably restored by session checkpoints. Receipt storage is not automatically
synchronized with rewind. Never assume recovery undoes every side effect.

## Usage

Python 3.12+, macOS or Ubuntu. Prepare a normal Council run first. Keep a private
receipt directory outside the source/run trees. Before the selected runs launch,
write a narrow contract (not the conversation history):

```json
{
  "schema": "codex-council/decision-contract@1",
  "task_id": "parser-check",
  "decision_id": "input-behavior",
  "question": "Does this candidate preserve the required input behavior?",
  "assumptions": ["Only captured fixtures and local behavior are in scope."],
  "criteria": [{"id": "behavior", "kind": "command", "expected_exit": 0,
                "expectation": "The relevant behavioral test succeeds."}]
}
```

`task_id` must match the prepared runs; all referenced runs examine the same
captured subject. Repeat `--run` to bind multiple runs. Use actual absolute paths:

```zsh
python3 experiments/decision_receipts.py declare \
  --store /absolute/private-receipt-dir \
  --contract /absolute/contract.json --run /absolute/prepared-run
```

Launch/follow Council normally. After the run ends, Claude inspects its reply,
`result.json`, and evidence, then explicitly authors a record such as:

```json
{
  "expected_revision": 0,
  "recorded_by": "claude-code-lead",
  "host_position": "use_for_now",
  "rationale": "I provisionally rely on this result for the declared scope only.",
  "outcomes": [{"criterion_id": "behavior", "outcome": "supported",
                "evidence": {"run_id": "ACTUAL_RUN_ID", "role_id": "ACTUAL_ROLE_ID",
                             "attempt_file": "ACTUAL_ATTEMPT_FILENAME.json",
                             "command_index": 0}}]
}
```

The command index is zero-based. Outcomes are `supported`, `contradicted`,
`blocked`, or `unassessed`; the first two require evidence. Account for every
criterion except when setting aside: use `outcomes: []` and explain why. Every
update uses the current revision. `set_aside` may use changed requirements but
must retain the same task/decision ids; it does not revalidate the new question.

```zsh
python3 experiments/decision_receipts.py record \
  --store /absolute/private-receipt-dir \
  --contract /absolute/current-contract.json --decision /absolute/host-position.json
python3 experiments/decision_receipts.py status \
  --store /absolute/private-receipt-dir --contract /absolute/current-contract.json
```

`status` exits 0 for matching recorded bindings, 1 for stale/unknown, and 2 for
invalid inputs or conflict. **Exit 0 is not approval, correctness, or a merge
recommendation.** Read `recorded_host_position` separately. The default summary
is bounded to 2,048 UTF-8 bytes and retains the binding status and advisory-only
label even when details are omitted. No current contract means unknown.

Expected nonzero exits can support intentional failure reproductions. For
non-command reasoning use `kind: "judgment"`, omit `expected_exit`, and reference
only `run_id` and `role_id`. That remains the host's judgment about a contribution,
not empirical proof. Non-Git and context-only tasks use the same protocol.

## Boundaries

Reusable supporting/contradicting evidence currently requires input-preserving,
read-only contributions with external tools and command network disabled. This is
a limit of the experiment, **not a restriction on Council's task or role**. A
writable worker can still implement and test many chunks. Capture its final
candidate for validation only when a reusable final-state record is worthwhile;
this helper cannot prove that an earlier green check examined the final bytes.

Hashes and Codex JSONL do not prove test adequacy, independence, truth, or actual
sandbox enforcement. Scope may omit dependencies, remote state, toolchain changes,
or new intent. Check those separately. Broad scope can cause needless invalidation;
rehashing large inputs/logs costs time. Missing evidence means unknown.

Private JSON, advisory locking, revision checks, and atomic replacement preserve
history but are not a same-user security boundary. Reads never execute recorded
commands. Receipt schema is now `decision-receipt@2`; old `@1` records and the old
`decide` command are intentionally not silently reinterpreted. Inspect old records
with their original checkout; start a new declaration for new work.

## Local evaluation prompt

```text
Review PR #23 against current main. Read issue #22, this README, and the diff.
Do not merge, enable auto-merge, close the issue, publish, or change main.
Preserve native model/effort settings, credentials, and permission boundaries.

Claude is the lead decision-maker within my instructions. Council is an on-demand,
task-shaped collaborator, not a fixed reviewer or authority. One worker may do
many chunks. Receipts record Claude's revisable position, not Council approval.
No automatic receipt lookup, extra reviewer, workflow, or per-turn invocation.

Run the offline tests and pinned lint. Then use disposable fixtures and the
installed signed-in tools to compare fresh-context-only work with explicit
receipt lookup under equivalent current inputs and available evidence.
Check one worker/many checks; a non-coding task; handoff after compaction; changed
requirements; reconsideration after passing checks; setting aside after missing
evidence; rollback without reviving an old position; and no receipt for a simple
task. A native repair loop must remain free to change course.

Record exact versions/SHAs, observed behavior, added calls/context, stale-position
mistakes, and bookkeeping. Fix reproduced defects only on the PR branch and rerun
checks; do not weaken protections or claim fake tests prove real sandbox behavior.
Separate Git conflict status, CI, and functional usefulness. Report ready for my
review, needs changes, or insufficient evidence. I decide whether to merge.
```

```zsh
python3 -B -m unittest discover -s tests -p 'test_decision_receipts.py' -v
python3 -B -m unittest discover -s tests -p 'test_*.py' -v
```

Offline tests use fake protocol data plus a real-runner subprocess fixture, not
paid inference or real sandbox validation. Promote only after local comparisons
show useful handoffs or less repeated work without worse decisions or overhead.
Leaving this experiment unused or removing it is a valid outcome.
