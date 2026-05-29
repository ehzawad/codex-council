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
    **"if nothing material, say so clearly"** so the role can return
    silence rather than bluffing when out of its lens.
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
and pass its path with `--roles-file`**, and write the gathered context
to `context.md` in the same staging directory and pass it with
`--context-file`. Roles are supplied only by file — by design. A
multi-role JSON array embedded in a shell argument is the single most
common launch failure (one stray quote or unbalanced brace breaks the
call); keeping the panel in a file written with the Write tool means
there is no shell escaping to get wrong. A council call takes as long as the
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
every Write path and every later Bash command. If `mktemp` prints
`/tmp/claude-501/codex-council.diJhSz`, then roles, context, stdout, and stderr
must all be under that exact directory. A different parent path is a launch
bug; stop and fix the paths before running.

```bash
# 0. Create exactly one private staging dir (mode 0700). Copy the exact stdout
#    path; that printed path is ABS_RUNDIR for every later Write and Bash call.
mktemp -d "${TMPDIR:-/tmp}/codex-council.XXXXXX"

# 1. With the Write tool, write these exact files under the printed ABS_RUNDIR:
#      ABS_RUNDIR/roles.json
#      ABS_RUNDIR/context.md
#    Do not run mktemp again, do not use $TMPDIR again, and do not substitute a
#    different temp root such as /var/folders.
#    Panel shape:
#    [{"id":"<lens>","label":"<Title>","instruction":"<single paragraph; name
#      the specific failure modes; include: if nothing material, say so clearly;
#      end with: Thoroughness beats speed.>"}, ...]

# 2. Cheap pre-flight: confirm both launch inputs are in the same printed dir.
python3 "${CLAUDE_PLUGIN_ROOT}/skills/codex-council/scripts/codex_council.py" \
  --check-staging-dir 'ABS_RUNDIR'

# 3. Launch with Bash run_in_background: true and NOTHING appended. One layer,
#    foreground command, stdout+stderr redirected into the same printed dir:
python3 "${CLAUDE_PLUGIN_ROOT}/skills/codex-council/scripts/codex_council.py" \
  --roles-file 'ABS_RUNDIR/roles.json' \
  --context-file 'ABS_RUNDIR/context.md' \
  > 'ABS_RUNDIR/out.md' \
  2> 'ABS_RUNDIR/err.log'
```

Then **wait for Claude Code's completion notification — do not sleep-poll.** When
it arrives, read `ABS_RUNDIR/out.md` for the report; read `ABS_RUNDIR/err.log`
for per-role progress and the final `[codex-council] CODEX_COUNCIL_DONE ...` line
(its presence means the report is fully written; it carries the exit code).

The process exit code is deliberately council-level and partial-failure
tolerant: it is `0` when at least one role responds and `1` only when every
role fails. Treat shell status as transport status. Always inspect the report
Summary and the sentinel's `ok=N total=M exit=X` fields before deciding the
council succeeded.

If a run is ever lost or orphaned, recover entirely from disk:

```bash
pgrep -fl 'codex_council[.]py'      # any council alive?
pgrep -fl -- 'ABS_RUNDIR/roles.json' # THIS run specifically (disambiguates when several run)
tail -n 40 ABS_RUNDIR/err.log        # last line CODEX_COUNCIL_DONE -> finished
```

Last err line is `CODEX_COUNCIL_DONE` → done, read `out.md`. No such line but
`pgrep` finds it → still running. No line and no process → it crashed; inspect
`err.log` and any partial `out.md`.

Bare invocation (no `--roles-file`, with context piped or staged) exits 2 — a
safety net for accidental fan-out.

Constraints:

