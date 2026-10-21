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

Four identities remain distinct: logical source project, task/investigation,
captured inputs, and optional Codex thread. The source root and task id join the
existing host-session and role key for continuity. A new scratch directory does
not accidentally create a new logical project. Fresh is the prepared default;
resume is explicit and history is subordinate to the complete current brief.

The staged request, context and role capability/lens contracts are frozen by
preparation. Model/effort/selection remain fillable after discovery. Context changes,
new requirements or a different contribution require recapture rather than quietly
reusing stale input identity. Hashes bind artifacts, not semantic correctness.

## Capture backend

The initial backend is independent copies for Git and non-Git projects. Git is a
read-only filename/index/HEAD source, never an instruction to verify only committed
HEAD. Current tracked and selected untracked bytes, deletions and executable bits
are captured. Before/after inventories and file identity stamps reject observed
mutation. This is not an atomic filesystem snapshot: coordinate overlapping writers
briefly and report limitations around uncooperative external writers.

Copies do not share writable inodes or Git control directories with the live tree.
Selected symbolic links, special files and non-portable name collisions are refused.
Common credential, agent-config and cache paths are omitted and recorded. This
reduces accidental exposure but is not a general secret scanner. ACLs, xattrs,
submodules, sparse layout, remote data and dependency environments are not reproduced.

The frozen baseline is read-only to catch mistakes, not a same-uid trust boundary.
Each role gets a separate writable copy and runs tests/experiments only according
to its explicit capabilities. Missing Git metadata or dependencies can block a
baseline; blocked setup is not evidence of an implementation defect. Worktrees are
not required for isolation here, and Codex's worktree/resume incompatibility is
avoided rather than hidden. A future backend must preserve the same invariants.

## Execution policy

Role names do not grant capabilities. Read-only is default; workspace-write enables
experimental files in that role's scratch cwd. Network, native integrations and
bypass require an authorization declaration. Bypass must acknowledge that network
denial cannot be enforced by that sandbox mode. Native tools retain their own
privileges, independent of command-network policy.

The adapter reads actual-cwd configuration, enumerates MCP names, then reads again
with requested restrictions. It removes extra writable roots and implicit temporary
write grants, sets non-interactive approval, disables supported external-tool
features and explicitly disables the discovered MCP servers. Wrong/unknown types,
managed overrides or an altered inventory block execution. Preflight reports the
policy and launch reobserves it; --start's local revalidation does not repeat slow
metadata operations before detaching. Prepared setup publishes liveness between
bounded metadata operations so long panels do not look like silent dead runners.

There is a remaining check-to-exec window. Config observations are not a serving
process attestation, and the command sandbox is not a sandbox of the entire Codex
process or of all host reads. An authorization string supplied by Claude is not an
independent authorization authority. A live-host denial suite is a release gate.
Failure never silently grants bypass or edits user configuration.

## Model discovery and selection

Discovery and execution use each role's actual scratch cwd and the same Codex
binary/environment. Relative CODEX_HOME is normalized before relocation, without
copying credentials. Source project agent configuration is deliberately not copied;
source-layer defaults may differ from scratch-native defaults and preflight says so.
No silent claim of source configuration parity is made.

The existing account/config/managed-requirements/catalog adapter, strict value
grammar, authoring validation, launch-time revalidation, explicit pins, native
fallback and per-role provenance remain. Each role owns its planning snapshot;
launch observations are retained separately. No thread or turn starts during model
or policy discovery. Nothing ranks ids, versions, recommended markers, or reputation.
Report values describe what was sent, not an attestation of what served a turn.

## Execution and lifecycle

The existing POSIX supervisor remains authoritative. --start exclusively claims a
private directory, passes its held lock into a new session, and returns once the
child publishes its record. Only the supervisor holds that lock; Codex children do
not inherit it. Both lock identity/token and process-start identity are checked.
One-use directories, conservative unknown states and safe signal targeting remain.

Bounded fan-out and per-thread nonblocking locks prevent simultaneous turns in one
saved thread. Every child has its own process group. Byte-inactivity watchdogs,
bounded post-exit drain, descendant cleanup, signal latching, reply-first persistence,
and orphan reaping remain. The supervisor is not a reboot-persistent job scheduler.
Same-user tampering, reparented descendants and a SIGKILL before child recording are
not solved by a new workspace abstraction.

