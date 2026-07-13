---
name: codex-council
description: >-
  Adaptive, context-driven Codex council for project implementation, computer
  science, software/ML engineering, DevSecOps, research, and other complex
  work: Claude orchestrates role-framed `codex exec` agents to collaborate and
  reconcile toward one shared goal. Use for direct invocation
  `/codex-council:codex-council` or when the user invokes "codex council,"
  "codex coterie," or "codex team." For nearby agent-team language, infer the
  intended workflow from the live conversation and project state; ask a brief
  disambiguation only when OpenAI Codex and Claude Code's built-in Agent
  workflow remain genuinely plausible. On invocation, reconstruct what the
  user is actually doing, autonomously ask and answer contextual working
  questions, compose task-specific roles with no built-in catalog, share the
  decision-complete context, announce the panel, and launch without a manual
  approval gate.
---

# Codex Council

Coordinate N bounded-parallel `codex exec` agents around any user goal, each
framed with a role tailored to make a distinct contribution. This is an
AGI-style, general-purpose collaboration pattern rather than a claim that any
underlying model is proven AGI: a role may investigate, build, diagnose,
challenge, create, plan, research, or review across domains, subject to the
active model and available tools. The script aggregates all responses into one
structured markdown report, and Claude reconciles them into one coherent
outcome rather than forwarding a pile of independent opinions. Each role keeps
its own Codex thread per project so useful framing and project knowledge
accumulate across calls.

The skill is general-purpose with a deliberate programmatic center of gravity:
project implementation, computer science, software and ML/AI engineering,
DevSecOps, platform/security/automation, debugging and testing, and technical
research—without turning those fields into a fixed role menu.

**You (Claude) are the orchestrator.** The panel-proposal step is
load-bearing: **ultrathink** there. Read the user's actual work, figure out
what outcome and contributions they need, design role ids / labels /
instructions ground-up from that specific work. There is no catalog, no
checklist of domains, no template panel to reach for. Treat your composed
task-specific panel as granted by default: announce it briefly, then trigger
`codex exec` without asking for launch approval.

## Disambiguation when the requested agent workflow is unclear

Treat the direct slash invocation or a clear use of "codex council," "codex
coterie," or "codex team" as Codex Council intent and go to Step 1. Missing
those exact names is only an ambiguity signal, never an automatic stop. Read the
surrounding conversation, current project, prior turns, and requested outcome:
if the user is clearly continuing council work or asking for OpenAI Codex, use
this skill; if they clearly want Claude Code's built-in `Agent` subagents, use
that dynamic workflow instead.

When both mechanisms remain genuinely plausible, ask one short disambiguation
via `AskUserQuestion` (or concise plain text if that tool is unavailable):

- Question: "Did you mean Claude's built-in Agent subagents, or the Codex
  council/coterie/team?"
- Header: "Which?"
- Option 1: "codex-council (Recommended)"
  - Description: "Use OpenAI Codex role-framed collaborators with shared task
    context and Claude reconciliation toward one result."
- Option 2: "Claude dynamic workflow (ultracode)"
  - Description: "Use Claude Code's built-in Agent subagents with direct tool
    access and Claude-native orchestration."

Follow the selected workflow. Do not ask merely because an exact trigger name
is absent when the user's surrounding intent already resolves the choice.

## Step 1 — Read the actual work

**Ultrathink here.** This is the step where adaptivity lives or dies.

Look at what the user is actually doing in the conversation and workspace.
Privately ask and answer the following contextual questions from available
evidence; this is self-questioning, not a questionnaire for the user:

- What larger problem are they solving, what project are they implementing,
  and what concrete outcome or decision do they need now?
- What have they been editing or asking about, and which files, modules,
  features, objects, drafts, datasets, queries, experiments, or deployments are
  currently being implemented, tested, researched, or operated?
- Which bugs, errors, symptoms, regressions, security risks, performance
  failures, or ambiguous behaviors are they hunting, and what hypotheses and
  evidence already exist?
- What are the known unknowns, plausible unknown unknowns or blind spots,
  missing evidence, and dependencies that could overturn the current path?
