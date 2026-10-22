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

The skill speaks contract epoch 5. The runner uses only the standard library and
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

For the turn that invokes it, the skill pre-approves `mktemp -d` and the runner
commands exactly as SKILL.md writes them. That pre-approval does not carry into
later turns, so SKILL.md tells Claude to invoke the skill again before a follow-up
`--status`, `--freshness` or `--cancel` and before every new council. Claude Code
asks before Claude itself invokes a skill that pre-approves tools, so in a `dontAsk`
or other unattended session invoke `/codex-council:codex-council` directly or allow
`Skill(codex-council:codex-council)`. Such a session also needs, in its session or
project settings, Write permission for the run directory (it is outside the project;
a Write rule for the temporary directory is enough) and an allow rule for the runner
command pattern. The skill does not pre-approve Write. `--start` prints its
`--follow`, `--status` and `--cancel` commands with absolute interpreter and script
paths, which do not match that allow rule; SKILL.md has Claude use the
`python3 "${CLAUDE_PLUGIN_ROOT}/..."` form instead.

The main skill is deliberately small. It reads focused references when needed,
not all documentation on every invocation. Tests and project documentation are
outside the installed plugin source (`plugins/codex-council`).

## Architecture

Claude stays the orchestrator. The runner is a standard-library Python CLI that
captures inputs, launches one detached supervisor per run directory and records
evidence; every contribution is a `codex exec` worker in its own scratch copy.

```
 Claude Code session
 +------------------------+               +-------------+
 | codex-council skill    | writes        | Private run |
 | briefs, selects,       | request.json, | directory   |
 | reconciles             |-------------->|             |
 +------------------------+ roles.json,   | run.json    |
   |                        context.md    | baseline/   |
   | runs each command                    | workspaces/ |
   v                                      | catalogs/   |
 Runner CLI: codex_council.py             | evidence/   |
 +------------------------+               | replies/    |
 | --prepare, --discover, | capture and   | result.json |
 | --check-staging-dir    |-------------->| out.md      |
 |                        | per-role      |             |
 |                        | copies        |             |
 +------------------------+               |             |
 | --start                |               |             |
 +------------------------+               |             |
 | --follow, --status,    | reads         |             |
 | --freshness,           |-------------->|             |
 | --cancel, --reap       |               |             |
 +------------------------+               |             |
   |                                      |             |
   | --start spawns                       |             |
   v                                      |             |
 +------------------------+ status.json,  |             |
 | Detached supervisor    | err.log, raw  |             |
 | own session, holds     |-------------->|             |
 | supervisor.lock        | attempts,     |             |
 |                        | replies       |             |
 +------------------------+               +-------------+
   |     ^           |     ^
   |     |           |     |    v  bounded fan-out
   v     |           v     |    ^  JSONL, reply
 +-------------+   +-------------+
 | codex exec  |   | codex exec  |
 | role A      |   | role B      |
 | own scratch |   | own scratch |
 | copy        |   | copy        |
 +-------------+   +-------------+
 Codex workers, one per role
```

## One invocation

Each step is its own foreground command with `--skill-contract 5`. A run directory
is used once: anything other than model/effort/selection/service_tier changes
needs a new one.

```
 Claude: mktemp -d, write request.json, <-----------+
 roles.json and context.md                          |
   |                                                |
   v                                                |
 --prepare: observed-stable capture, read-only      |
 baseline, per-role copies, run.json                |
   |                                                |
   v                                                |
 --discover: per-role metadata probe in that        |
 role's scratch cwd                                 |
   |                                                |
   v                                                |
 Claude: optionally fill model, effort and          |
 selection from the snapshots; service_tier         |
 only on an explicit user request                   |
   |                                                |
   v                                                |
 --check-staging-dir: contract, capture, <-----+    |
 selection authoring, execution policy         |    |
   |                                           |    |
   +-- exit 2: selection --> Fix model,        |    |
   |   effort, selection or service_tier       |    |
   |   fields ---------------------------------+    |
   |                                                |
   +-- exit 2: needs recapture --> Recapture        |
   |   in a new directory --------------------------+
   |
   | exit 0
   v
 --start: claim supervisor.lock, err.log, out.md;
 spawn the detached supervisor
   |
   v
 --follow under Monitor; read settled replies as
 they arrive
   |
   v
 --status: lock free and recorded runner identity
 gone
   |
   +-- not responding --> --cancel the verified runner
   +-- gone, live groups --> --reap verified codex groups
   |
   v
 --freshness: captured scope and staged context
 unchanged?
   |
   v
 Claude reconciles result.json, replies and attempt
 evidence
```

