# codex-council

Adaptive, on-demand **OpenAI Codex collaboration for Claude Code**: task-shaped
contributions, captured inputs, disposable experiments, and one reconciled result.
Claude decides when collaboration is useful, supplies current context, and remains
responsible for the outcome. The runner owns execution and evidence, not judgment.

A contribution can investigate, reproduce, test, research, compare alternatives, or
build an authorized candidate. One is often enough. There is no role catalog,
model ranking, fixed panel size, peer-debate loop, or automatic per-turn hook.

## Requirements

| Component | Supported |
|---|---|
| Claude Code | 2.1.x, with plugin skills |
| Codex CLI | codex-cli 0.163 or later, signed in |
| Python | 3.12 or later, on macOS or Linux |

The skill speaks contract epoch 6. The runner uses only the standard library and
POSIX process controls. Missing policy metadata blocks restricted execution rather
than granting bypass. Neither native user configuration nor the host Claude
model/effort is edited. The fake-Codex test suite makes no claim about real-model
quality or real-host sandbox enforcement; [Testing](#testing) describes the
opt-in signed-in host checks. See [DESIGN.md](DESIGN.md) for the mechanism.

## Install and invoke

```zsh
claude plugins marketplace add ehzawad/codex-council
claude plugins install codex-council@codex-council
```

Invoke `/codex-council:codex-council` or ask for a “codex council.” Claude may also
offer one, or use it, when an independent contribution could clearly change the
next decision; nothing triggers it automatically, and a new turn alone is never a
reason. Requests for Claude's own subagents remain a separate workflow.

For the turn that invokes it, the skill pre-approves only the runner commands,
exactly as SKILL.md writes them; each run directory comes from the runner's own
`--new-run`. That pre-approval does not carry into later turns, so SKILL.md tells
Claude to invoke the skill again before a follow-up `--status`, `--freshness` or
`--cancel` and before every new council. Claude Code asks before Claude itself
invokes a skill that pre-approves tools, so in a `dontAsk` or other unattended
session invoke `/codex-council:codex-council` directly or allow
`Skill(codex-council:codex-council)`. Such a session also needs, in its session or
project settings, Write permission for the runs directory (outside the project; see
[Where runs live](#where-runs-live); a rule for the temporary directory no longer
covers it) and an allow rule for the runner command pattern. The skill does not
pre-approve Write. `--start` prints its `--follow`, `--status` and `--cancel`
commands with absolute interpreter and script paths, which do not match that allow
rule; SKILL.md has Claude use the `python3 "${CLAUDE_PLUGIN_ROOT}/..."` form instead.

The main skill is deliberately small. It reads focused references when needed,
not all documentation on every invocation. Tests and project documentation are
outside the installed plugin source (`plugins/codex-council`).

## How a council works

![How a council works: you ask Claude; Claude, deciding within your instructions, has the runner freeze a snapshot of your project; each Codex worker works on its own copy; replies and evidence go to the run folder, which keeps them and the snapshot across restarts; Claude reads the results and answers you. A dashed note beside Claude marks optional, experimental notes of Claude's position, never required.](docs/diagrams/overview.svg)

Claude stays the orchestrator and decides, within your instructions. The runner is a
standard-library Python CLI that captures inputs, launches one detached supervisor
per run directory and records evidence; every contribution is a `codex exec` worker
in its own scratch copy. The dashed note is an optional
[experiment](#optional-decision-receipts-experiment), not part of the plugin.

| Part | What it is |
|---|---|
| Claude | Runs the `codex-council` skill: briefs each contribution, selects models, reconciles evidence. |
| Runner | `codex_council.py`; each step is its own foreground command. |
| Supervisor | One detached process per run: holds `supervisor.lock`, runs workers with bounded parallelism, records evidence. |
| Codex workers | One `codex exec` per role, each in its own scratch copy of the snapshot. |
| Run folder | The private run directory: inputs, snapshot, copies, catalogs, evidence, replies, `result.json`, `out.md`. |

## One invocation

![One council, step by step: a sequence diagram across You, Claude, the runner with its run folder, and the Codex workers. You ask; Claude has the runner create a new run folder, writes the brief into it and has the runner snapshot the files and store a copy per worker; Claude checks, then starts; the runner starts each worker on its own copy, and each replies with evidence, which the runner saves to the run folder; Claude reads the results, checks freshness with the runner, weighs the evidence, and answers you.](docs/diagrams/council-sequence.svg)

Each step is its own foreground command with `--skill-contract 6`:

| Step | Command | What happens |
|---|---|---|
| New run | `--new-run` | Allocates a fresh private run directory and prints its absolute path. |
| Brief | none | Claude writes `request.json`, `roles.json` and `context.md` there. |
| Snapshot | `--prepare RUNDIR` | Observed-stable capture, read-only baseline, per-role copies, `run.json`. |
| Models | `--discover RUNDIR` | Per-role metadata probe in that role's scratch cwd. Claude may then fill model, effort and selection from the snapshots; `service_tier` only on an explicit user request. |
| Check | `--check-staging-dir RUNDIR` | Contract, capture, selection authoring and execution policy. Exit 2 names the fix: model, effort, selection or `service_tier` fields are fixed in place; anything else needs a new run. |
| Start | `--start RUNDIR` | Claims `supervisor.lock`, `err.log` and `out.md` and spawns the detached supervisor. |
| Follow | `--follow RUNDIR` | Relays progress under Monitor; Claude reads settled replies as they arrive. |
| Confirm | `--status RUNDIR` | Done only when the lock is free and the recorded runner identity is gone. `--cancel` stops a verified runner; `--reap` signals verified recorded workers once the runner is gone. |
| Freshness | `--freshness RUNDIR` | Are the captured scope and staged context unchanged? |
| Reconcile | none | Claude weighs `result.json`, replies and attempt evidence. |

A run directory is used once: anything other than model/effort/selection/service_tier
changes needs a new one. Claude stages a fresh, decision-complete brief whenever a
council is useful, whether that is one or ten turns after the last run. A stable
task id and logical project identity are separate from the captured input hash and
optional Codex thread. Previous conclusions are context, not automatically current
evidence.

Preparation captures **working bytes**, not just Git HEAD: dirty tracked files,
staged/unstaged edits, deletions, and non-ignored untracked files. A partially staged
file is captured as its working version. Non-Git capture uses explicit include paths
and keeps empty directories; a context-only question may select no files. Pause
overlapping writers during capture: the runner refuses observed changes but does
not claim an atomic snapshot of arbitrary external writers. Paths that collide
under Unicode canonical caseless matching, whether files, empty directories or
directory components such as `Foo/` and `foo/`, refuse the capture. The source and
the run directory must not overlap: `--prepare` refuses a `source_root` that
contains the run directory, such as `$HOME`, which holds `~/.local/state`, and says
why. Choose a narrower `source_root`, or set `XDG_STATE_HOME` outside it in the
environment Claude Code starts with (this also moves saved threads and role locks),
then allocate a new run.

Every contribution receives an independent copy of the same baseline, so Claude may
continue live work afterward. Copies share no Git control metadata or hardlinks and
avoid Codex's worktree/resume incompatibility. Git-dependent build commands can need
additional setup; a missing prerequisite is a coverage gap, not a source bug.

## Input and execution contracts

See [invocation-contract.md](plugins/codex-council/skills/codex-council/references/invocation-contract.md)
for the JSON examples, field grammar and capture scope. Permissions are separate
from role labels:

| Capability | Default | Scope |
|---|---|---|
| Command sandbox | read-only | workspace-write for scratch experiments; bypass requires an authorization and `network: true`. |
| Command network | false | `true` requires workspace-write or bypass and an authorization; read-only always has it off. |
| External tools | deny | Apps/plugins/browser/computer/hooks/subagents/web, notify, installer/remote-plugin features and configured MCP servers disabled; native integrations require an authorization. |
| Thread continuity | fresh | Explicit resume within logical source, task, host session and role. |
| Required contribution | true | Missing execution/evidence leaves required coverage incomplete. |

Preflight shows role count, model/effort plan, context bytes, watchdog, scratch
scope, capabilities and continuity, and the launch reobserves execution policy
before inference. Configuration evidence is not an attestation of a later process:
managed settings, unsupported fields or an unknown tool inventory fail closed. No
silent bypass or model hopping is used as recovery.

Known credential, configuration, agent-instruction and cache paths met during
selection are excluded and recorded in `run.json`; that list is neither a complete
inventory of ignored paths nor a secret scan, so put any project rule a role needs
in the brief. Native model inheritance means what Codex resolves in the **scratch
execution cwd**, not the source project's `.codex/config.toml`. Every role has its
own catalog snapshot; pin supported values explicitly when that distinction
matters. Every worker invocation and policy probe also sends
`-c project_root_markers=[]`: Codex takes the role's scratch copy as the project
root and loads no AGENTS.md or `.codex` layer from a Git repository above it, which
matters because runs live under `$HOME` (possibly a dotfiles repository). The probe
must observe that empty list; otherwise execution is blocked. `--discover` reads
configuration without that override, so for a run inside another Git work tree it
can report native defaults from a layer the workers do not load; pin explicitly
when that matters.

Fast mode is opt-in per role and only on an explicit user request: Claude sets that
role's `service_tier` to `fast`, which costs more usage. Not every model has a Fast
tier, so preflight and launch check the tier against that role's own discovery and
refuse with exit 2 when it is not advertised or cannot be proven. It is a
per-invocation override; `~/.codex/config.toml` is never edited. Reports state what
was sent, not which model or tier served a turn.

## Lifecycle and evidence

![Life of a run: a state machine. Claude asks for a new run (Created); it becomes Snapshot taken once Claude writes the brief and the runner freezes your files. A failed check needs a fix (model, effort or selection), or the run is left for a new one; a passing check makes it Checked, and start makes it Running. It ends Finished when the workers finish or the runner fails, Stopped when cancelled or shut down cleanly, or Runner gone when the runner is killed or the power is lost. A run is removed only when you prune old runs (30 days by default), whether it ended or was left unstarted.](docs/diagrams/run-lifecycle.svg)

The monospace words are what `--status` reports for each ending.

### Where runs live

`--new-run` creates `<state>/codex-council/runs/<UTC time>-<16 hex>/`, where
`<state>` is `$XDG_STATE_HOME` when that is an absolute path and `~/.local/state`
otherwise, so by default `~/.local/state/codex-council/runs/`. Unlike the earlier
`mktemp -d` under `$TMPDIR`, a run persists across reboots and temporary-file
cleaning: its replies, raw attempts, `result.json` and candidate bytes stay
available for reconciliation and deliberate recovery. A reboot ends the runner and
its workers; the run directory stays, and `--status` then reports `interrupted`, or
`gone` if the runner was killed outright. Missing directories are created 0700
whatever the umask. `--new-run` refuses with exit 2, and repairs nothing, when
`<state>/codex-council` (the state directory) or its `runs/` is a symlink,
foreign-owned or not private, or when the path holds a single quote or a control
character, which the skill's single-quoted path cannot carry. `<state>` itself may
be a symlink or have any mode. Every mode still accepts any private 0700 directory.
Details:
[Run directories](plugins/codex-council/skills/codex-council/references/runtime-behavior.md#run-directories).

### Running and evidence

The detached supervisor relies on private-directory gates, a lock token, process
identities, liveness ticks, bounded concurrency and conservative cleanup.
`preparing` means bounded metadata work, not worker dispatch. `--follow` is read-only.
The final line alone does not prove completion: `--status` requires a free lock
and a vanished recorded identity. `--cancel` stops a verified detached runner;
`--reap` signals verified recorded workers only after the runner is gone.

| Output | Meaning |
|---|---|
| run.json / baseline / workspaces | Invocation identity, exact captured inputs and independent experimental copies. |
| catalogs/ | Model/configuration discovery in each actual execution cwd. |
| evidence/ | Private raw stdout/stderr, attempt metadata, policy observations, launch snapshots and candidate artifacts. |
| result.json | Versioned observations, completeness and applicability, separate from collaborator interpretation. |
| replies/ / out.md | Early role replies and the readable final report. |

Directory and file names under these folders use the role keys recorded in
`run.json`; an id longer than 32 characters gets a hashed key.

Command fields in JSONL are Codex-reported observations, not an independent OS audit.
The runner never infers substantive correctness from a clean exit or agreement.
Artifact manifests cover added, removed and changed regular files, executable-flag
changes included; small text changes also get a candidate diff, while binary or
large changes stay in scratch. Other permission changes and empty-directory changes
are not recorded. A symlink or special file a role leaves is listed by path and
kind, never followed or diffed. **Nothing automatically applies, commits, or
publishes a candidate change to the live source.** Claude checks current intent,
freshness, evidence and integration results before accepting it.

Exit 0 requires a delivered report, at least one successful role and complete
required contributions. Exit 1 covers no successful role, undelivered output, or
incomplete required coverage/evidence; exit 2 is invalid input or refused policy.
`--freshness` exits 0 unchanged, 1 stale/unknown (a moved or deleted source is
unknown), 2 invalid contract. Workers start only from `--start` on a completed
capture, once per run directory.

## Cleaning up old runs

Nothing removes runs automatically, and the skill prunes only when you ask. Ask
Claude to prune old council runs, or run the runner yourself. It is
`plugins/codex-council/skills/codex-council/scripts/codex_council.py` in a checkout;
an installed copy is in Claude Code's plugin cache, by default
`~/.claude/plugins/cache/codex-council/codex-council/<version>/skills/codex-council/scripts/`.

```zsh
python3 plugins/codex-council/skills/codex-council/scripts/codex_council.py --prune-runs
```

| Command | Effect |
|---|---|
| `--prune-runs` | Lists each entry of the runs directory as eligible, skipped (with the reason) or ignored. Deletes nothing. |
| `--prune-runs --delete` | Removes the eligible runs. |
| `--older-than DAYS` | Sets the age, a whole number of days (default 30). |

Only these are eligible:

- **Ended**: a launched run whose status recorded its end more than DAYS days ago:
  finished (`done` or `aborted`) or stopped (`interrupted`, as by `--cancel` or a
  clean shutdown).
- **Interrupted, runner gone**: a run still marked running because its runner was
  killed outright, as at a power loss, whose last tick is more than DAYS days old.
- **Never launched**: a run with no launch records in which nothing changed for
  DAYS days and at least one day, so a prune never races a capture in progress.

A launched run must also have its recorded supervisor lock free, and its recorded
runner and Codex workers verifiably gone; unknown liveness is a skip. Only recorded
processes count: a background process a tool left running is not detected and does
not block the prune. The prune holds a launched run's lock while removing it,
works through directory descriptors, never follows a link and never crosses
filesystems. A never-launched run is first renamed `.pruning-<name>` and checked
again; a leftover `.pruning-<name>` is skipped by every later prune. Saved threads
and role locks are never touched. Exit 0 means done, 1 a removal failed partway
(what remains is left in place), 2 invalid arguments or an unsafe runs directory.
Details:
[Pruning runs](plugins/codex-council/skills/codex-council/references/runtime-behavior.md#pruning-runs).

A prune never removes a launch that stopped before its runner wrote `status.json`
(`--status` says `ended before dispatch`), nor any run outside the runs directory,
such as a `mktemp -d` run from before 1.4.0 or one under an earlier
`XDG_STATE_HOME`. Remove such a run by hand once nothing of it is running (check
with `--status`): `chmod -R u+w DIR && rm -rf DIR` (its frozen baseline is
read-only).

## Continuity and failures

![Fresh or continued thread: an activity diagram. Without a request to continue, the default, a worker starts a fresh thread. When asked, it continues a thread saved for the same project, task, session and role, or starts fresh if there is none. If Codex no longer has the saved thread, the worker starts fresh and warns that earlier turns are lost when repeating is safe; otherwise it stops, keeping the thread so what it did can be checked first.](docs/diagrams/continuity.svg)

"Safe to repeat" is the replay rule described below. A role waits, without taking a
fan-out slot, while another council holds the same continuity lock (logical
source, task, host session and role).

Saved threads are scoped by `CODEX_COUNCIL_SESSION_KEY`, then detected host ids,
then project scope. `CODEX_COUNCIL_SESSION_KEY` is the reliable scope: a detected
`CLAUDE_CODE_SESSION_ID` can keep the startup id after `--resume` or `--continue`,
and integrated terminals in the same VS Code window share `VSCODE_PID`. Thread state
does not save a model or effort, and a resumed history does not establish current
facts. Saved state has no automatic expiry; do not delete lock files while runs use
them.

A role gets at most two logical attempts, separated by a five-second backoff after a
transient 429/5xx or a replay-safe stall. Replay is safe only for a read-only role
with external tools denied whose attempt showed no side-effect-capable or unreadable
output: the exec JSONL omits some tool calls, so a writable role is never replayed.
Otherwise the failure is `replay-blocked:*` and needs deliberate recovery. Quota,
authentication and model rejection are terminal and keep valid continuity.

Settings: `CODEX_COUNCIL_MAX_PARALLEL` defaults to 6,
`CODEX_COUNCIL_STALL_SECS` to 1800 (0 disables), `CODEX_COUNCIL_MODEL_ROUTING` to
auto (off disables automatic choices), and state under
`$XDG_STATE_HOME/codex-council` when that is an absolute path, otherwise
`~/.local/state/codex-council` (a 0700 directory with 0600 lock files whatever the
umask; an owned state directory with any other mode is reset to 0700, while a
symlinked, foreign-owned or non-directory state path refuses the launch); runs live
in its `runs/` subdirectory. There is no run-level deadline. Detached work keeps
running and spending until completion or cancellation; do not abandon observation.
See [runtime recovery](plugins/codex-council/skills/codex-council/references/runtime-behavior.md).

## Security and known limits

Scratch separation is not a whole-process security boundary. Codex command sandbox
modes do not promise to hide HOME or constrain every external integration. An
orchestrator-written authorization string is a declaration, not independently
authenticated user consent. Same-user/bypass processes can tamper with files;
checksums and read-only modes detect mistakes, not all malicious activity. Use
trusted inputs and verify actual sandbox denials on the target host.

Excluded data, dependency state, Git metadata, ACLs/xattrs and remote services are
not recreated. `--freshness` compares selected file bytes and executable flags,
empty directories, Git HEAD and selected index entries, the recorded omission list
and the staged context; it does not check omitted file contents, user intent or
external dependencies. Raw logs may hold sensitive tool output; keep them private.
Private means POSIX mode bits only: an ACL inherited from a parent directory, as
macOS allows, can still grant another user access, and the runner does not strip
ACLs. The exec reader holds each attempt's protocol output in memory, so runner
RAM is not bounded for arbitrarily chatty workers.

## Optional decision receipts (experiment)

`experiments/decision_receipts.py` is a checkout-only experiment outside the
installed plugin. On request it records Claude's revisable working position over a
council's evidence and recalls it later against current inputs. It never
dispatches, approves or gates anything, and nothing requires it; a `status` exit 0 is
not approval. Usage is in its `--help`. A deliberate `--prune-runs --delete` removes
runs, and receipts bound to them then read unknown.

## Testing

From the repository root, with Python 3.12 or later:

```zsh
env -u CODEX_COUNCIL_LIVE_TESTS -u CODEX_COUNCIL_LIVE_WORKFLOW \
  python3 -m unittest discover -s tests -p 'test_*.py' -v
uvx --from 'ruff==0.15.21' ruff check .
claude plugin validate plugins/codex-council
claude plugin validate .
```

The offline suite runs unit, fake-Codex protocol, CLI and real-process liveness
tests; it uses no Codex account and no inference. CI runs it on Ubuntu (Python
3.12 to 3.15) and macOS (3.13), plus the same Ruff check.

The README diagrams are SVG files generated by `docs/diagrams/generate.py` (standard
library only). After editing it, run `python3 docs/diagrams/generate.py`;
`tests/test_diagrams.py` fails when a committed SVG differs from the generator's
output.

Two opt-in gates use the installed `codex`, the signed-in account and real usage.
Run them only after the offline checks pass; each needs its variable set to `1`:

```zsh
CODEX_COUNCIL_LIVE_TESTS=1 python3 -m unittest tests.test_live_codex -v
CODEX_COUNCIL_LIVE_WORKFLOW=1 python3 -m unittest discover -s tests -p 'test_live_workflow.py' -v
```

`test_live_codex` checks model/effort routing, the `-c` overrides sent and thread
continuity on trivial prompts. `test_live_workflow` checks that workspace-write
allows a scratch write but denies an external write and a command-network
connection, and that read-only denies both writes, using temporary canaries and a
host-local listener. A model that declines the probe is not a pass, and a refused
policy is never a reason to enable bypass. The workflow gate prints the private
evidence directories it keeps; inspect them, then, once the runner has exited,
remove each with `chmod -R u+w DIR && rm -rf DIR` (the captured baseline is
read-only, so a plain `rm -rf` fails). Smoke-test threads stay in Codex history.

For a host smoke check, start one fresh session from this checkout, which changes
nothing installed:

```zsh
claude --plugin-dir ./plugins/codex-council
```

Invoke `/codex-council:codex-council` on a small task, then check the brief,
`--status`, `result.json` and `--freshness` before accepting the result. Record the
commit, OS, Claude Code and Codex versions, and host model/effort. Do not change
global model settings to test the plugin.

## License

[MIT](LICENSE).
