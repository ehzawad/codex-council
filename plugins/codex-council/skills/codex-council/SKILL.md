---
name: codex-council
description: >-
  Multi-perspective Codex review via parallel `codex exec` sub-agents.
  Claude (the orchestrator) composes the role panel on-the-fly per
  invocation from the user's actual work; there is no built-in role
  catalog and no domain list. Auto-use ONLY when the user's text
  contains the phrase "codex council" or "codex team reconciliation"
  (and close variants such as "ask codex council," "codex council
  review," "reconcile with codex team"). Otherwise stop — broader
  phrases like "codex agent team," "codex panel," "fan out to codex
  agents," "agent team," "agents in parallel," "subagents," "council
  review," "panel review," "multi-angle review" route to Claude
  Code's built-in `Agent` tool. Direct invocation
  `/codex-council:codex-council`. On invocation, ultrathink about
  what the user is actually doing, compose a tailored panel
  ground-up, confirm via AskUserQuestion, then launch.
---

# Codex Council

Fan out a prompt to N parallel `codex exec` sub-agents, each framed
with a role tailored to the work. The script aggregates all responses
into one structured markdown report. Each role keeps its own Codex
thread per project so framings accumulate across calls.

Codex is strongest on technical and structured reasoning. Whether a
council adds value over a single pass should be judged from the
current task, not from a label on it.

**You (Claude) are the orchestrator.** The panel-proposal step is
load-bearing: **ultrathink** there. Read the user's actual work,
figure out what judgment they need, design role ids / labels /
instructions ground-up from that specific work. There is no catalog,
no checklist of domains, no template panel to reach for. Then confirm
via `AskUserQuestion` and only after that trigger `codex exec`.

## Disambiguation gate — only if "codex council" / "codex team" is missing

If the trigger phrase that fired this skill does **not** contain
"codex council" or "codex team reconciliation" (or a close variant),
**stop**. The user may have meant Claude Code's built-in `Agent` tool
(subagents spawned with `subagent_type` like `general-purpose`,
`Plan`, `Explore`, `claude-code-guide`, `code-reviewer`) — a
different mechanism (Claude's model with direct tool access, not
OpenAI Codex via `codex exec`).

Ask one short disambiguation via `AskUserQuestion` before doing
anything:

- Question: "Did you mean Claude's built-in `Agent` subagents, or
  codex-council (OpenAI Codex fan-out)?"
- Header: "Which?"
- Option 1: "Claude Agent subagents (Recommended)"
- Option 2: "codex-council (OpenAI Codex)"

Only proceed past this gate if the user explicitly picks codex-council.

If the trigger phrase **does** clearly say "codex council" or
"codex team reconciliation," skip this gate and go to Step 1.

## Step 1 — Read the actual work

**Ultrathink here.** This is the step where adaptivity lives or dies.

Look at what the user is actually doing in the conversation: what
they've been editing, what they've been asking, what files / objects
/ drafts / queries are in flight. Then ask one question:

> *What are 2–4 distinct kinds of judgment that would catch the
> failure modes specific to this work?*

That question — answered from the actual material in front of you —
is the panel. Do **not** start by assigning the task to a category
and then picking roles to match it. Category-first is the formulaic
trap this plugin is designed to defeat. Stock labels borrowed from
unrelated work do nothing useful; lenses you derived by reading the
current material do.

Cheap probes if you need them (otherwise skip):
- `git status --short`
- `git diff HEAD --stat` if there are changes worth summarizing
- Read the relevant file / draft / dataset / query / artifact the
  user is working with if it isn't already in your context

Compose, don't pattern-match. For the material in front of you,
name 2–4 independent ways it could fail or mislead — wrong on the
substance, wrong for the audience, wrong against prior art, wrong
in some specific operational dimension, etc. Each is a candidate
lens. The lens names are local to this invocation; they should not
look like they came from a menu, and you should not reuse them
across unrelated work.

If the user named a panel in the invocation arguments (e.g.
`/codex-council:codex-council 3 agents: <lens-a>, <lens-b>, <lens-c>`),
trust them — turn that into role JSON and skip to Step 3.

Re-examine every invocation. Do not silently reuse a prior proposal;
the work shifts in one turn.

## Step 2 — Compose the panel JSON

Design 2–4 roles (default 3, max 6). Each role is
`{id, label, instruction}`:

- `id` — kebab-case, `^[a-z0-9_-]+$`, ≤32 chars. Derive from the
  lens you just composed for THIS work. Stable IDs reused across
  invocations resume the same Codex thread for that role; novel IDs
  start fresh.
- `label` — human title shown in the report.
- `instruction` — a single paragraph. Three properties make
  instructions useful (in this order of importance):
  - **Specific to the work.** Name the failure modes this role
    should hunt, in the vocabulary of the actual task. A useful
    instruction reads like a checklist a human expert would run; a
    useless one reads like "review for quality and clarity."
  - **Honest about scope.** Include the literal clause
    **"if nothing material, say so clearly"** (or a close
    paraphrase) so the role can return silence rather than bluffing
    when out of its lens.
  - **Conventionally paced.** End with the literal sentence
    **"Thoroughness beats speed."** This shapes Codex's cadence and
    is checked by tests.

Roles in the same panel must be **sharply distinct** so a role asked
to review something outside its lens returns "nothing material"
instead of overlapping with its siblings. Overlap = wasted Codex
calls.

## Step 3 — Confirm with the user (interactive Q/A)

Write one short user-facing paragraph that names what you inferred
the work to be (one sentence on why) and the roles you composed
(id + one-line summary each).

Then call `AskUserQuestion` to gate the launch:

- Question: "Run this <N>-agent council, or adjust the panel?"
- Header: "Panel"
- `multiSelect: false`
- Option 1: "Run as proposed (Recommended)" — description names the
  roles
- Option 2: "Adjust the panel" — description: "I'll ask one
  follow-up"

Branches:

- **"Run as proposed"** → proceed to Step 4.
- **"Adjust the panel"** → ask one short text follow-up ("What to
  change — different count, swap a role, retune an instruction?")
  and recompose before proceeding.

**Fallback if `AskUserQuestion` is unavailable**: ask the same
confirmation as one plain-text question and wait for the user's next
turn. Do not fan out without confirmation.

## Step 4 — Launch the council

Once confirmed, **write the panel JSON to a file with the Write tool
and pass its path with `--roles-file`**, piping the gathered context
on stdin. Roles are supplied only this way — by design. A multi-role
JSON array embedded in a shell argument is the single most common
launch failure (one stray quote or unbalanced brace breaks the call);
keeping the panel in a file written with the Write tool means there is
no shell escaping to get wrong. A council call takes as long as the
slowest role and has **no wall-clock cap** — a role may think for hours
or days. Launch it with **exactly one backgrounding
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

**Stage everything in a private per-run directory**, not fixed world-readable
`/tmp` names. The report and the piped context can hold sensitive reviewed
content, and predictable `/tmp/council_*` files are world-readable under a
typical umask and can be pre-created or symlinked by another local user (who
could then read the report or forge the `CODEX_COUNCIL_DONE` line). Make one
private dir per run with `mktemp -d` (mode `0700`) and keep roles, context, and
the output files inside it. Shell variables do **not** persist across Claude
Code Bash calls, so capture the printed path and substitute it literally for
`RUNDIR` in the Write calls and the launch command.

```bash
# 0. Private per-run staging dir (mode 0700). Note the printed path and use it
#    literally as RUNDIR in every step below.
mktemp -d "${TMPDIR:-/tmp}/codex-council.XXXXXX"

# 1. With the Write tool (not a heredoc, so there is zero shell escaping) write
#    the panel to RUNDIR/roles.json and the gathered context to RUNDIR/context.md.
#    Panel shape:
#    [{"id":"<lens>","label":"<Title>","instruction":"<single paragraph; name
#      the specific failure modes; include: if nothing material, say so clearly;
#      end with: Thoroughness beats speed.>"}, ...]

# 2. Cheap pre-flight: confirm the JSON parses before fanning out.
python3 -c 'import json,sys; json.load(open(sys.argv[1], encoding="utf-8"))' RUNDIR/roles.json

# 3. Launch with Bash run_in_background: true and NOTHING appended. One layer,
#    foreground command, stdout+stderr redirected into the private dir:
python3 "${CLAUDE_PLUGIN_ROOT}/skills/codex-council/scripts/codex_council.py" \
  --roles-file RUNDIR/roles.json \
  < RUNDIR/context.md \
  > RUNDIR/out.md \
  2> RUNDIR/err.log
```

Then **wait for Claude Code's completion notification — do not sleep-poll.** When
it arrives, read `RUNDIR/out.md` for the report; read `RUNDIR/err.log`
for per-role progress and the final `[codex-council] CODEX_COUNCIL_DONE ...` line
(its presence means the report is fully written; it carries the exit code).

If a run is ever lost or orphaned, recover entirely from disk:

```bash
pgrep -fl 'codex_council[.]py'      # any council alive?
pgrep -fl -- 'RUNDIR/roles.json'    # THIS run specifically (disambiguates when several run)
tail -n 40 RUNDIR/err.log           # last line CODEX_COUNCIL_DONE -> finished
```

Last err line is `CODEX_COUNCIL_DONE` → done, read `out.md`. No such line but
`pgrep` finds it → still running. No line and no process → it crashed; inspect
`err.log` and any partial `out.md`.

Bare invocation (no `--roles-file`, with context piped) exits 2 — a
safety net for accidental fan-out.

Constraints:

- Max 6 roles per call (matches Codex's concurrent-thread default).
- `id`: `^[a-z0-9_-]+$`, ≤32 chars.
- `label`: non-empty.
- `instruction`: non-empty single paragraph.

## Building the context

The snippets below show only how to compose the stdin context; the actual launch
must always use Step 4's form — `run_in_background: true`, stdout+stderr
redirected to files, nothing appended.

The stdin contents are whatever the roles need to see. Context comes
from one of two sources:

- **Shell-extracted from disk** — raw artifacts the user is looking
  at right now. The shell composition and its safety guards matter.
- **Claude-composed prose digest** — when Claude already has the
  understanding and Codex doesn't need the raw source to evaluate
  the question. Compress, don't dump.

Choose the smallest sufficient slice. The script hard-rejects input
over 10 MiB (by byte count); it does not truncate. That ceiling is a
sanity guard, not a budget — Codex's own context window is the real
limit, so compress regardless.

In the examples below, `RUNDIR` is the private per-run dir from Step 4 and
`RUNDIR/roles.json` stands in for your panel (write it with the Write tool
first). The trailing `> RUNDIR/out.md 2> RUNDIR/err.log` is Step 4's mandatory
redirection — these are complete launches, so each must still run under Bash
`run_in_background: true` with nothing appended. The examples vary only in how
the stdin context is built.

### Shell-extracted

**Uncommitted diff:**

```bash
git diff HEAD | python3 ${CLAUDE_PLUGIN_ROOT}/skills/codex-council/scripts/codex_council.py --roles-file RUNDIR/roles.json > RUNDIR/out.md 2> RUNDIR/err.log
```

**Diff plus untracked files that matter** (with binary / size /
symlink guards):

```bash
{ git diff HEAD
  git ls-files --others --exclude-standard | while IFS= read -r f; do
      [ -f "$f" ] && [ ! -L "$f" ] || continue
      file --mime "$f" | grep -q 'charset=binary' && continue
      [ "$(wc -c <"$f")" -gt 32768 ] && continue
      printf '\n=== %s ===\n' "$f"
      cat <"$f"
  done
} | python3 ${CLAUDE_PLUGIN_ROOT}/skills/codex-council/scripts/codex_council.py --roles-file RUNDIR/roles.json > RUNDIR/out.md 2> RUNDIR/err.log
```

**An artifact plus a question** — pipe the relevant file or excerpt
plus the question the council should answer:

```bash
{ printf 'Question: %s\n\n' '<what you want the council to check>'
  cat <"$file"      # or: head -50 data.csv, or: pbpaste, etc.
} | python3 ${CLAUDE_PLUGIN_ROOT}/skills/codex-council/scripts/codex_council.py --roles-file RUNDIR/roles.json > RUNDIR/out.md 2> RUNDIR/err.log
```

**Bounded diagnostic transcript** — for a test / CI failure or any
command output the council should diagnose. Bound the noise so the
question survives the 10 MiB cap:

```bash
{ printf 'Question: %s\n\n' '<what should the council diagnose?>'
  printf 'Command: %s\n' '<the failing command>'
  printf 'Exit status: %s\n\n' "$exit_status"
  echo 'Output (last 128 KiB):'
  tail -c 131072 <"$log_file"
} | python3 ${CLAUDE_PLUGIN_ROOT}/skills/codex-council/scripts/codex_council.py --roles-file RUNDIR/roles.json > RUNDIR/out.md 2> RUNDIR/err.log
```

If the diagnosis needs source context too, append bounded source
excerpts using the same `[ -f ] && [ ! -L ] && file --mime && wc -c`
guards as the diff+untracked snippet above.

### Claude-composed

When Claude already understands the situation, compress the
understanding into prose rather than making Codex re-discover it
from raw source. Pipe via a quoted heredoc — not `echo`, which is
fragile with multiline content and unsafe if the content contains
`$VAR`, `$(...)`, backticks, or backslashes:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/codex-council/scripts/codex_council.py --roles-file RUNDIR/roles.json > RUNDIR/out.md 2> RUNDIR/err.log <<'EOF'
<Claude-composed digest goes here>
EOF
```

Two common digest scopes:

- **Project digest** — what the codebase IS. Purpose, architecture,
  load-bearing modules, conventions, current direction, known
  constraints. For when the council should evaluate the project as
  a whole without a specific change in flight.
- **Session retrospective** — what we DID this session. Goal, files
  touched, decisions made, open questions, current branch state.
  For meta-questions about accumulated work.

Mark uncertainty explicitly. When state matters, verify live (e.g.
`git status --short --branch`) rather than recalling it from memory.

**Never pipe an empty string.** If a shell extractor would yield
nothing and there's nothing to digest, write a self-contained
question and pipe it via heredoc instead.

## Session continuity

One Codex thread per (project, role) pair, stored at
`$XDG_STATE_HOME/codex-council/{project-hash}__{role-id}.json`.
Follow-up calls resume the per-role thread so each role accumulates
its own framing across Claude Code sessions. Stale resumes restart
only the affected role — siblings are unaffected. Role IDs reused
across calls continue their own thread; new IDs start fresh.

Set `CODEX_COUNCIL_SESSION_KEY` before launching Claude Code to
scope a session away from the project-wide council threads (e.g.,
per branch or task ID). The state-file name becomes
`{project-hash}-{session-hash}__{role-id}.json`.

## Retries

- Rate-limit (429) and 5xx errors retry once with exponential backoff.
- Auth errors never clear state and never retry — fix the auth then
  re-run.
- No wall-clock timeout is enforced — each role runs as long as Codex
  takes (hours or days is fine). `codex exec` has no run-level timeout
  either; its per-provider stream-idle guard only covers a stalled
  connection and is retried. Ctrl+C still tears down every in-flight
  codex process group.

## After Codex Council responds

The output is markdown:

```
# Codex Council — N/M roles responded (T.Ts)

## Summary
- **<Label>** [<id>]: ok — 12.3s
- **<Label>** [<id>]: ok — 18.7s
- **<Label>** [<id>]: FAILED — 0.4s

## <Label> (<id>)
<full reply>

## <Label> (<id>)
<full reply>

## <Label> (<id>)
_Failed: <error tag and message>_
```

Reconcile across roles: where they agree, that's a strong signal;
where they conflict, surface the disagreement rather than collapsing
it. Then report your own read — what you accept, what you challenge,
what remains uncertain. Failed-role messages start with a
bracket-tagged class (`[auth]`, `[retriable:rate-limit]`,
`[retriable:5xx]`, `[orchestrator-exception]`, `[orchestrator-bug]`)
so the failure mode is machine-readable.

Multi-turn is available when a reply warrants follow-up; default is
single-turn.