- Which constraints or assumptions are unstated, contradictory, outdated, or
  possibly wrong? What does the user appear to believe that the artifacts do
  not yet prove?
- Is the work converging on a defined goal, recursing toward a blocked
  dependency, or zigzagging through exploratory unknowns? What next move would
  create the most information or progress?
- What recent decisions, rejected approaches, user preferences, and research
  findings still constrain the implementation?

Ask the user only when a missing choice would materially change the panel or
authorized outcome. Otherwise infer cautiously, mark uncertainty, and proceed.
Then ask one final synthesis question:

> *What distinct contributions would move this shared goal forward and catch
> its work-specific failure modes?*

That question — answered from the actual material in front of you — is the
panel. Do **not** start by assigning the task to a category and then picking
roles to match it. Category-first is the formulaic trap this plugin is
designed to defeat. Stock labels borrowed from unrelated work do nothing
useful; lenses you derived by reading the current material do.

Cheap probes if you need them (otherwise skip):
- `git status --short`
- `git diff HEAD --stat` if there are changes worth summarizing
- Read the relevant file / draft / dataset / query / artifact the user is
  working with if it isn't already in your context

Compose, don't pattern-match. For the material in front of you, name the
independent contributions the goal needs: implementation, testing, diagnosis,
research synthesis, substantive correctness, operational reliability,
adversarial challenge, integration, or another work-specific contribution.
Each is a candidate lens. The lens names are local to this invocation; they
should not look like they came from a menu, and should not be reused across
unrelated work.

If the user named a panel in the invocation arguments (e.g.
`/codex-council:codex-council 3 agents: <lens-a>, <lens-b>, <lens-c>`),
trust them — turn that into role JSON and skip to Step 3.

Re-examine every invocation. Do not silently reuse a prior proposal;
the work shifts in one turn.

## Step 2 — Compose the panel JSON

Design 2–6 sharply distinct roles by default (default 3). Use more only when
the work genuinely has more independent lenses and the user's Codex
`agents.max_threads` or `CODEX_COUNCIL_MAX_PARALLEL` is deliberately higher;
extra roles are queued when the panel exceeds active concurrency, and the
panel itself has no script-imposed count limit. Each role is
`{id, label, instruction}`:

- `id` — kebab-case, `^[a-z0-9_-]+$`, no length limit. Derive it from the lens
  you just composed for THIS work. Stable IDs reused across invocations resume
  the same Codex thread for that role; novel IDs start fresh. Long IDs are
  hashed only for the internal state filename; the full ID stays intact.
- `label` — non-empty, single-line human title shown in the report, with no
  length limit.
- `instruction` — **a JSON array of short strings, one sentence per
  item** (the script joins them into one paragraph with single
  spaces). Always write the array form, never one long string: a
  multi-kilobyte single-line JSON literal is exactly where file writes
  corrupt — spliced text, lost fields, stray filler keys. Three
  properties make instructions useful (in this order of importance):
  - **Specific to the work.** Name the failure modes this role
    should hunt, in the vocabulary of the actual task. A useful
    instruction reads like a checklist a human expert would run; a
    useless one reads like "review for quality and clarity."
  - **Honest about scope.** Include an item with the literal clause
    **"if nothing material, say so clearly"** so the role can return
    silence rather than bluffing when out of its lens.
  - **Conventionally paced.** Make the final item exactly the literal
    sentence **"Thoroughness beats speed."** This shapes Codex's
    cadence and is checked by tests (the check runs on the joined
    paragraph, so the sentence must come last).

Each role object has **exactly** the keys `id`, `label`, `instruction` —
nothing else. Do not add filler keys like `"_"` or `"notes"`; the script
rejects any unknown key. If a written roles.json fails validation, rewrite
the whole file cleanly — never patch a substring of it.

Roles in the same panel must be **sharply distinct** so a role asked to
review something outside its lens returns "nothing material" instead of
overlapping with its siblings. Overlap = wasted Codex calls.