`preparing` is an activity signal for metadata work, not worker dispatch. --follow
is read-only and notices lost status/identity. Terminal text is advisory: --status
confirms released supervisor lock and vanished recorded identity. If a present
runner stops responding, cancel it before reaping anything. Unknown means do not
signal. An unattended host must continue observing or cancel before returning.

## Replay, evidence and result semantics

All transient retries now pass the existing replay-safety scanner, not only stalls.
A 429/5xx after tool-capable or unreadable output is replay-blocked. The error class
can be transient without the whole task being safe to repeat. The old thread and
partial artifacts remain available for deliberate recovery. There is at most one
automatic retry with the existing five-second backoff.

Raw protocol stdout and stderr are written per attempt as the worker runs. Metadata
records argv, identity, prompt hash, transport outcome, protocol-reported commands,
usage, hashes and gaps. A loss of raw storage or unreadable protocol is not disguised
as complete evidence. Interrupted attempts retain their records, and settled roles'
contributions survive cancellation. The old exec reader still buffers its streams;
raw spooling alone does not make total runner memory bounded.

The result envelope separates runner observations, Codex-reported protocol data,
and collaborator interpretation. It includes exact input/context identity, baseline
integrity, required contribution coverage and current applicability. Text patches
are convenience artifacts; exact bytes remain in scratch. No artifact is applied,
committed or published automatically. Reconciliation checks evidence and current
requirements, not vote counts or a clean transport exit.

Prepared exit 0 requires a delivered report and complete required contributions.
A failed optional role does not make a completed required contribution disappear.
Stale captured inputs are a separate applicability verdict, not a retroactive claim
that the old experiment never happened. --freshness covers only selected inputs;
Claude checks changed intent and external conditions separately.

## Module boundaries

| Module | Owns |
|---|---|
| codex_council | CLI, existing supervisor/fan-out/continuity, runner observation wiring. |
| council_common | Existing private paths, atomic writes, diagnostics and shared primitives. |
| council_discovery | Bounded metadata transport, catalog/config normalization and snapshots. |
| council_selection | Role/selection data, pure model resolver and authoring validation. |
| council_failures | Existing structured failure classification and recovery wording. |
| council_liveness | Existing status/identity/lock handling, follow/status/cancel/reap. |
| council_workspace | Read-only source capture, independent copies, integrity and freshness. |
| council_policy | Capability validation and fail-closed effective-config observation. |
| council_evidence | Raw attempts, protocol summaries, candidate artifacts and result envelope. |
| council_workflow | Versioned invocation contracts, bindings and per-role discovery orchestration. |

Imports form a DAG; production siblings do not import the entrypoint. The runner
owns mutable run state. Runtime has no third-party Python dependency. Tests and
project documentation are not installed as skill prompt content.

## Verification and release gate

Unit tests exercise strict contracts, copies, links, freshness, policy observations
and evidence gaps. CLI tests use real subprocesses and a fake Codex protocol for
red/green experiments, task-scoped fresh/resume, transient retry safety, required
coverage, actual-cwd routing and cancellation. Existing liveness scenarios remain
in the full suite. CI adds macOS coverage alongside Ubuntu/Python versions.

The fake does not implement Codex sandbox security or evaluate model quality.
Opt-in signed-in host tests check real inside/outside write denials; behavioral
cases check selective invocation and multi-turn context fidelity. Record the actual
host model, Claude Code version, Codex version and effective configuration. Unknown
or failed gates remain explicit; do not merge automatically on fake-suite success.

Documentation tests validate executable command/schema contracts and packaging.
They do not freeze hundreds of prose phrases. The committed PDF is removed in
favor of Markdown sources and an optional generated release artifact.

Non-goals remain predefined roles, hardcoded model rankings, peer debate, automatic
per-turn invocation, automatic patch integration, and a second agent loop replacing
Codex exec. New experimental workspaces are a deliberate revision of the former
no-isolation policy, not a conversion into a generic cluster scheduler.