Claude stages a fresh, decision-complete brief whenever a council is useful, whether
that is one or ten turns after the last run. A stable task id and logical project
identity are separate from the captured input hash and optional Codex thread.
Previous conclusions are context, not automatically current evidence.

Preparation captures **working bytes**, not just Git HEAD: dirty tracked files,
staged/unstaged edits, deletions, and non-ignored untracked files. A partially staged
file is captured as its working version. Non-Git capture uses explicit include paths
and keeps empty directories; a context-only question may select no files. Pause
overlapping writers during capture: the runner refuses observed changes but does
not claim an atomic snapshot of arbitrary external writers. Paths that collide
under Unicode canonical caseless matching, whether files, empty directories or
directory components such as `Foo/` and `foo/`, refuse the capture.

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
matters. Fast mode is opt-in per role and only on an explicit user request: Claude
sets that role's `service_tier` to `fast`, which costs more usage. Not every model
has a Fast tier, so preflight and launch check the tier against that role's own
discovery and refuse with exit 2 when it is not advertised or cannot be proven. It
is a per-invocation override; `~/.codex/config.toml` is never edited. Reports state
what was sent, not which model or tier served a turn.

## Lifecycle and evidence

```
 Host      C  Claude
 Runner    R  Runner CLI
           S  Detached supervisor
 Workers   X  codex exec per role
 Evidence  D  Run directory
 Arrows:   --> call or write, <.. reply

 C   R   S   X   D
 |-->|   |   |   |  --prepare, --discover,
 |   |   |   |   |  --check-staging-dir
 |   |---------->|  run.json, baseline/,
 |   |   |   |   |  workspaces/, catalogs/
 |-->|   |   |   |  --start, its own foreground call
 |   |-->|   |   |  spawn in a new session with the
 |   |   |   |   |  held lock
 |   |   |------>|  supervisor.json, then status.json
 |   |   |   |   |  ticks
 |<..|   |   |   |  started line and follow/status/
 |   |   |   |   |  cancel commands
 |   |   |-->|   |  scratch cwd, explicit policy,
 |   |   |   |   |  current brief
 |   |   |<..|   |  JSONL events and reply
 |   |   |------>|  raw attempts, replies/role.md,
 |   |   |   |   |  result.json, out.md
 |-->|   |   |   |  --follow relays err.log progress
 |-->|   |   |   |  --status: lock free, runner
 |   |   |   |   |  identity gone
 |-->|   |   |   |  --freshness before using any
 |   |   |   |   |  result
```

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

## Continuity and failures

```
 Continuity key: logical source, task,
 host session, role
   |
   v
 Continuity lock free? <---------------------+
   |                                         |
   +-- held by another council -->           |
   |   Wait without a fan-out slot ----------+
   |
   | acquired
   v
 execution.continuity
   |
   +------------------------+
   | fresh                  | resume
   v                        v
 New Codex thread         Resume the saved thread
   |   ^                    |    |    |
   |   |  thread missing,   |    |    | thread missing,
   |   |  replay-safe       |    |    | possible tool work
   |   +--------------------+    |    v
   |                             |  replay-blocked:stale
   |                             |  thread and effects kept
   +-----------------------------+
   |
   v
 Complete current captured brief
   |
   v
 New evidence tied to the current capture
```

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
symlinked, foreign-owned or non-directory state path refuses the launch). There is
no run-level deadline. Detached work keeps running and spending until completion or
cancellation; do not abandon observation. See
[runtime recovery](plugins/codex-council/skills/codex-council/references/runtime-behavior.md).

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
