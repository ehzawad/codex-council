# Decision receipts: checkout-only experiment for issue #22

This is **not an installed plugin feature**. The stable runner, skill, permissions,
model selection, run formats and invocation timing are unchanged. Nothing imports
this helper from the plugin; nothing injects its output into Claude's context.
Use it explicitly from this checkout while evaluating the idea. Do not merge or
promote it merely because deterministic tests pass.

## The corrected mental model

A **chunk** is some work. A **worker/role** is who does it. A **checkpoint** is a
useful decision boundary. A **receipt** records the evidence and reasoning behind
that decision. These are not one-to-one. One Codex worker can implement several
chunks, run several checks, and return one contribution. One receipt can cover
several criteria and contributions from multiple runs; several independent
receipts can refer to the same run. No new worker is created by this helper.

Do not require a new role, rule, process, review, or receipt for every chunk. Reuse
criteria when the requirements are the same. Chunk size, role count, model/thread
continuity and when to invoke Council remain host decisions. The design borrows
explicit evidence boundaries from [Helix](https://shopify.engineering/helix), not
its ordered gate sequence or platform-specific reviews.

Fresh context remains authoritative. Historical receipts are useful only when the
current inputs do not explain a consequential prior decision: for example, why
an alternative was rejected, what an earlier experiment actually ruled out, or
which assumptions supported a decision before compaction. Current code alone may
not preserve those reasons. If current artifacts and the fresh brief already
contain the needed information, **skip receipts entirely**. A past rejection is
not a permanent veto, and acceptance is not a general permission grant.

## Small first slice, not a checkpoint scheduler

`decision_receipts.py` reuses the existing capture/integrity helpers and reads
existing prepared runs and their evidence. It never dispatches Codex, executes
commands found in evidence, modifies a run, applies artifacts, changes model
settings, or schedules a follow-up. Git metadata reads can occur during freshness
checks, using the same helper as the runner.

There are three explicit operations:

- `declare`: freeze task-specific criteria against one or more **prepared but not
  launched** runs with the same captured subject. No recapture or second source
  copy is needed. Use an existing private directory outside source/run trees.
- `decide`: record the host's accepted/rejected/blocked/withdrawn decision with an
  expected revision. Command evidence must match a recorded command event and its
  declared expected exit; a judgment remains a judgment, not empirical proof.
- `status`: inspect only the selected receipt against an explicitly supplied
  current decision contract and currently readable inputs/evidence. Default output
  is at most **2048 UTF-8 bytes**, not tokens. Omissions are reported, and stale or
  unknown binding status is never silently removed to fit the budget.

No automatic history search, parent-dependency graph, approval authority,
transitions that control other work, or global memory. A single private JSON file
preserves revisions under a stable advisory lock and atomic replacement. A
compare-and-set revision prevents lost updates; a busy lock returns a conflict,
not an indefinite wait. There is a 1 MiB history bound for this small experiment.
No success is claimed if persistence fails. Do not remove a live lock file.

The **current decision contract** is not the entire conversation. It is the narrow
question, requirements and assumptions whose answer the host is considering reusing.
Other turns can change without invalidating it. The host must update this contract
when the question or assumptions actually change; the tool cannot read the user's
mind. Referenced requirements, fixtures and dependency lock files should also be
in the prepared run's captured scope.

## Example: one worker, two checks, one decision

Prepare a normal Council run using the existing skill. The final candidate should
already be its captured input. A read-only contribution can check several
properties in one turn. Before `--start`, write a contract such as:

```json
{
  "schema": "codex-council/decision-contract@1",
  "task_id": "parser-check",
  "decision_id": "parser-boundary",
  "question": "Does this candidate preserve valid input and reject invalid input?",
  "assumptions": ["Only the captured fixtures and local behavior are in scope."],
  "criteria": [
    {"id": "valid", "kind": "command", "expected_exit": 0,
     "expectation": "The valid-input test command succeeds."},
    {"id": "invalid", "kind": "command", "expected_exit": 1,
     "expectation": "The deliberately invalid-input probe fails as expected."}
  ]
}
```

`task_id` must equal the prepared run's task id. Choose expectations from the
actual checks; `expected_exit: 1` above is an example, not a universal failure
rule. An investigation can succeed by disproving a hypothesis.

From the repository checkout, substitute the actual absolute paths:

```zsh
python3 experiments/decision_receipts.py declare \
  --store /absolute/private-receipt-dir \
  --contract /absolute/contract.json --run /absolute/prepared-run
```

Now launch/follow Council normally. After it has ended, read `result.json`, the
reply, and relevant attempt logs. Use the actual `run_id`, role id, attempt
metadata filename, and **zero-based command index** to author the decision:

```json
{
  "expected_revision": 0,
  "actor": "local-host",
  "disposition": "accepted",
  "rationale": "Both declared behaviors were observed; no broader claim is made.",
  "outcomes": [
    {"criterion_id": "valid", "outcome": "supported",
     "evidence": {"run_id": "ACTUAL_RUN_ID", "role_id": "ACTUAL_ROLE_ID",
                  "attempt_file": "ACTUAL_ATTEMPT_FILENAME.json", "command_index": 0}},
    {"criterion_id": "invalid", "outcome": "supported",
     "evidence": {"run_id": "ACTUAL_RUN_ID", "role_id": "ACTUAL_ROLE_ID",
                  "attempt_file": "ACTUAL_ATTEMPT_FILENAME.json", "command_index": 1}}
  ]
}
```

```zsh
python3 experiments/decision_receipts.py decide \
  --store /absolute/private-receipt-dir \
  --contract /absolute/current-contract.json --decision /absolute/decision.json
python3 experiments/decision_receipts.py status \
  --store /absolute/private-receipt-dir --contract /absolute/current-contract.json
```

For non-Git work, use the runner's explicit input selection. A criterion with
`kind: "judgment"` has no `expected_exit`; its evidence contains just `run_id` and
`role_id`. The host records its rationale after assessing that contribution.
Such a receipt is explicitly labeled `host-judgment-of-collaborator-text`.

Every criterion needs an outcome: supported, contradicted, blocked, or unassessed.
Supported/contradicted need evidence. Accepted requires all supported; rejected
requires at least one contradicted; blocked requires at least one gap. Withdrawn
has no new outcomes and a rationale, retaining the previous history. Any revision
uses the current `expected_revision`; a conflict requires rereading, not blind
retry. Changed criteria need a new declaration, not rewriting the old record.

`status` returns 0 only for unchanged captured bindings, 1 for stale/unknown, and
2 for invalid input or a conflict. **Exit 0 does not mean accepted or correct.**
Inspect `recorded_disposition` as well. Without a current contract the binding is
unknown. Recall does not change historical decisions. A rejected candidate that
later changes is stale history, not evidence against its new version.

## What can go wrong, and what this prototype does not promise

**Wrong scope or circular reasoning.** A convenient test can pass while missing
the actual requirement. The same model can invent a weak criterion and accept its
own answer. A matching exit code does not establish test adequacy, independence,
or task success. The host still checks relevance and may commission independent
work when useful. There is no automatic second reviewer.

**Version confusion.** Evidence for input A cannot certify candidate B after a
writer changes it. This first version accepts evidentiary outcomes only from
input-preserving read-only contributions with external tools denied, checking
both baseline and worker workspace against the captured subject. After writable
experiments, capture the chosen final candidate for a final-state check when a
reusable acceptance is worthwhile. The same collaborator can do several chunks
before that boundary. Do not recheck every trivial edit just to earn a receipt.
The helper does not prove that an earlier command in a mutating run tested its
final output. It refuses that claim instead of trusting a final prose assurance.

**Stale or anchoring history.** Fresh requirements win. Relevant source/reference
changes make the old binding stale; missing or changed evidence makes it unknown.
All recorded raw streams are rehashed at recording and recall. Current contract
and scope matching do not observe remote services, toolchain changes, hidden
inputs, changed user intent or unrecorded dependencies. Include meaningful version
information in the contract and recheck external conditions separately. Matching
bindings are not an attestation of a current execution environment.

**Context and bookkeeping costs.** Nothing is loaded automatically. Select one
relevant receipt, read its bounded summary, and fetch detailed evidence only when
needed. Do not paste the whole history into `context.md`. Even with small output,
rehashing large captures/logs can be expensive. No cross-run cache, arbitrary
optimization or automatic invalidation exception is added in this experiment.

**Trust and lifecycle.** Local JSON, hashes, permissions and advisory locks are
not a same-user security boundary. `actor` is self-attributed, never authenticated
human approval. Raw JSONL is Codex-reported evidence, not an independent OS audit.
Checks are observed, not atomic against arbitrary outside writers. Keep evidence
until its decision is no longer needed; deleting runs makes recall unknown. No
permission or native setting is changed by any receipt disposition.

## Verification and local go/no-go

Run from this checkout (Python 3.12+ on macOS or Ubuntu):

```zsh
python3 -B -m unittest discover -s tests -p 'test_decision_receipts.py' -v
python3 -B -m unittest discover -s tests -p 'test_*.py' -v
```

The tests include one real runner invocation with a fake Codex worker performing
two actual local checks, without dispatching a worker per check. The fake proves
protocol integration, not model quality or actual Codex sandbox enforcement.
Other tests cover non-Git judgment, expected failures, two runs/one decision,
changed candidates/criteria, missing evidence, bounded output, concurrent updates,
interrupted persistence, and unchanged installed-plugin files.

Use this local evaluation prompt with Claude, Codex, or Antigravity:

```text
Evaluate the checkout-only decision-receipt experiment for issue #22.
Read experiments/README.md and the actual diff. Do not change native models,
permissions or the stable plugin. Do not merge or publish anything.

First run the offline tests and inspect their assertions. Then, with my normal
signed-in local tools, test one worker doing several chunks/checks with a single
useful final decision. Use the existing Council capture and execution path; the
receipt helper must not cause another worker to be dispatched on its own.

Compare fresh-context-only handling with explicit receipt lookup after a
compaction or interruption, including a rejected alternative with unchanged
inputs and another whose requirements have changed. Also include a simple task
where the correct decision is not to use receipts at all. Use equivalent current
inputs and available evidence for the comparison; do not give the receipt variant
privileged facts. Preserve current requirements over any historical decision.

Record actual model/tool calls, total added context bytes, repeated experiments,
stale acceptance/rejection mistakes, and human bookkeeping. Distinguish deterministic
binding checks from model judgment. A smaller summary is not proof of a better
decision. Report pass/fail/unknown with evidence and recommend keep, revise, or
remove. Do not enlarge this into a workflow engine to make the evaluation pass.
```

Promotion needs evidence of reduced repeated work or better handoff without worse
stale decisions or unnecessary calls/context. If the fresh brief already suffices,
leaving this experiment unused, or removing it, is a successful design outcome.
