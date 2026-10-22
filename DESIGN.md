# Codex Council: captured collaboration

## Responsibility boundary

Claude interprets the task, decides whether a contribution is worth commissioning,
composes its lens, selects grounded model settings, and reconciles the outcome.
The runner validates contracts, captures inputs, establishes execution policy,
coordinates Codex processes, and records observations. It never scores substantive
claims, ranks models, chooses a fixed panel, schedules periodic self-invocation,
or merges candidate artifacts.

A host/model combination can make different decisions from another. Correctness
therefore has two layers: deterministic runtime invariants, and behavioral evals
of invocation/context/reconciliation. A successful decision not to invoke is a
valid behavioral outcome. Universal task support is an interface property, not
a promise that unavailable tools/data or unobservable outcomes can be verified.

## Identity and state

Four identities are distinct: logical source project, task/investigation,
captured inputs, and optional Codex thread. The source root and task id join the
host-session and role key for continuity. A new scratch directory does
not accidentally create a new logical project. Fresh is the prepared default;
resume is explicit and history is subordinate to the complete current brief.
Saved-thread state lives in a 0700 directory with 0600 lock and state files
whatever the umask, under `$XDG_STATE_HOME` when that is an absolute path (a
relative value is ignored, as the XDG base-directory specification requires) or
else `~/.local/state`; an owned state directory with any other mode is reset to
0700, while a symlinked or foreign-owned one is refused by the pre-flight and
again at launch, and is never changed.

The staged request, context and role capability/lens contracts are frozen by
preparation. Model/effort/selection/service_tier stay fillable after discovery.
Context changes, new requirements or a different contribution require recapture
rather than quietly reusing stale input identity. Hashes bind artifacts, not
semantic correctness.

## Capture backend

The capture backend makes independent copies for Git and non-Git projects. Git is a
read-only filename/index/HEAD source, never an instruction to verify only committed
HEAD. Current tracked and selected untracked bytes, deletions and an
executable/non-executable flag are captured, not the full permission mode (copies
are 0600/0700, the baseline 0400/0500). Before/after inventories and file identity
stamps reject observed
mutation. This is not an atomic filesystem snapshot: coordinate overlapping writers
briefly and report limitations around uncooperative external writers.

Copies do not share writable inodes or Git control directories with the live tree.
Selected symbolic links and special files are refused by name; so, in implicit Git
scope, are a submodule (populated, empty or missing) and an untracked nested
repository. Each refusal names the path and the fix (exclude it, or select a
submodule's or nested repository's files with include), so none is silently
flattened or dropped. Collisions under Unicode canonical caseless matching are
refused among selected file paths, empty directories and every directory
component (`Foo/` and `foo/`, or `Foo/a.txt` and `foo/b.txt`). Known credential,
agent-config, instruction and cache paths met during selection are omitted and
recorded; the list is not a complete inventory of ignored paths and not a secret
scanner. ACLs, xattrs, submodules, sparse layout, remote data and dependency
environments are not reproduced.

The frozen baseline is read-only to catch mistakes, not a same-uid trust boundary.
Each role gets a separate writable copy and runs tests/experiments only according
to its explicit capabilities. Missing Git metadata or dependencies can block a
baseline; blocked setup is not evidence of an implementation defect. Worktrees are
not required for isolation here, and Codex's worktree/resume incompatibility is
avoided rather than hidden.

## Execution policy

Role names do not grant capabilities. Read-only is default; workspace-write enables
experimental files in that role's scratch cwd. Network, native integrations and
bypass require an authorization declaration. Bypass must acknowledge that network
denial cannot be enforced by that sandbox mode. Native tools retain their own
privileges, independent of command-network policy.

The adapter reads actual-cwd configuration, enumerates MCP names, then reads again
with requested restrictions. It removes extra writable roots and implicit temporary
write grants, sets non-interactive approval, ignores user and project execpolicy
rules (an allow rule would run its command outside the sandbox), turns memories
off, and, with external tools denied, empties notify, disables the examined
external-tool features and explicitly disables the discovered MCP servers by bare
name. Wrong/unknown types, managed overrides or requirements, an unaddressable MCP
name or an altered inventory block execution. Command network requires
workspace-write or bypass and an authorization. Preflight reports the policy and
the detached child reobserves it; --start repeats only the local contract, capture
and selection-authoring checks before detaching. Launch setup publishes liveness
between bounded metadata operations so long panels do not look like dead runners.