- Max 6 roles per call (matches Codex's concurrent-thread default).
- `id`: `^[a-z0-9_-]+$`, ≤32 chars.
- `label`: non-empty single line, ≤80 UTF-8 bytes.
- `instruction`: non-empty single paragraph, ≤8192 UTF-8 bytes; must include
  "nothing material" and must end with "Thoroughness beats speed."

## Building the context

The snippets below show only how to compose `ABS_RUNDIR/context.md`; the actual
launch must always use Step 4's `--context-file` form — `run_in_background:
true`, stdout+stderr redirected to files, nothing appended.

The context contents are whatever the roles need to see. Context comes
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

In the examples below, `ABS_RUNDIR` is the exact private per-run dir printed by
`mktemp` in Step 4. Every pipeline block starts with
`set -euo pipefail`. Put that line in the same Bash invocation as the pipeline;
shell options do not persist across Claude Code Bash calls. This makes context
extraction fail closed: if an upstream extractor (`git diff`, `git ls-files`,
`tail`, etc.) fails, stale or partial context is not written. Do not add
`|| true` to context pipelines.

Staging context: `git diff HEAD` means tracked worktree vs `HEAD`, so it
includes both staged and unstaged tracked changes. Use `git diff --cached` for
staged-only reviews and `git diff` for unstaged-only reviews. Untracked files
are never included by those diff commands; add them explicitly with the
NUL-delimited snippet below when they matter.

### Shell-extracted

**Uncommitted diff:**

```bash
set -euo pipefail
git diff HEAD > 'ABS_RUNDIR/context.md'
```

**Staged-only diff:**

```bash
set -euo pipefail
git diff --cached > 'ABS_RUNDIR/context.md'
```

**Diff plus untracked files that matter** (with binary / size /
symlink guards):

```bash
set -euo pipefail
{
  git diff HEAD
  git ls-files -z --others --exclude-standard -- |
    while IFS= read -r -d '' f; do
      [ -f "$f" ] && [ ! -L "$f" ] || continue
      mime=$(file --brief --mime -- "$f")
      case "$mime" in
        *charset=binary*) continue ;;
        *charset=utf-8*|*charset=us-ascii*) ;;
        *) continue ;;
      esac
      size=$(wc -c <"$f")
      size=${size//[[:space:]]/}
      [[ "$size" =~ ^[0-9]+$ ]]
      (( size <= 32768 )) || continue
      printf '\n=== untracked file: %q ===\n' "$f"
      cat <"$f"
    done
} > 'ABS_RUNDIR/context.md'
```

**An artifact plus a question** — write the relevant file or excerpt
plus the question the council should answer:

```bash
set -euo pipefail
{
  printf 'Question: %s\n\n' '<what you want the council to check>'
  cat <"$file"      # or: head -50 data.csv, or: pbpaste, etc.
} > 'ABS_RUNDIR/context.md'
```

**Bounded diagnostic transcript** — for a test / CI failure or any
command output the council should diagnose. Bound the noise so the
question survives the 10 MiB cap:

```bash
set -euo pipefail
{
  printf 'Question: %s\n\n' '<what should the council diagnose?>'
  printf 'Command: %s\n' '<the failing command>'
  printf 'Exit status: %s\n\n' "$exit_status"
  echo 'Output (last 128 KiB):'
  tail -c 131072 <"$log_file"
} > 'ABS_RUNDIR/context.md'
```

If the diagnosis needs source context too, append bounded source
excerpts using the same `[ -f ] && [ ! -L ] && file --mime && wc -c`
guards as the diff+untracked snippet above.

### Claude-composed

When Claude already understands the situation, compress the
understanding into prose rather than making Codex re-discover it
from raw source. Write the digest with the Write tool directly to
`ABS_RUNDIR/context.md`, using the exact path printed by `mktemp`.

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

**Never write an empty context file.** If a shell extractor would yield
nothing and there's nothing to digest, write a self-contained question to
`ABS_RUNDIR/context.md` instead.

## Session continuity

One Codex thread per (project, host session, role) tuple, stored at
`$XDG_STATE_HOME/codex-council/{project-hash}-{session-hash}__{role-id}.json`
when a stable host-session id is available. The runner auto-detects common
session identifiers such as Claude session ids, `CODEX_THREAD_ID`,
`TERM_SESSION_ID`, `TMUX_PANE`, `STY`, and `VSCODE_PID`, so separate terminal
tabs/panes in the same repo do not normally share role threads. Follow-up calls
from the same host session resume the per-role thread so each role accumulates
its framing. Stale resumes restart only the affected role — siblings are
unaffected. Role IDs reused across calls continue their own thread; new IDs
start fresh.

`CODEX_COUNCIL_SESSION_KEY` remains an explicit override for custom scoping
(e.g. per branch or task ID). Set `CODEX_COUNCIL_DISABLE_AUTO_SESSION_KEY=1`
only if you intentionally want the older project-wide state file shape:
`{project-hash}__{role-id}.json`.

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
