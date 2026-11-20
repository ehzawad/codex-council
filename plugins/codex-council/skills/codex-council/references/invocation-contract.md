# Captured invocation contract

This is a versioned filesystem protocol, not a role catalog. Claude owns task
interpretation, invocation timing, panel composition, and reconciliation. The
runner owns paths, input identity, process lifetime, permissions sent, and records.

## Files before preparation

`request.json` describes the logical task and capture scope:

```json
{
  "schema": "codex-council/request@1",
  "task_id": "parser-boundary-investigation",
  "reason": "A reproduction can distinguish the two current explanations.",
  "source_root": "/absolute/project",
  "include": ["src", "tests", "pyproject.toml"],
  "exclude": ["tests/large-fixtures/*"]
}
```

Use a stable task id for a continuing investigation, not a turn number. A new
question/objective gets a new id. `task_id` must fully match
`[A-Za-z0-9][A-Za-z0-9._-]*`. `reason` explains the marginal value of this
invocation. It is not a runner-scored confidence or a model ranking.

`source_root` is an absolute directory; it and the run directory must be
separate, non-overlapping trees. A `--new-run` directory lives under
`$XDG_STATE_HOME` (by default `~/.local/state`), so `--prepare` refuses a
`source_root` such as `$HOME` that contains it and names the fix: a `source_root`
that does not contain it, or XDG_STATE_HOME set outside it (see
[runtime-behavior.md](runtime-behavior.md#run-directories)).

Include entries are normalized relative paths
without `..`, absolute names, backslashes, or traversed symbolic links.
With `include` omitted, Git supplies tracked and non-ignored untracked filenames;
the runner reads their **current working bytes**, including staged/unstaged edits
and deletions; a partially staged file is captured as its working version. Explicit
includes also keep empty directories. A non-Git project requires `include`.
`include: []` supports a pure question without filesystem inputs; it is refused when
any role is live or worktree, whose edits it could not report. Exclusions are
case-sensitive shell-style patterns matched against relative names, not validated as
paths. An omitted or `null` `exclude` means no exclusions. Implicit Git scope lists
files, so exclude a directory there as `dir/*`. Explicit includes may select ignored
fixtures.

Known credential, agent/VCS configuration, instruction and dependency-cache paths
met during selection are excluded and recorded in `run.json.snapshot.omitted`.
That list is neither a complete inventory of ignored paths (implicit Git scope never
lists them) nor a secret scan; sanitize unusual sensitive inputs yourself and
narrow the scope before capture. A selected symlink or special file is refused by
name, as are, with `include` omitted, a Git submodule (populated, empty or missing)
and an untracked nested Git repository. Each refusal names the path and the fix:
add it to `exclude` (it is then recorded in `snapshot.omitted`; a name with glob
characters such as `[id].js` is given as the escaped pattern), or select a
submodule's or nested repository's files with an explicit `include`. Excluding a
symlink also omits the tracked paths below it. None is silently flattened or
dropped. Collisions under Unicode canonical caseless matching
among selected file paths, empty directories and every directory component are
refused too. File bytes and an executable/non-executable flag are captured, not the
full permission mode; ownership, ACLs, extended attributes, sparse layout and
external services are not reproduced.

`roles.json` is a nonempty array. This is an example, not a predefined role:

```json
[
  {
    "id": "boundary-experiment",
    "label": "Boundary experiment",
    "instruction": [
      "Determine which explanation fits the captured parser behavior.",
      "Reproduce the baseline, try the smallest discriminating experiment, and retain evidence."
    ],
    "execution": {
      "sandbox": "workspace-write",
      "network": false,
      "external_tools": "deny",
      "continuity": "fresh",
      "required": true
    }
  }
]
```

Ids must fully match `[a-z0-9_-]+`, with no trailing newline; ids longer than 32
characters use a hashed key in file and directory names. Labels are single-line.
Instructions are arrays of nonempty strings. Optional fields are `model`, `effort`,
`selection`, `service_tier` and `execution`, described below. The runner supplies
common framing; Claude need not reproduce a literal phrase or closing sentence.
Unknown/duplicate JSON keys and non-finite numbers remain errors. Context is
nonempty UTF-8, never silently truncated. Preflight displays its byte count, not a
fabricated token bill.

## Capabilities and trust

`execution` is independent of the role's label or instructions:

| Field | Default | Meaning |
|---|---|---|
| sandbox | read-only | Command sandbox: read-only, workspace-write, or explicitly authorized bypass. |
| network | false | Command-network request. `true` requires workspace-write or bypass and an authorization; read-only requires false. Bypass cannot enforce false and requires true explicitly. |
| external_tools | deny | Disable apps/plugins/browser/computer/hooks/subagents/web, memories, the notify program, installer and remote-plugin features, and configured MCP servers, or explicitly authorize native integrations. |
| continuity | fresh | Resume only when this task's earlier thread is useful. |
| required | true | Missing/failed contribution or missing evidence makes required coverage incomplete. |
| authorization | absent | Required explanation of user permission for bypass/network/native external tools or a live or worktree workspace. |
| workspace | copy | `copy`: a private copy of the capture in the run directory. `worktree`: a Git worktree on its own new branch. `live`: the captured source_root itself. worktree and live need workspace-write or bypass and an authorization; a read-only role is copy only. |

Workspace-write permits writes to that role's cwd (its copy, its worktree, or the
live tree); additional writable roots and implicit /tmp/TMPDIR grants are removed.
Execution is non-interactive with approval_policy=never. Every sandboxed run passes
`--ignore-rules`, because a user or project execpolicy rule with `decision="allow"`
runs its command outside the sandbox. A copy role also gets
`project_root_markers=[]`: its copy lives in the run directory, so the copy is
Codex's project root and no AGENTS.md or `.codex` layer from a Git repository above
the run directory (a dotfiles home, say) reaches it. Live and worktree roles get no
markers override: Codex resolves the project root, AGENTS.md and project `.codex`
natively, as if the user ran codex there. Codex trusts a linked worktree like its
main repository, because project trust is keyed by the main repository root.

When external_tools=deny, memories are off, the examined features are disabled,
`agents.enabled=false` removes the subagent tools, web search is disabled, `notify`
is emptied (it runs a host program after each turn), and MCP names are enumerated
and disabled by bare name; a server name outside `[A-Za-z0-9_-]` cannot be
addressed by a Codex `-c` key and blocks the run. The disabled surfaces are the
default-on surfaces of codex-cli 0.170, not a promise about tools a later Codex
adds. In 0.170, `features.multi_agent=false` leaves the v2 subagent tools
(spawn_agent, send_message, wait_agent, followup_task, list_agents,
interrupt_agent) exposed; only `agents.enabled=false` removes them, and the policy
probe requires it, so an older CLI fails closed for restricted roles.
external_tools=native leaves Codex's own harness (memories, features, MCP, hooks,
plugins, notify, web search) as the user configured it. Effective configuration is
read again with the requested restrictions, and unknown/overridden settings (a
layer that keeps a copy role's project root markers, say), or managed requirements
that forbid them, block execution. Per role, `--discover`, launch discovery, the
policy probe, the service-tier check and the worker send the same overrides. No
automatic fallback to bypass exists.

**Full access** is `sandbox: bypass`, `network: true`, `external_tools: native` and
an `authorization`; it needs no other key. Codex then runs any command, uses the
network, uses live web search (Codex's default under full access), and uses its own
tools and subagents; a bypass role with external_tools deny is told instead that web
search, MCP tools and subagents stay disabled. The prompt tells every bypass role to
work autonomously (implement, verify its own result, and reconcile with other
writers sharing its tree before reporting), to take a local change as far as a
verified, reviewable result, and to stop and report blocked when a design decision
is unsettled. Use full access only when the user explicitly asks for it.

**Limits:** config/read is evidence of configuration, not an OS attestation of a
later worker. Configuration can change between checking and exec; an MCP inventory
change in that window fails the worker with a message saying so. The command
sandbox is not a sandbox around the entire Codex process, does not promise to
hide the rest of HOME, and cannot authenticate the user's permission from an
orchestrator-written `authorization` string. Native external tools have their own
privileges and may use network independently of the command sandbox. Bypass grants
unrestricted local capability. Run trusted inputs only, and validate actual denial
behavior with the opt-in live tests before relying on a particular host setup.

## Prepare, discover, then resolve

`--prepare RUNDIR` exclusively claims preparation, copies current inputs into an
owner-read-only baseline, binds each role's workspace and writes `run.json`. It
checks content and metadata before/after copying and refuses observed change. Pause
overlapping writers for this interval. This is **observed stable**, not an atomic
snapshot in the presence of arbitrary concurrent writers. A failed capture consumes
its run directory; never repair or reuse it, but allocate a new one with
`--new-run`. The capture is always taken; the workspace decides where a role runs.

**copy** (default): an independent writable copy for Git and non-Git projects, with
no shared Git metadata or hardlinks. Git-dependent build/version commands can fail
without .git; record this as an environment gap, not proof of a source-code bug.
Dependencies and external state need explicit, authorized setup and must be
identified in the evidence.

**worktree**: source_root must be inside a Git work tree whose HEAD is a commit;
otherwise `--prepare` refuses and suggests copy or live. The runner adds a Git
worktree at `workspaces/<key>`, detached at HEAD (the role's cwd is source_root's
subdirectory in it), running Git with user and system config, hooks and fsmonitor
disabled. It overlays the capture, so the role starts from what the user sees:
uncommitted edits, untracked selected files and deletions. Omitted paths (AGENTS.md
and other instruction files, `.codex`) stay as committed at HEAD; uncommitted
changes to them are not overlaid. Only after every role is prepared does it create
branch `codex-council/<run dir name>/<role key>`. An existing branch, or one that
conflicts in the ref namespace, is refused before capture; a prepare that fails
while binding roles removes the worktrees it added and creates no branch.
`--prepare` prints each worktree's path, branch and base. A bypass worktree role may
commit on its branch. A workspace-write one cannot (its worktree's `.git` is outside
the writable root), and its prompt says so.

**live**: the role's cwd is the captured source_root, so its edits land in the
user's working tree. There is no writer limit: any number of live and worktree roles
may share a run, the owner accepts collisions, and no lock spans runs. The prompt
tells a live role to preserve others' edits, re-read files before editing them, and
leave commits and pushes to the user unless they asked.

Each role's discovery runs in its actual execution cwd, with the policy probe's
overrides for that cwd, so `--discover` can exit 2 when a policy cannot be
established, like the preflight. For copy roles, source `.codex`, `.claude` and
`.agents` directories are intentionally not copied or flattened into global
settings, and agent instruction files (AGENTS.md, AGENTS.override.md, CLAUDE.md,
CLAUDE.local.md, at any depth, matched case-insensitively) are omitted even when an
include selects them and recorded in `snapshot.omitted`; the brief carries any
project rule a role needs. Native inheritance therefore means **the configuration
Codex resolves in the execution cwd**, which for a copy role may differ from the
live project's configuration. Live roles load the project's own AGENTS.md and
`.codex`; worktree roles load them as committed at HEAD. Inspect the discovery
summary; use a supported explicit pin when the user's intended model must be
preserved.
The host Claude session's settings and user Codex configuration are never edited.

Each role gets its own `model-snapshot.json` under the catalog path recorded in
`run.json`. Selection modes are: explicit user pin, routed model+effort,
native_effort, or omission for native inheritance; the shapes and value grammar
are in [panel-design.md](panel-design.md). Automatic choices name that role's
snapshot_id. Model and effort descriptions guide Claude; the runner validates
membership and provenance, never ranks.

`service_tier` is an optional role field, not a selection mode. Set it only on an
explicit user request (`"fast"` for fast mode); it takes the model/effort value
grammar and may stand alone. Preflight and launch check it per role against the
resolved model, or the proven native model when no model is sent, and refuse an
unadvertised or unprovable tier with exit 2 (rules in
[panel-design.md](panel-design.md)). It is sent as `-c service_tier="<value>"` on
every invocation, resume included, and recorded as the sent value: `service_tier=`
in the reply header, `sent_service_tier` in `result.json`. The launch's
effective-configuration check is retained as `evidence/<key>/service-tier.json`.

At launch each automatically selected role and each role with a `service_tier`
gets a fresh discovery in the same cwd, retained as
`evidence/<key>/launch-model-snapshot.json`. Reports describe values **sent**, not
an attestation of a serving model or tier.

Only model/effort/selection/service_tier may change after prepare. Changes to
context, task, panel, instructions or execution policy require a fresh
`--new-run` directory. The logical source root, task id and workspace mode (live
and worktree roles keep their own saved threads) scope saved threads, not the
workspace path. Session scope still follows CODEX_COUNCIL_SESSION_KEY, then
host ids, then project scope.

## Evidence and reconciliation

`result.json` schema `codex-council/result@1` identifies run, task, snapshot, context
hash, role completeness, baseline integrity, applicability, attempts and artifacts.
Each attempt streams exact stdout/stderr bytes to private files while running;
finalized metadata records argv, pid/pgid, transport outcome, observed command
events, usage when supplied, hashes and coverage gaps. Process-start identity is in
the supervisor and status records. Incomplete/oversized protocol records
can limit summaries without truncating raw files. The exec reader buffers its
protocol output in memory, so total runner RAM is not bounded.

Command/exit/output fields in JSONL are **Codex-reported observations**, not an
independent OS audit, and they can be incomplete: a sandbox-denied code-mode command
emits no `command_execution` item, although it may already have had effects.
Collaborator prose is stored separately as interpretation. No parser declares
substantive claims true. Required coverage and execution success are distinct from
applicability to current work and from verified task success.

The brief asks every role to end with one last line: `Status: ready`,
`Status: blocked: <the one question for the user>` or `Status: failed: <reason>`.
The runner parses it, tolerating markdown emphasis, into
`roles.<id>.interpretation.reported_status` = `{state, detail}`, or null when the
last line is not one. It is the collaborator's own claim, not verification. The
reply-file header gains `reported=<state>`.

Artifact manifests cover added, removed and changed non-omitted regular files,
executable-flag changes included; other permission changes and empty-directory
changes are not recorded. Live and worktree manifests use the capture's selection.
A non-omitted symlink, special file, nested Git repository, Git submodule or
unreadable entry a role leaves is listed by path with its kind (`symlink`,
`special-file`, `nested-git-repo`, `git-submodule`, `unreadable`) and the captured
`before` entry it replaced; it is never followed, read or diffed, and is not a
coverage gap. A file that changes or vanishes while it is inspected is flagged
`changed_during_inspection`, without a patch. Small text changes also produce a
human-inspectable diff; binary, large and executable-flag-only changes retain
metadata and exact bytes in the workspace. Treat all artifacts as untrusted.
Frozen-directory modes and digests detect mistakes, not malicious same-user/bypass
tampering. Per workspace:

- **copy**: `evidence/<key>/candidate.diff`; the exact bytes stay in the role's
  copy. Nothing applies patches to the live source.
- **worktree**: `evidence/<key>/candidate.diff` plus `worktree` = `{path, branch,
  base, repo, head, checked_out, commit_count, commits}` and
  `applied_to_live_tree: false`. Nothing is applied to the user's tree; Claude
  merges or applies what it accepts.
- **live**: result.json gains a top-level `live` section: one shared
  `evidence/live.diff` for the run, its `files`, `writers` (every live role id),
  `changed_before_launch` (in-scope paths that already differed from the capture
  at launch), `applied_to_live_tree: true`, and an `attribution` note that changes
  are not attributed per role. Each live role's `artifacts` only point at that
  shared diff. When the tree cannot be inspected, `live` is null and each live
  role records `artifact-inspection-incomplete`.

`--freshness RUNDIR` returns 0 for unchanged captured scope, 1 for stale/unknown,
and 2 for an invalid contract. It checks selected file bytes and executable flags,
empty directories, Git HEAD and the index entries of selected paths, the recorded
omission list, and the staged context; restaging an unrelated, excluded or omitted
file does not make a capture stale (adding an excluded path to the index, or
removing one, changes the omission list, which does). A changed staged context adds
its reason and keeps the file-level detail (changed paths, Git and omission flags),
as `result.json` applicability does; a moved or deleted source root reports unknown
(exit 1), not an invalid contract. After live edits it reports stale by design. It
does not check omitted file contents, changed
user intent, or unrecorded external dependencies. Claude must compare the current
goal as well. A failed baseline due to unavailable dependencies is a blocked check,
not a discovered bug.