There is a remaining check-to-exec window. Config observations are not a serving
process attestation, and the command sandbox is not a sandbox of the entire Codex
process or of all host reads. An authorization string supplied by Claude is not an
independent authorization authority. A live-host denial suite is a release gate.
Failure never silently grants bypass or edits user configuration.

## Model discovery and selection

Discovery and execution use each role's actual scratch cwd, the same Codex
binary/environment and no profile override. Relative CODEX_HOME is normalized
before relocation, without copying credentials. Source agent configuration and
instruction files are not copied (see the
[invocation contract](plugins/codex-council/skills/codex-council/references/invocation-contract.md)),
so source-layer defaults may differ from scratch-native defaults; preflight says
so, and no claim of source configuration parity is made.

Discovery reads account, configuration, managed requirements and the model catalog
through one adapter, with strict value grammar, authoring validation, launch-time
revalidation, explicit pins, native fallback and per-role provenance. Each role owns
its planning snapshot in `catalogs/<key>/`; at launch each automatically selected
role, and each role that requests a service tier, gets a fresh discovery in its
own cwd, kept as `evidence/<key>/launch-model-snapshot.json`. Other inherited and
pinned roles skip that catalog discovery; execution-policy probes still run.
Routed and native-effort choices both reject an advertised retirement that has
passed (for native effort, the proven native model's), and the discovery summary
marks native-effort adjustment unavailable on a retired native model. No thread or
turn starts during model or policy discovery.
A service tier (fast mode) is set only on an explicit user request, never by
routing. Preflight and launch check it against the tiers advertised for the
resolved model, or the proven native model when no model is sent, and refuse with
exit 2 when it is not advertised or cannot be proven; both also read effective
configuration with the worker's overrides to confirm the model and tier hold.
Nothing ranks ids, versions, recommended markers, or reputation.
Report values describe what was sent, not an attestation of what served a turn.

## Execution and lifecycle

The POSIX supervisor is authoritative. --start exclusively claims a private
directory, passes its held lock into a new session, and waits up to ten seconds for
the child's supervisor record: exit 0 can mean starting, running or already ended
(its note and --status say which), even when --start's own stdout is closed or
broken, and exit 1 is a spawn failure or a child that exited before writing its
record. Only the supervisor holds that lock; Codex children do not inherit it. Both
lock identity/token and process-start identity are checked. Directories are one-use
and unknown states are conservative. Signals go only to verified recorded
identities while the lock state agrees; the check and the signal are separate
operations, not an atomic guarantee.

Bounded fan-out and nonblocking per-continuity-key locks serialize runs with the
same logical source, task, host-session scope and role; a role waiting on its key
gives up its fan-out slot. Every child has its own process group. Byte-inactivity watchdogs,
bounded post-exit drain, descendant cleanup, signal latching, reply-first persistence,
and orphan reaping complete the lifecycle. The supervisor is not a reboot-persistent job scheduler.
Same-user tampering, reparented descendants and a SIGKILL before child recording are
not solved by the workspace copies.

`preparing` is an activity signal for metadata work, not worker dispatch. During
the asynchronous fan-out status.json is republished on transitions and about every
15 seconds; preparation publishes between bounded metadata operations, and
synchronous filesystem work or finalization can leave longer gaps. --follow is
read-only and notices lost status/identity. Terminal text is advisory: --status
confirms a free supervisor lock and a vanished recorded identity, and --cancel
reports success only once the lock is observed free. If a present runner stops
responding, cancel it before reaping anything. Unknown means do not signal. An
unattended host must continue observing or cancel before returning.

`--status` combines the supervisor lock, supervisor.json, status.json and the
process table into one state for a detached run:

```
 --start spawns the supervisor
   |
   v
 +-------------------+               +------------------+
 | starting          | exits before  | ended before     |
 | lock held, no     |-------------->| dispatch         |
 | status.json yet   | status.json   | lock free        |
 +-------------------+               +------------------+
   |                                   |
   | status.json published             v
   v                                  end
 +-------------------+               +------------------+
 | running           | unreadable or | unknown          |
 | lock held,        | replaced      | lock and records |
 | fresh tick        | records       | disagree         |
 |                   |-------------->|                  |
 |                   |<--------------|                  |
 |                   | records agree +------------------+
 |                   | again
 |                   |
 |                   | killed        +------------------+
 |                   | without a     | gone             |
 |                   |-------------->| lock free, no    |
 |                   | terminal      | terminal state   |
 |                   | state         +------------------+
 |                   |                 |              ^
 |                   |                 | --reap any   |
 |                   |                 | verified     |
 |                   |                 v groups       |
 |                   |                end     SIGKILL |
 |                   | no tick for   +------------------+
 |                   | 120s          | not responding   |
 |                   |-------------->| tick 120s or     |
 |                   |<--------------| older            |
 |                   | ticks again   +------------------+
 +-------------------+                 |
   |                                   | --cancel
   | finished, or                      | SIGTERM
   | --cancel SIGTERM                  |
   v                                   |
 +-------------------+                 |
 | still exiting     |<----------------+
 | terminal state,   |
 | lock held         |
 +-------------------+
   |
   | lock free, identity gone
   v
 +-------------------+
 | ended             |
 | done, interrupted |--> end
 | or aborted        |
 +-------------------+
```

## Replay, evidence and result semantics

Every transient retry and every stale-resume recovery passes the replay-safety
gate. The scanner checks item events against a safe-item allowlist (messages,
reasoning, error notices) and marks undecodable lines unsafe; other top-level event
types are accepted. The exec JSONL omits some tool calls (a sandbox-denied code-mode
command emits no item), so only a read-only role with external tools denied, whose
unseen calls leave no effects, can be replayed; any other role's failed attempt is
replay-blocked. The failure verdict names a transient class; replay safety and the
attempt budget decide the retry. There are at most two logical attempts, separated
by a five-second backoff, and a replay-safe stale resume can additionally restart
fresh within an attempt. The old thread and partial artifacts remain available for
deliberate recovery.

```
 Logical attempt n of 2 <-----------------------------+
   |                                                  |
   v                                                  |
 codex exec, fresh or resume,                         |
 in the role's scratch cwd                            |
   |                                                  |
   v                                                  |
 Outcome <------------------------------------+       |
   |                                          |       |
   +-- turn completed with a reply            |       |
   |   --> Role ok; thread saved              |       |
   |                                          |       |
   +-- auth, quota, model rejected,           |       |
   |   or exit 0 without a reply              |       |
   |   --> Terminal failure                   |       |
   |                                          |       |
   +-- saved thread missing --> Replay-safe?  |       |
   |     yes --> Fresh run within this        |       |
   |             attempt, continuity-lost ----+       |
   |             warning                              |
   |     no  --> replay-blocked:stale                 |
   |                                                  |
   '-- 429/5xx or a stall --> Replay-safe?            |
         no  --> replay-blocked or stall:             |
                 inspect partial effects              |
         yes --> Attempt left?                        |
                   yes --> Wait 5 seconds ------------+
                   no  --> Final transient error
```

Replay-safe means a read-only role with external tools denied whose attempt showed
no side-effect-capable item and no unreadable output. A stall whose turn had already
completed with a reply keeps that reply with a warning.

Raw protocol stdout and stderr are written per attempt as the worker runs.
Finalized attempt metadata records argv, pid/pgid, prompt hash, transport outcome,
protocol-reported commands, usage, hashes and gaps; process-start identity lives in
the supervisor and status records. A loss of raw storage or unreadable protocol is not disguised
as complete evidence. Interrupted attempts retain their records, and settled roles'
contributions survive cancellation. The exec reader buffers its streams in memory;
raw spooling alone does not make total runner memory bounded.

The result envelope separates runner observations, Codex-reported protocol data,
and collaborator interpretation. It includes exact input/context identity, baseline
integrity, required contribution coverage and current applicability. Text patches
are convenience artifacts; exact bytes remain in scratch. A symlink or special file
a role leaves is listed by path and kind, never followed or diffed. No artifact is
applied, committed or published automatically. Reconciliation checks evidence and
current requirements, not vote counts or a clean transport exit.

Exit 0 requires a delivered report, at least one successful role, and complete
required contributions, each with an observed completed turn.
A failed optional role does not make a completed required contribution disappear.
Stale captured inputs are a separate applicability verdict, not a retroactive claim
that the old experiment never happened. --freshness covers only the capture:
selected file bytes and executable flags, empty directories, Git HEAD, the index
entries of selected paths, the recorded omission list and the staged context.
Staging an unrelated file leaves a capture fresh; a new commit does not. Claude
checks changed intent and external conditions separately.

## Module boundaries

| Module | Owns |
|---|---|
| codex_council | CLI, supervisor/fan-out/continuity, runner observation wiring. |
| council_common | Diagnostics, private-path policy, atomic writes, strict JSON and shared primitives. |
| council_discovery | Bounded metadata transport, catalog/config normalization and snapshots. |
| council_selection | Role/selection data, pure model resolver and authoring validation. |
| council_failures | Structured failure classification and recovery wording. |
| council_liveness | Status/identity/lock handling, follow/status/cancel/reap. |
| council_workspace | Read-only source capture, independent copies, integrity and freshness. |
| council_policy | Capability validation and fail-closed effective-config observation. |
| council_evidence | Raw attempts, protocol summaries, candidate artifacts and result envelope. |
| council_workflow | Versioned invocation contracts, bindings and per-role discovery orchestration. |

```
 codex_council (CLI and supervisor)
   +-> council_workflow
   |     +-> council_policy
   |     |     +-> council_discovery -> council_common
   |     |     '-> council_workspace
   |     +-> council_selection
   |     |     +-> council_discovery
   |     |     '-> council_common
   |     +-> council_workspace
   |     '-> council_discovery
   +-> council_liveness -> council_common
   +-> council_failures -> council_common
   +-> council_evidence -> council_workspace
   '-> council_common
```

Each arrow is an import, and all of these modules run inside the runner process.
The entrypoint also imports the discovery, selection, policy and workspace modules
directly; those edges are left out above for legibility. The workflow-to-discovery,
workflow-to-selection and policy-to-discovery edges are lazy, function-level
imports.

Imports form a DAG; production siblings do not import the entrypoint. The runner
owns mutable run state. Runtime has no third-party Python dependency. Tests and
project documentation are not installed as skill prompt content.

## Verification and release gate

Unit tests exercise strict contracts, copies, links, freshness, policy observations
and evidence gaps. CLI tests use real subprocesses and a fake Codex protocol for
red/green experiments, task-scoped fresh/resume, transient retry safety, required
coverage, actual-cwd routing and cancellation. The liveness scenarios run in the
full suite. CI runs it on Ubuntu with Python 3.12 to 3.15 and on macOS with 3.13.

The fake does not implement Codex sandbox security or evaluate model quality.
Opt-in signed-in host tests check real inside/outside write and network denials.
`tests/fixtures/invocation_cases.json` holds manual or host-evaluation scenarios for
explicit invocation and context fidelity; CI validates only its structure. Record
the actual host model, Claude Code version, Codex version and effective
configuration. Unknown or failed gates remain explicit; do not merge automatically
on fake-suite success.

Documentation tests validate executable command/schema contracts and packaging,
plus a few literal skill instructions. Markdown is the only source, with diagrams
as plain-text drawings at most 60 columns wide. [README Testing](README.md#testing)
gives the commands.

Non-goals are automatic invocation, automatic patch integration, a second agent
loop replacing Codex exec, and a generic cluster scheduler.

## Supported versions

See the [README requirements](README.md#requirements). The skill and runner share
contract epoch 5.
