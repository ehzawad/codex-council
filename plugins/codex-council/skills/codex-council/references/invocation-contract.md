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
separate, non-overlapping trees. Include entries are normalized relative paths
without `..`, absolute names, backslashes, or traversed symbolic links.
With `include` omitted, Git supplies tracked and non-ignored untracked filenames;
the runner reads their **current working bytes**, including staged/unstaged edits
and deletions; a partially staged file is captured as its working version. Explicit
includes also keep empty directories. A non-Git project requires `include`. `include: []` supports a
pure question without filesystem inputs. Exclusions are case-sensitive
shell-style patterns matched against relative names, not validated as paths. An
omitted or `null` `exclude` means no exclusions. Implicit Git scope lists files, so
exclude a directory there as `dir/*`. Explicit includes may select ignored fixtures.

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
Instructions are arrays of nonempty strings. The runner supplies common framing;
Claude need not reproduce a literal phrase or closing sentence. Unknown/duplicate
JSON keys and non-finite numbers remain errors. Context is nonempty UTF-8, never
silently truncated. Preflight displays its byte count, not a fabricated token bill.

## Capabilities and trust

`execution` is independent of the role's label or instructions:

| Field | Default | Meaning |
|---|---|---|
| sandbox | read-only | Command sandbox: read-only, workspace-write, or explicitly authorized bypass. |
| network | false | Command-network request. `true` requires workspace-write or bypass and an authorization; read-only requires false. Bypass cannot enforce false and requires true explicitly. |
| external_tools | deny | Disable apps/plugins/browser/computer/hooks/subagents/web, the notify program, installer and remote-plugin features, and configured MCP servers, or explicitly authorize native integrations. |
| continuity | fresh | Resume only when this task's earlier thread is useful. |
| required | true | Missing/failed contribution or missing evidence makes required coverage incomplete. |
| authorization | absent | Required explanation of user permission for bypass/network/native external tools. |

Workspace-write permits experimental writes to that role's scratch cwd; additional
writable roots and implicit /tmp/TMPDIR grants are removed. Execution is
non-interactive with approval_policy=never. Every sandboxed run passes
`--ignore-rules`, because a user or project execpolicy rule with `decision="allow"`
runs its command outside the sandbox. Memories are off for every policy. When
external_tools=deny, `notify` is emptied (it runs a host program after each turn)
and MCP names are enumerated and disabled by bare name; a server name outside
`[A-Za-z0-9_-]` cannot be addressed by a Codex `-c` key and blocks the run. The
disabled features are the default-on surfaces of codex-cli 0.163, not a promise
about tools a later Codex adds. Effective configuration is read again with
the requested restrictions, and unknown/overridden settings, or managed
requirements that forbid them, block execution. No automatic fallback to bypass
exists.

**Limits:** config/read is evidence of configuration, not an OS attestation of a
later worker. Configuration can change between checking and exec. The command
sandbox is not a sandbox around the entire Codex process, does not promise to
hide the rest of HOME, and cannot authenticate the user's permission from an
orchestrator-written `authorization` string. Native external tools have their own
privileges and may use network independently of the command sandbox. Bypass grants
unrestricted local capability. Run trusted inputs only, and validate actual denial
behavior with the opt-in live tests before relying on a particular host setup.

## Prepare, discover, then resolve

`--prepare RUNDIR` exclusively claims preparation, copies current inputs into an
owner-read-only baseline, creates independent writable per-role copies and writes
`run.json`. It checks content and metadata before/after copying and refuses observed
change. Pause overlapping writers for this interval. This is **observed stable**,
not an atomic snapshot in the presence of arbitrary concurrent writers. A failed
capture consumes its run directory; never repair/reuse it.

The backend is copy-based for Git and non-Git projects. No shared Git metadata,
hardlinks, automatic commits, or worktree/resume incompatibility. A clean-HEAD
worktree alone would omit precisely the dirty edits under investigation. Git-dependent
build/version commands can fail without .git; record this as an environment gap,
not proof of a source-code bug. Dependencies and external state need explicit,
authorized setup and must be identified in the evidence.

Each role's discovery runs in its actual scratch cwd. Source `.codex`, `.claude` and
`.agents` directories are intentionally not copied or flattened into global settings,
and agent instruction files (AGENTS.md, AGENTS.override.md, CLAUDE.md, CLAUDE.local.md,
at any depth, matched case-insensitively) are omitted even when an include selects
them and recorded in `snapshot.omitted`; the brief carries any project rule a role
needs. Native inheritance
therefore means **the configuration Codex resolves in the execution cwd**, which
may differ from the live project's configuration. Inspect the discovery summary;
use a supported explicit pin when the user's intended model must be preserved.
The host Claude session's settings and user Codex configuration are never edited.

Each role gets its own `model-snapshot.json` under the catalog path recorded in
`run.json`. Selection modes are: explicit user pin, routed model+effort,
native_effort, or omission for native inheritance; the shapes and value grammar
are in [panel-design.md](panel-design.md). Automatic choices name that role's
snapshot_id. Model and effort descriptions guide Claude; the runner validates
membership and provenance, never ranks. At launch each automatically selected role
and each role with an explicit service-tier request
gets a fresh discovery in the same cwd, retained as
`evidence/<key>/launch-model-snapshot.json`.
Reports describe values **sent**, not an attestation of a serving model.

Only model/effort/selection/service_tier may change after prepare. Changes to context, task,
panel, instructions or execution policy require a fresh run directory. The logical
source root and task id scope saved threads, not the scratch path. Session scope
still follows CODEX_COUNCIL_SESSION_KEY, then host ids, then project scope.

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
emits no `command_execution` item, although it may already have had effects. Collaborator prose is stored separately as interpretation.
No parser declares substantive claims true. Required coverage and execution success
are distinct from applicability to current work and from verified task success.

Artifact manifests cover added, removed and changed non-omitted regular files,
executable-flag changes included; other permission changes and empty-directory
changes are not recorded. A non-omitted symlink or special file a role leaves is
listed by path with kind `symlink` or `special-file` (and the captured `before`
entry when it replaced a captured file); it is never followed, read or diffed, and
is not a coverage gap. Small text changes also produce a human-inspectable
`candidate.diff`; binary, large and executable-flag-only changes retain metadata
and exact bytes in scratch. Nothing applies patches to the live source. Treat all
artifacts as untrusted. Frozen-directory modes and digests detect mistakes, not
malicious same-user/bypass tampering.

`--freshness RUNDIR` returns 0 for unchanged captured scope, 1 for stale/unknown,
and 2 for an invalid contract. It checks selected file bytes and executable flags,
empty directories, Git HEAD and the index entries of selected paths, the recorded
omission list, and the staged context; restaging an unrelated, excluded or omitted
file does not make a capture stale (adding an excluded path to the index, or
removing one, changes the omission list, which does). A changed staged context adds
its reason and keeps the file-level detail (changed paths, Git and omission flags),
as `result.json` applicability does; a moved or deleted source root reports unknown
(exit 1), not an invalid contract. It does not check omitted file contents, changed
user intent, or unrecorded external dependencies. Claude must compare the current
goal as well. A failed baseline due to unavailable dependencies is a blocked check,
not a discovered bug.