Frame every role as a collaborator contributing concrete work toward the same
goal, not an isolated opinion generator: consume the shared context, form and
answer lens-specific working questions, distinguish evidence from inference,
and return findings, work, dependencies, risks, and open questions in a form
Claude can reconcile with the other roles.

For implementation tasks in one shared workspace, prevent write races: assign
one explicit executor/integrator to mutate files by default while parallel
roles inspect, test, research, or propose; multiple writers need isolated
worktrees or serialized phases. Retries can repeat side effects, so do not let
independently retried roles perform overlapping or irreversible mutations.

## Step 3 — Announce the panel and proceed

Write one short user-facing paragraph that names what you inferred
the work to be (one sentence on why) and the roles you composed
(id + one-line summary each).

Do **not** call `AskUserQuestion` to confirm the panel, and do **not**
wait for a launch approval response — that manual gate breaks automatic
long-running agentic flows. The composed task-specific role panel is
accepted by default; only if the user explicitly asked to review or
adjust it, ask one short text follow-up ("What to change — different
count, swap a role, retune an instruction?") and recompose. Otherwise
go directly to Step 4.

## Step 4 — Launch the council

After composing and announcing the panel, **write the panel JSON to a file with the Write tool
and pass its path with `--roles-file`**, and write the gathered context
to `context.md` in the same staging directory and pass it with
`--context-file`. Roles are supplied only by file — by design. A
multi-role JSON array embedded in a shell argument is the single most
common launch failure (one stray quote or unbalanced brace breaks the
call); keeping the panel in a file written with the Write tool means
there is no shell escaping to get wrong. A council call takes as long as the
slowest role — the council has no total elapsed-time or run-level deadline,
and a role may think for hours or days while its codex subprocess keeps
producing output bytes. The only liveness control is a per-subprocess
output-inactivity watchdog (`CODEX_COUNCIL_STALL_SECS` seconds of byte
silence, default 1800; 0 disables it) whose stall policy is described in
[runtime-behavior.md](references/runtime-behavior.md). Launch with
**exactly one backgrounding
layer**: the Bash tool parameter `run_in_background: true`. The shell command
itself must stay foreground, and **stdout and stderr must be redirected to
files** so the run is observable while it works and recoverable from disk if the
completion notification is ever lost.

**Do not add a second backgrounding/detach layer.** `run_in_background: true`
already wraps the command in a shell that Claude Code tracks and notifies you
about when it exits. Any inner detach makes that wrapper exit immediately: you
get a **false "completed" in ~0s with empty output**, and `codex_council.py` is
reparented to `launchd`/PID 1 — orphaned, untracked, and it will **never** send
the real completion notification. Forbidden in the launch command: a trailing
`&`; zsh `&!` or `&|`; `nohup`; `setsid`; `disown` (before or after launch, incl.
`cmd & disown`); `bg`; `coproc`; `( ... ) &`; `{ ...; } &`; `sh -c '... &'` /
`zsh -c '... &'`; any wrapper function/script that forks and exits; a bare
`>/dev/null` (redirect to files instead); and piping/wrapping into a supervisor
such as `launchctl`, `tmux new -d`, `screen -dm`, `at`, `batch`, or `daemonize`.

**Stage everything in one private per-run directory**, not fixed world-readable
`/tmp` names. The report and context can hold sensitive reviewed content, and
predictable `/tmp/council_*` files are world-readable under a typical umask and
can be pre-created or symlinked by another local user (who could then read the
report or forge the `CODEX_COUNCIL_DONE` line). Run `mktemp -d` exactly once.
The exact path printed by `mktemp` is the only valid run directory for this
invocation. Do not recompute it from `$TMPDIR`, `${TMPDIR:-/tmp}`, `/tmp`,
`/var/folders`, `pwd`, or another `mktemp` call; do not normalize it or switch
temp roots between tool calls. Shell variables do **not** persist across Claude
Code Bash and Write calls, so paste the printed absolute path literally into
every Write path and every later Bash command; roles, context, stdout, and
stderr must all be under that exact printed directory. A different parent
path is a launch bug; stop and fix the paths before running.

```bash
# 0. Create exactly one private staging dir (mode 0700). Copy the exact stdout
#    path; that printed path is ABS_RUNDIR for every later Write and Bash call.
mktemp -d "${TMPDIR:-/tmp}/codex-council.XXXXXX"

# 1. With the Write tool, write these exact files under the printed ABS_RUNDIR:
#      ABS_RUNDIR/roles.json
#      ABS_RUNDIR/context.md
#    Do not run mktemp again, reuse $TMPDIR, or substitute another temp root.
#    Panel shape — instruction is ALWAYS an array of sentence-sized strings,
#    and each object has exactly these three keys (unknown keys are rejected):
#    [
#      {
#        "id": "<lens>",
#        "label": "<Title>",
#        "instruction": [
#          "<one sentence naming a specific failure mode to hunt>",
#          "If nothing material, say so clearly.",
#          "Thoroughness beats speed."
#        ]
#      }
#    ]

# 2. Cheap pre-flight: confirm both launch inputs are in the same printed dir.
python3 "${CLAUDE_PLUGIN_ROOT}/skills/codex-council/scripts/codex_council.py" \
  --check-staging-dir 'ABS_RUNDIR' --skill-contract 1

# 3. Launch with Bash run_in_background: true and NOTHING appended. One layer,
#    foreground command, stdout+stderr redirected into the same printed dir:
python3 "${CLAUDE_PLUGIN_ROOT}/skills/codex-council/scripts/codex_council.py" \
  --roles-file 'ABS_RUNDIR/roles.json' \
  --context-file 'ABS_RUNDIR/context.md' \
  --skill-contract 1 \
  > 'ABS_RUNDIR/out.md' \
  2> 'ABS_RUNDIR/err.log'
```

`--skill-contract 1` pins the SKILL/script contract epoch; a mismatch is a
stale SKILL/script pair — re-run `scripts/dev-link.sh` and restart the session.

After launch, keep the Claude Code background-task id. The runner writes
per-role start/completion lines immediately and a status heartbeat to
`ABS_RUNDIR/err.log` while work remains; each heartbeat carries per-role
`quiet=Ns` (or `retry-wait` during backoff) plus the `watchdog=` threshold.

- If this Claude Code version exposes session crons, create a **one-shot
  30-minute wake-up** whose prompt names the background task id and exact
  `ABS_RUNDIR`; at wake-up, retrieve the native background-task status, read
  the end of `err.log`, send the user a concise update
  (completed/active/queued), and schedule another one-shot wake-up only if the
  run continues.
- Otherwise, use Claude Code's native background-task wait/output mechanism
  (`TaskOutput` when exposed) with its supported wait horizon — never a shell
  `sleep` loop — and at each horizon read `err.log`, update the user, and
  continue through the same native mechanism.

Claude Code emits a completion notification for a finished background task.
When it arrives, read `ABS_RUNDIR/out.md` for the report and `ABS_RUNDIR/err.log`
for per-role progress and the final `[codex-council] CODEX_COUNCIL_DONE ...`
line (its presence means the report is fully written; it carries the exit code).

The process exit code is deliberately council-level and partial-failure
tolerant: it is `0` when at least one role responds and `1` only when every
role fails. Treat shell status as transport status. Always inspect the report
Summary and the sentinel's `ok=N total=M exit=X` fields before deciding the
council succeeded.

If a run is ever lost or orphaned, recover entirely from disk:

```bash
pgrep -fl 'codex_council[.]py'      # any council alive?
pgrep -fl 'ABS_RUNDIR/roles.json'   # THIS run specifically (disambiguates when several run)
tail -n 40 'ABS_RUNDIR/err.log'     # last line CODEX_COUNCIL_DONE -> finished
```

Triage in exactly this order — first match wins. `active` is scheduling state,
not proof of health: `quiet=Ns` measures time since the last output byte, not
semantic progress, so never describe a role as working normally solely because
it is active or has low quiet. The runner recovers from output inactivity
only; semantic wedge detection is out of scope.

1. `CODEX_COUNCIL_DONE` present → finished; read `out.md`; never re-invoke.
2. A `[retriable:stall]` or stall-termination line present → the runner is
   handling it; do not launch another council.
3. Active roles with `quiet` below the printed `watchdog=` value → keep
   monitoring; report the run as "output-active", not healthy.
4. `quiet` at/past the watchdog with no stall line after a short grace and a
   fresh read → the watchdog itself is suspect: stop the tracked background
   task, confirm the council process is gone, inspect `err.log`, then
   re-invoke exactly once.
5. `watchdog=disabled` → no automatic liveness recovery; rising quiet is
   indeterminate; require an explicit manual decision.
6. No sentinel and no process → it crashed; diagnose `err.log` and any
   partial `out.md` before any reinvocation.

Bare invocation (no `--roles-file`) exits 2 — a safety net for accidental fan-out.

Role contract (no plugin-imposed content-size or panel-count caps):

- The panel may contain any number of roles. Active concurrency defaults to 6,
  follows a positive user-level Codex `agents.max_threads` when configured, or
  an explicit `CODEX_COUNCIL_MAX_PARALLEL`; remaining roles wait in an
  in-process queue rather than launching simultaneously.
- `roles.json` and `context.md` must be regular, non-symlink files, so the
  validated private staging directory cannot redirect either input elsewhere.
- Each role object: exactly the keys `id`, `label`, `instruction`; any
  other key is rejected with exit 2.
- `id`: non-empty and `^[a-z0-9_-]+$`; no length limit.
- `label`: non-empty and single-line; no length limit.
- `instruction`: an array of non-empty strings, one sentence per item —
  the only accepted form. The script joins items with single spaces and
  validates the joined paragraph: no length limit; must include "nothing
  material" and must end with "Thoroughness beats speed."

If `--check-staging-dir` — or the launch itself, which re-validates each
input's parent-directory privacy the same way before reading any content —
rejects the directory (wrong mode, symlink, wrong owner, missing):
**abandon that directory.** Do not chmod it, do not mkdir it, and do not
reuse its name — a predictable, hand-created path defeats the privacy the
gate exists for, and files already written there may have been exposed. Run
`mktemp -d` again, copy the NEW printed path, re-Write BOTH `roles.json`
and `context.md` into it, and re-run the pre-flight on that new path.

## Building the context

The context contents are whatever the roles need to understand and advance the
same live goal. Context comes from one or both sources:

- **Shell-extracted from disk** — raw artifacts the user is looking
  at right now. The shell composition and its safety guards matter.
- **Claude-composed context** — when Claude already has the understanding and
  Codex does not need raw source. Preserve every materially relevant fact,
  decision, uncertainty, and artifact reference; remove only genuinely
  irrelevant or duplicated material.

Build a **decision-complete working set**, not a literal transcript dump. The
script does not impose a byte ceiling on `context.md`, stdin, role fields, or
the composed prompt, and it never truncates them; relevance selection belongs
to Claude as the host orchestrator. For a long-running session, assemble
context in this order:

1. **Problem, project, trajectory, and immediate objective:** state what the
   user is solving or implementing, the result needed now, the current
   branch/worktree/runtime state, and whether the work is goal-directed,
   recursively blocked, or exploratory/zigzagging through unknowns.
2. **In-flight work:** identify files, modules, features, objects, drafts,
   datasets, queries, experiments, deployments, tests, and research currently
   being changed, implemented, validated, or operated.
3. **Active problems and hypotheses:** preserve bugs, errors, symptoms,
   regressions, security/performance failures, failing commands, attempted
   fixes, working theories, and the evidence for or against them.
4. **Recent working context at high fidelity:** include recent user constraints,
   decisions, actions, outputs, and artifacts that directly led to the current
   state. Preserve exact wording or raw material when details matter.
5. **Current primary evidence:** include relevant files, diffs, diagnostics,
   data, research sources, or commands verified live rather than recalled.
6. **Older durable context as a faithful summary:** carry decisions, rejected
   approaches and why, invariants, user preferences, earlier evidence, and
   dependencies that still constrain today's work. An old fact is included
   whenever removing it could change the recommendation; age alone is never a
   reason to discard it.
7. **Unknowns, assumptions, and provenance:** distinguish verified current
   state from host summaries and inference; name known unknowns, plausible
   blind spots, missing evidence, unstated or possibly wrong assumptions, and
   what observation would resolve each material uncertainty.

Exclude superseded state, conversational repetition, stale intermediate
outputs, and unrelated history. If Claude Code compacted the host conversation,
use the compaction summary as an index, re-check live project state, and carry
forward the old details that remain decision-relevant. Do not compress or
excerpt merely to satisfy this plugin: there is no plugin size budget. The
active model, provider, OS, and memory still impose downstream or physical
limits; if one is reached, preserve the staged source material and surface the
actual downstream error rather than silently dropping context.

When extracting diffs, files, untracked artifacts, or diagnostics from disk,
read [context-staging.md](references/context-staging.md) and use its fail-closed,
filename-safe recipes. The actual launch still uses Step 4's `--context-file`
background flow.

### Claude-composed

When Claude already understands the situation, carry that understanding into
prose rather than making Codex re-discover it from raw source. Preserve all
material details and remove only redundancy or information unrelated to the
decision. Write it with the Write tool directly to `ABS_RUNDIR/context.md`,
using the exact path printed by `mktemp`.

Three common context scopes:

- **Project context** — what the codebase IS. Purpose, architecture,
  load-bearing modules, conventions, current direction, known constraints.
  For evaluating the project as a whole without a specific change in flight.
- **Live problem-solving and implementation map** — what the user is trying to
  accomplish now; in-flight modules/artifacts/tests/research; observed bugs,
  errors, and hypotheses; what has been tried; known unknowns and blind spots;
  possibly wrong assumptions; blockers, ownership, and the next decision or
  executable step. For building, debugging, operating, or researching a
  project through uncertain terrain.
- **Session retrospective** — what we DID this session. Goal, files
  touched, decisions made, open questions, current branch state.
  For meta-questions about accumulated work.

Mark uncertainty explicitly. When state matters, verify live (e.g.
`git status --short --branch`) rather than recalling it from memory.

**Never write an empty context file.** If a shell extractor would yield
nothing and there's no context to add, write a self-contained question to
`ABS_RUNDIR/context.md` instead.

## Runtime continuity and retries

Each `(project, host session, role)` has a persisted Codex thread. Reuse a role
ID only for a semantically continuous lens and task; otherwise mint a new ID,
and always let current staged evidence override remembered assumptions. The
runner retries transient 429/5xx and replay-safe stall failures once, never
retries auth or quota failures, imposes no run-level deadline beyond the
output-inactivity watchdog, and keeps continuity-lock waiters out of active
concurrency. Read
[runtime-behavior.md](references/runtime-behavior.md) when scoping sessions or
diagnosing continuity, retries, stalls, long runs, or heartbeat state.

## After Codex Council responds

The output is markdown:

```
# Codex Council — N/M roles responded (T.Ts)

## Summary
- **<Label>** [<id>]: ok — 12.3s
- **<Label>** [<id>]: FAILED — 0.4s

## <Label> (<id>)
<full reply or _Failed: error tag and message_>
```

Reconcile every contribution toward the user's shared goal: combine compatible
work, choose between conflicting recommendations with reasons, preserve useful
dissent, and turn the panel into one actionable outcome. Do not merely relay N
answers. Then report your own read — what you accept, what you challenge, what
remains uncertain. Failed-role messages for recognized failure classes
start with a bracket-tagged class (`[auth]`, `[retriable:rate-limit]`,
`[retriable:5xx]`, `[retriable:stall]`, `[stall]`,
`[orchestrator-exception]`, `[orchestrator-bug]`); unrecognized failures
carry the raw stderr untagged.

The parallel roles do not directly message one another during a single fan-out;
collaboration happens through the shared context, complementary role contracts,
and Claude's reconciliation. When cross-pollination would improve the result,
stage the first-round findings, decisions, and open questions into fresh
context and re-invoke only the relevant roles; reuse IDs only for semantically
continuous follow-up. Default to one round when it is sufficient.
