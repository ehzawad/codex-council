# Panel design

Read this reference when composing a panel is not obvious: choosing how many
roles to run, writing instructions that produce useful replies, choosing a
model or effort per role, or planning writers and follow-up rounds.

## Contents

- Reading the work
- Sizing the panel
- Writing instructions
- Model and effort per role
- Writers, retries, and worktrees
- Follow-up rounds and continuity

## Reading the work

These questions can help when the situation is unclear. Answer them from the
conversation and the workspace; they are not a questionnaire for the user.

- What larger problem is the user solving, and what outcome or decision do
  they need now?
- Which files, modules, drafts, datasets, queries, experiments, tests, or
  deployments are in flight?
- Which bugs, errors, regressions, or risks are they chasing, and what
  hypotheses and evidence already exist?
- What is unknown, and which assumptions might be outdated or wrong?
- Is the work converging on a goal, blocked on a dependency, or still
  exploring?
- Which earlier decisions, rejected approaches, and preferences still
  constrain the work?

Ask the user only when a missing choice would materially change the panel or
the authorized outcome.

## Sizing the panel

There is no default role count. Start from one role and add another only
when it brings a genuinely independent lens: it would look in places, or for
failure modes, that the existing roles would not. Each added role costs a
full Codex run, can become the straggler the run waits on, and adds
reconciliation work.

Examples:

- "Why does this test fail intermittently?" → one diagnosis role with the
  failing output, the test, and the code under test.
- "Review this PR" for a small change → one reviewer. For a change touching
  auth and a data migration → a correctness reviewer and a security reviewer,
  because each would find things the other would not.
- "Implement this feature" → one implementer, plus an independent test or
  verification role if the change is large enough to warrant it.
- A broad audit of a service (API contracts, concurrency, deployment,
  performance) → one role per separable track, four or five or more.

Possible kinds of contribution include implementation, testing, diagnosis,
research synthesis, correctness, operational reliability, adversarial
challenge, and integration. This list is for thinking, not for filling in:
most panels need only a few of these, and many need one. Name each lens in
the vocabulary of the actual work so it could not be mistaken for a stock
role.

Keep roles distinct enough that a role asked about something outside its
lens answers "nothing material" instead of duplicating a sibling.

## Writing instructions

A useful instruction reads like the checklist a domain expert would run for
this exact work; a weak one reads like "review for quality." Name the files,
behaviors, or claims in scope, what the deliverable is, and where to stop.

Two phrases are required by the script:

- An item containing "nothing material", so a role can say plainly that its
  lens found nothing instead of inventing findings.
- A final item that is exactly "Thoroughness beats speed."

Frame each role as a collaborator: it consumes the shared context, separates
verified evidence from inference, and returns its result, evidence,
dependencies on other work, risks, and open questions in a form you can
reconcile. The runner adds a short collaboration brief to every prompt with
the same expectations, so the instruction can focus on the lens itself.

## Model and effort per role

Both keys are optional. Omitting them inherits the model and
`model_reasoning_effort` from `~/.codex/config.toml`, which is the right
choice when the user has not asked for anything different and the panel has
no obvious straggler.

- `model` — any id the user's Codex setup accepts. As of this release the
  GPT-6 family in Codex is `gpt-6-luna` (fast, suited to narrow or mechanical
  checks), `gpt-6-sol` (the general workhorse), and `gpt-6-astra` (the
  hardest, most ambiguous, or longest-horizon lens). Check what the user's
  setup has (their config, or earlier successful runs) and never invent an
  id; Codex rejects an unknown model and that role fails.
- `effort` — a lowercase word passed as `model_reasoning_effort`. The
  supported values depend on the model. Common ones are `none`, `minimal`,
  `low`, `medium`, `high`, `xhigh`, and `max`; `ultra` exists on some models
  but not all (Luna rejects it), and Astra does not accept `none`. Codex
  validates the value, not the plugin, and rejects an unsupported one.

A council takes as long as its slowest role. When one role is narrow (a
single-file check, a mechanical scan), a lower effort or a faster model keeps
it from holding the whole run open while broader roles keep their full
effort. Do not lower effort on the role that carries the hardest judgment.
A low-effort "nothing material" is weak evidence: in a live test a
`gpt-6-luna`/`low` round-trip check declared CSV persistence correct while
missing carriage-return corruption. Spot-check such a verdict before relying
on it, or give that lens more effort when a miss would be costly.

The runner passes these as `codex exec -m <model>` and
`-c model_reasoning_effort="<effort>"` on every invocation, including a
resume. Overrides apply per invocation and are not stored with the thread: a
follow-up that reuses an id and omits them runs on the config default, so
repeat them when continuity matters. If a resumed thread runs on a different
model than it was recorded with, Codex notes it and the role's report shows
that note as a warning.

## Writers, retries, and worktrees

All roles run in the same working directory. When there are several roles
and any of them edits files, let one role own writes and have the others
inspect, test, research, or propose. Multiple writers need serialized phases
(one council after another). Codex also has its own `--worktree` option for
isolated checkouts, but this runner does not use it yet, so do not plan a
panel around per-role worktrees.

Retries can repeat side effects. The runner never auto-retries a role that
had begun tool work before a stall, but rate-limit and 5xx retries do replay
the attempt, so avoid giving independently retried roles overlapping or
irreversible mutations.

While a council runs, your own edits count as a writer too: act early only
on work that cannot collide with a running role that may write.

## Follow-up rounds and continuity

Each `(project, host session, role id)` keeps its Codex thread. Reuse an id
only when the lens and task are continuous, so the role builds on what it
already knows; otherwise mint a new id. Current staged evidence always
overrides what a thread remembers.

When one round's findings should inform another role, stage the findings,
decisions, and open questions into fresh context and re-invoke only the roles
that need them. A running role cannot receive messages. A follow-up launched
while the first council is still running needs different role ids, because
the same id waits on its continuity lock until the running role finishes.
