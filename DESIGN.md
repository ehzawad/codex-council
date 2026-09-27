# codex-council internals

Implementation details for contributors. User-facing docs live in
[README.md](README.md).

## No catalog, no defaults

The script accepts roles **only** via `--roles-file` (a path to a JSON
file holding the list of `{id, label, instruction}` objects, each with
optional `model`, `effort`, and `selection`), and the
preferred launch path supplies context via `--context-file` in the same
private staging directory. `instruction` is a **list of sentence-sized
strings** — the only accepted form — that the script
whitespace-normalizes and joins into the single paragraph Codex sees.
The list form exists because the
only production writer of roles.json is an LLM file-Write: multi-KB
single-line JSON string literals are where its writes corrupt.
For the same reason, unknown keys in a role object are **rejected**
with a rewrite-the-whole-file message — stray filler fields like
`"_": ""` are the signature of a glitched write, not harmless extras —
and so is a key repeated at any object level, which would silently hide
one value behind another.
Every roles-file validation defect carries the same uniform recovery:
rewrite the entire file in one complete Write operation, never patch a
substring of a file that already glitched once.
The staging-dir gate (`--check-staging-dir`) lstats the directory:
symlinks, non-dirs, foreign-owned dirs, and group/other-accessible
modes are all rejected with an action-first recovery hint that forbids
chmod/mkdir/reuse of the rejected path and demands a fresh `mktemp -d`
(a recovery hint satisfiable by chmod/mkdir on the same predictable
path would defeat the privacy the gate exists for). The **launch path
re-validates the same privacy contract**: each on-disk input's lexical
parent directory (`dirname(abspath(...))`, never realpath-first, so a
symlink parent cannot launder into its target) must pass the identical
private-dir check before any content is read, in both staged and
stdin-context modes. The staged input files themselves must
be regular non-symlinks, preventing a private directory from redirecting
validation or reads to an external path. Keeping the panel and context in files
also keeps a large role array and multiline context out of the shell, where a
stray quote, brace, or missing redirection target would otherwise break the
call before the runner can diagnose it. There is no
built-in role catalog, no positional shortcuts, and no `--list-roles`
flag. Bare invocation (no `--roles-file`, with context piped or staged)
exits 2 — the script's way of telling Claude to go compose a panel.

The orchestrator deliberately has no plugin-imposed content-size or panel-count
ceiling: role count, role IDs, labels, instructions, staged context, stdin, and
composed prompts are accepted without truncation. Active role concurrency is
bounded separately, so a large panel queues instead of spawning every process
at once. The active model/provider and available memory are still real
downstream constraints; their failures are surfaced rather than guessed at by
an arbitrary content cap.

The reasoning: every hardcoded catalog is a bias. A fixed set of coding
roles biases Claude toward coding panels, and even a broad thematic
shelf still biases Claude toward "pick-from-this-shelf" rather than
"compose-from-context." Leaving the catalog out entirely makes Claude (the orchestrator) read the
user's task, design role ids/labels/instructions on the fly, announce the
composed panel, and then fan out. The
script's job is fan-out, retry, and aggregation — Claude owns reconciliation
into the user's shared goal rather than relaying disconnected role opinions.
The same reasoning applies to models: the runner carries no model roster and
no effort table, because every such list goes stale with the next model
generation, subscription change, or provider. Capability comes from runtime
discovery, and the fallback is always the user's own native configuration
(see [Model selection architecture](#model-selection-architecture)).

Context-derived roles do not mean context-light roles. Before panel synthesis,
Claude reconstructs the user's live task model: the larger problem and project
implementation, current trajectory, in-flight files/modules/tests/artifacts,
bugs and errors under investigation, hypotheses and research evidence, known
unknowns and plausible blind spots, and unstated or possibly wrong assumptions.
Claude asks and answers those working questions from the conversation and live
workspace, asking the user only when a missing choice materially changes the
authorized outcome.

Practical consequence: every invocation requires Claude to compose
the full role JSON. That's more tokens per panel proposal, but it
matches the actual design intent (adaptive in-context selection) and
removes any pull toward formulaic coding-flavored panels.

The intended product shape is an adaptive, general-purpose verification and
collaboration pattern. Claude identifies the claims, decisions, and results
worth checking, derives roles from the current goal, and orchestrates them
through shared context, workspace access, persisted role threads, and final
reconciliation; Claude stays responsible for the result.
The same machinery can support implementation, diagnosis, creation, planning,
research, review, or other domains as far as the active model and tools allow.
It deliberately leans toward programmatic problem-solving—computer science,
software and ML/AI engineering, DevSecOps, platform/security automation,
debugging/testing, project implementation, and evidence-based technical
research—without reinstating a domain catalog or fixed role shelf.

There is also no default role count. The runner accepts a panel of one, and
the skill sizes panels to the work: one role for a focused bug, review, or
question; more only when each added role brings a lens the others would not.
Every role costs a full Codex run and reconciliation effort, and the council
waits on its slowest role, so over-composed panels are slower without being
better. The collaboration brief the runner adds to each prompt is
count-neutral for the same reason ("you may be the only role, or one of
several").

Per-role `model`, `effort`, and `selection` keys are described in
[Model selection architecture](#model-selection-architecture). In short, the
runner validates their shape with one grammar, checks automatic choices only
against this run's discovered data, and passes what it decides to send as
parent `codex exec` options, `-m <model>` and
`-c model_reasoning_effort="<effort>"`, ahead of `resume` so fresh and resumed
attempts carry the same values. A role that omits all three inherits Codex's
native configuration in the worker's execution context, and its commands are
byte-identical to those of earlier releases.

Codex itself has a stable in-process `multi_agent` capability (on by
default) and a `multi_agent_v2` feature (stable but off by default, as of
codex-cli 0.156.1). The council deliberately
uses external `codex exec` fan-out because each role needs an independently
persisted thread id and process-level failure/cancellation isolation. A
positive user-level `agents.max_threads` is still used as a conservative
concurrency signal; it is not treated as proof of provider capacity because
these are separate processes.

A single fan-out is parallel contribution, not direct peer messaging. Claude
mediates collaboration by giving every role the same situational context,
reconciling the report, and staging material findings into selective follow-up
rounds. Because all subprocesses share the working directory, implementation
panels with several roles assign one write-owning role; multiple writers must
use serialized phases (the runner does not yet use Codex's `--worktree`). This
also limits duplicate side effects when a transient failure causes a role
retry.

## End-to-end fan-out

```mermaid
sequenceDiagram
    participant C as Claude Code
    participant F as --follow (Monitor)
    participant T as codex_council.py
    participant D as codex app-server
    participant R as per-subprocess readers + watchdog
    participant A as codex exec (role A)
    participant B as codex exec (role B)
    participant N as codex exec (role N)

    C->>T: run_in_background: --context-file + --roles-file
    C->>F: Monitor: --follow RUNDIR
    T->>T: launch privacy gate on each input's lexical parent
    T->>T: parse roles, authoring check against RUNDIR/model-snapshot.json
    opt routing auto and a routed or native_effort role
        T->>D: one metadata-only launch discovery (kept in memory)
        D-->>T: launch snapshot, frozen for this council
    end
    T->>T: _resolve_selection per role, attach each SelectionDecision
    T-->>F: dispatch line, model selection line, fallback reasons
    par up to effective max_parallel
        T->>A: role framing + collaboration brief + shared context (only the dispatch -m / effort)
        T->>B: role framing + collaboration brief + shared context (only the dispatch -m / effort)
        T->>N: role framing + collaboration brief + shared context (only the dispatch -m / effort)
    end
    A-->>R: stdout/stderr chunks reset the shared quiet clock
    B-->>R: stdout/stderr chunks reset the shared quiet clock
    N-->>R: stdout/stderr chunks reset the shared quiet clock
    opt quiet reaches CODEX_COUNCIL_STALL_SECS
        R->>A: SIGTERM then SIGKILL the stalled attempt
        R-->>T: stall verdict + replay-safety flags
        T->>T: stall policy - ok-with-warning, retriable, or terminal
    end
    R-->>T: buffered JSONL events per role
    T->>T: per-role extract_final_message, or classify the failure
    loop as each role settles
        T->>T: write replies/role-id.md, then log K/N completion with reply=path
        T-->>F: err.log line
        F-->>C: event (reply= paths outside replies/ dropped)
        C-->>C: read reply as untrusted data, act on independent work
    end
    T-->>C: aggregated markdown report in out.md
    T-->>F: CODEX_COUNCIL_DONE (progress signal, follower exits 0)
    T-->>C: background-task completion notification
    C-->>C: check evidence and reconcile across roles
```

## Context working set

Claude, not the Python runner, decides what conversation context to stage. For
long host sessions it constructs a decision-complete working set that leads
with the objective, the acceptance criteria, and the question to verify, and
labels Claude's own conclusions as claims, with the evidence against them.
The rest is the current problem/project and goal-directed or exploratory
trajectory; in-flight modules, files, objects, drafts, queries, experiments,
tests, deployments, and research; active bugs/errors, symptoms, attempted
fixes, hypotheses, and evidence; recent working context at high fidelity; live
primary evidence from disk; known unknowns, blind spots, assumptions, and
provenance; and older still-relevant decisions, invariants, and rejected
approaches as a faithful summary.
Conversation age alone never controls inclusion. Superseded state, duplicate
discussion, and irrelevant history are omitted. A compacted host summary is an
index that must be reconciled with current live state before launch.

The runner accepts that staged context without a byte cap or truncation. It
does not attempt token counting because the active model/provider owns the real
context window and can change independently of this plugin. `_compose_prompt`
keeps the shared context intact, labels it, adds a compact collaboration brief
that tells each role how to interpret the situational map, and bookends it with
the role-specific instruction.

The brief frames each role as an independent cross-model check on Claude
Code's work. The user's goal, requirements, and constraints are authoritative.
Claude's account of the project state, its conclusions, and what has been
tried are claims to verify against the workspace, not facts to accept. The
brief also says the run is non-interactive, so assumptions are stated and
decisions that need the user become open questions. It tells roles not to
spawn subagents unless their instruction asks for them, to stay within their
lens and stop when its deliverable is complete, to keep verified evidence
separate from inference, to size testing to the change, and to finish with
plain paragraphs Claude can reconcile.

## Adaptive concurrency and progress

Panels have no count cap, but `run_council` wraps role execution in an
`asyncio.Semaphore`. The active limit resolves in this order:

1. positive `CODEX_COUNCIL_MAX_PARALLEL` override;
2. positive user-level Codex `agents.max_threads` from
   `$CODEX_HOME/config.toml` (or `~/.codex/config.toml`); current Codex
   documentation lists it as a legacy alias of
   `agents.max_concurrent_threads_per_session`, which the runner does not
   read;
3. `DEFAULT_MAX_PARALLEL=6`, the default Codex documented for
   `agents.max_threads` when the runner adopted it (current documentation
   leaves the default to Codex).

Each queued role makes a nonblocking continuity-lock probe while it briefly
holds a subprocess permit. If another council owns the same persisted thread,
the probe closes its file descriptor, releases the permit immediately, sleeps
outside the permit (doubling from 0.1s to a 2s cap, since the lock holder has
no run-level deadline), and retries; unrelated roles can run, and arbitrarily
large panels do not accumulate one open lock file per queued role. Only a role that
holds both its continuity lock and permit appears active or launches Codex.

Progress is stderr-only and best-effort: one shared diagnostics helper writes
the dispatch line, the model selection summary and fallback lines that follow
it, per-attempt start lines
(`<role>: started (fresh|resume) attempt=K/N watchdog=…`), retry/adoption
notices, stall diagnostics, the heartbeat, and the `CODEX_COUNCIL_DONE`
sentinel. A dead stderr permanently redirects diagnostics to a no-op sink and
never changes role results or the exit code. The heartbeat cadence adapts to
the watchdog — `min(1800, stall_secs // 3)` with a 300s floor while enabled
(600s at the default threshold), 1800s when disabled — and each line records
completed/active/queued counts, per-active-role `quiet=Ns` (or `retry-wait`
during backoff), the `watchdog=` threshold, and the plugin `version=`.
Claude Code redirects it to `err.log`. Per-role completion lines keep the
`[codex-council] K/N <id>: ok|FAILED (<secs>s)` prefix and append
` reply=<abs path>` when the reply file was written.

## Per-role reply files and the follower

Before v0.10.0 no reply reached disk until every role finished, so Claude
waited on the slowest role and an interrupted run lost all finished work.
Now, as each role settles (ok, failed, or crashed), the runner writes
`<RUNDIR>/replies/<key>.md`, where `RUNDIR` is the directory of
`--context-file` (or of `--roles-file` in stdin mode), both already
privacy-checked at launch. `<key>` reuses the state-file role component: the
literal id when it is a short safe filename, a deterministic SHA-256 key
otherwise. The file body is produced by the same per-role section renderer
as `out.md`, so an early read and the final report cannot drift, preceded by
a one-line status header
(`<!-- codex-council reply id=… status=… elapsed=… attempts=… selection=… -->`,
plus the sent `model=`/`effort=` when a role sends them, the
`requested_model=`/`requested_effort=` of a fallback, and `warning=yes`).
Writes are atomic through the shared `_atomic_write_private` helper: an
`O_CREAT|O_EXCL|O_NOFOLLOW` 0600 temp file in the same directory, fsync, then
`os.replace`. `replies/` is created
0700; if it already exists it must be a real, user-owned, private directory,
otherwise the runner skips reply files with one warning rather than failing
the council. The file is written before the completion line is logged, so a
`reply=` path always points at a complete file, and files survive
SIGINT/SIGTERM even though the sentinel does not.

`--follow DIR` is a read-only companion for Claude Code's Monitor tool. It
validates `DIR` with the same private-dir check, polls `DIR/err.log`, prints
every `[codex-council` line with a flush per line, and exits 0 after the
`CODEX_COUNCIL_DONE` sentinel, an interruption line, or a `runner aborted`
line (the runner logs one when stdout is dead at report time or an unhandled
exception escapes). It exits 3 with a `[codex-council-follow] no council
activity` line when, 120s after it starts, `err.log` is absent or has no
dispatch line, so a monitor on a mistyped path or a launch that failed
validation does not sit silent. It exits 4 (`runner presumed gone`) when a
dispatched run's `err.log` stays byte-silent for 2 x `PROGRESS_HEARTBEAT_SECS`
+ 60s, measured from the file mtime so the check survives a re-arm; a
wall-clock jump well beyond the monotonic advance between polls is treated as
a suspend and restarts the count, because the runner's heartbeat sleeps on
the monotonic clock. Monitors expire after at most 30 minutes (10 in
`claude -p`) and are re-armed while the council's task is still running;
the restarted follower replays earlier lines, which Claude de-duplicates. The follower never writes, so it cannot forge the sentinel or
alter a run. Roles can, though: they run unsandboxed as the same user and can
append to `err.log`, and no same-uid check can authenticate those lines. So the
follower drops completion lines whose `reply=` path is not directly inside
`RUNDIR/replies/` (the only shape the runner prints), SKILL.md treats reply
content as untrusted data, and the final reconciliation waits for the
`run_in_background` completion notification, which only Claude Code emits. A
session-cron wake-up or the native task wait remains the fallback when
Monitor is unavailable.

Early replies change what Claude may do, not how the council ends: Claude
may read a settled role, tell the user, and act on independent work, but the
final verdict, cross-role conflicts, and writes that overlap a running writer
role wait for the full report. The runner has no partial-cancellation
feature.

The SKILL templates depend on `--follow` and reply files (epoch 2) and on
`--discover` and the `selection` contract (epoch 3), so the skill contract
epoch is 3. `--skill-contract` also marks the skill path for the selection
rules below.

## Model selection architecture

Model selection has three parts: an optional discovery adapter that turns the
installed Codex's metadata into a run-scoped snapshot, one pure resolver that
decides what each role sends, and the unchanged `codex exec` runner, which
sends only the resolver's dispatch values. A few rules shape all three:

- **Native configuration is the baseline and the universal fallback.**
  Inheritance is omission: no `-m`, no `-c model_reasoning_effort`, and never
  an `inherit` or `default` placeholder. Codex then resolves its own effective
  configuration in the worker's execution context — CLI overrides, trusted
  project `.codex/config.toml` layers, the user's `$CODEX_HOME/config.toml`,
  cloud, system, and managed layers, and any managed new-thread defaults.
- **Routing is the skill's default, and explicit pins always win.** Invoking
  the skill authorizes its documented routing policy;
  `CODEX_COUNCIL_MODEL_ROUTING=off` disables automatic selection. A pin the
  user asked for is forwarded unchanged and never replaced.
- **Claude chooses, the runner validates.** Claude walks a fallback ladder
  per role — a routed pair grounded in this run's snapshot, else the proven
  native model with only the effort adjusted, else inheritance of both — and
  matches roles to catalog descriptions. The runner never ranks models or
  efforts; every check is exact membership in discovered data.
- **Discovery is optional, bounded, run-scoped, and never an inference
  turn.** Any discovery failure means inheritance, never a blocked run.
- **Never claim a model ran.** `codex exec --json` reports neither the model
  nor the effort that served a turn, so reports describe what was sent.
- **Authoring defects and evidence changes are different.** A malformed or
  unsupported choice exits 2 with the whole-file rewrite recovery; evidence
  that stops supporting a valid choice between planning and launch resolves
  that role to inheritance with a recorded reason.
- **The host keeps its own settings.** Claude keeps the host session's model
  and effort (the skill's frontmatter pins neither); council routing controls
  only the external Codex workers.

### The discovery adapter

`--discover RUNDIR` validates RUNDIR with the same private-directory check as
preflight (prefix `--discover: `; `roles.json` and `context.md` need not
exist yet), reads `CODEX_COUNCIL_MODEL_ROUTING`, runs `_discover()`, writes
the snapshot, and prints a summary. It cannot be combined with
`--roles-file`, `--context-file`, `--check-staging-dir`, or `--follow`, and it
exits 0 whenever RUNDIR and `CODEX_COUNCIL_MODEL_ROUTING` are valid, even when
discovery is unavailable or `codex` is missing, because inheritance is always
a valid outcome.

The adapter speaks newline-delimited JSON-RPC to
`codex app-server --listen stdio://` and matches responses by id while
unsolicited messages interleave:

```mermaid
sequenceDiagram
    participant R as codex_council.py
    participant V as codex --version
    participant A as codex app-server

    Note over R,A: one monotonic deadline of 20s covers every step
    R->>V: version probe with its own 5s cap
    V-->>R: codex-cli version, or null (informational)
    R->>A: spawn with the runner's cwd and environment in a new process group
    R->>A: id 1 initialize (clientInfo codex-council, experimentalApi false)
    A-->>R: codexHome
    R->>A: initialized notification
    R->>A: id 2 account/read (refreshToken false)
    A-->>R: account type and requiresOpenaiAuth kept, identity never read
    R->>A: id 3 config/read (cwd is the project root, includeLayers false)
    A-->>R: model, effort, provider, and the layer kind of model and effort
    R->>A: id 4 configRequirements/read (params null)
    A-->>R: managed new-thread defaults and provider keys
    loop at most 10 pages or 1000 entries
        R->>A: id 5+k model/list (limit 100, includeHidden, exact cursor)
        A-->>R: data and nextCursor
    end
    opt server-to-client request at any point
        A->>R: a message with both method and id
        R-->>A: error -32601, discovery marked inconclusive
    end
    R->>A: close stdin, then SIGTERM and SIGKILL to the group if needed
```

Only those methods are ever sent — never `thread/start`, `thread/resume`,
`turn/start`, or any login or account-changing method — and the tests assert
that the fake server sees nothing else. Notifications and other unsolicited
messages are counted and dropped (more than 10,000 fails the session). A
server-to-client request is answered with JSON-RPC `-32601`, recorded as
`server_request:<method>`, and makes discovery inconclusive.

One monotonic deadline, `DISCOVERY_TIMEOUT_SECS = 20`, covers the
`codex --version` probe (itself capped at 5s; an unparsable version is
recorded, never fatal), the spawn, the handshake, and every request;
notifications never extend it. Lines are capped at 8 MiB and total stdout at
32 MiB. Pagination echoes the exact opaque cursor with a fresh id and stops at
10 pages or 1,000 entries; a repeated cursor is a cycle. Reaching a bound with
pages outstanding marks the catalog incomplete — it never implies an omitted
model is unavailable. stderr is drained concurrently and only a 4 KiB tail is
kept; when the server exits early, its last stderr line, sanitized,
email-redacted, and cut to 200 characters, is recorded as a problem.

Teardown runs in `finally`: close stdin, wait 0.5s for the whole process
group, SIGTERM the group, wait 0.5s, SIGKILL, reap. Neither a SIGTERM-ignoring
server nor a grandchild holding a pipe outlives discovery. The adapter is
synchronous (`selectors` on raw fds) and runs before `asyncio.run`, so it
never blocks the council's event loop; Ctrl+C during launch discovery still
tears the app-server group down and exits 130.

`_discover()` never raises (Ctrl+C aside). A missing `codex`, a spawn error, a
timeout, a protocol violation, an RPC error, or even an internal bug yields a
snapshot with status `unavailable` and machine-safe problem codes:
`codex_missing`, `spawn_failed:<errno>`, `timeout:<method>`,
`server_exited:<method>`, `protocol_error:<kind>`,
`rpc_error:<method>:<code>`, `schema_unsupported:<method>:<field>`,
`server_request:<method>`, `catalog_incomplete:<why>`, `catalog_conflict`,
and `internal_error:<type>`. `codex_version_unavailable` is informational.

The pure `_normalize_*` helpers are the only code that reads response wire
names; everything downstream reads the snapshot. They tolerate additive
fields and reject wrong types without coercing them: `hidden: "false"` or a boolean
`retirementAt` makes an entry unusable. The dispatch identity is an entry's
`model` field, which is what `-m` receives. The picker `id` is kept distinct
and recorded as `catalog_id`, because the two need not be equal. Effort values
are an open vocabulary (any non-empty string). A malformed entry, or a
duplicate `model` with conflicting content, makes that model unusable and the
catalog incomplete while the rest stays readable; identical duplicates
collapse. A malformed page (`data` not a list, a non-string cursor, a missing
`result`) makes discovery unavailable.

### Execution-context parity

Discovery has to describe the context workers actually run in, so both are
launched the same way:

- the `codex` that `PATH` resolves (`shutil.which`, recorded as an absolute
  path);
- the runner's own working directory and environment, with no override, so a
  relative `CODEX_HOME` resolves identically for both;
- no `--profile`.

Project config layers are selected by `config/read`'s `cwd` parameter, not by
the server's spawn directory. That was verified on codex-cli 0.157.1: the
spawn directory is irrelevant, and omitting `cwd` drops every project layer.
Discovery therefore passes `_project_root()`, the same root workers get as
`codex exec -C` (the Git top level of the launch directory, else the launch
directory). A `.codex/config.toml` below that root is not part of the
council's baseline. `initialize` reports the `codexHome` the server selected.

The runner forwards no profile, and the app-server refuses `--profile` anyway
("--profile only applies to runtime commands and `codex mcp`"), so
`context.profile` is always null. A profile selected in some other Codex
process never applies to the council. `CODEX_API_KEY` is recorded as presence
only: `codex exec` honors it but the app-server does not. When it is set, the
catalog may describe a different auth context than the workers', so neither
routing nor native-model effort adjustment is offered.

### The snapshot

`RUNDIR/model-snapshot.json` (schema `codex-council/model-snapshot@1`) is
written through `_atomic_write_private`, the reply files' pattern. If the
write fails, any older snapshot is removed and `--discover` prints only a
line telling Claude to write no automatic selections, because evidence from
an earlier discovery must never be read back as this run's.

The snapshot records:

- a random 16-hex `snapshot_id`, the UTC creation time, and the plugin
  version;
- `status` (`ok` or `unavailable`) and its `problems`;
- the execution `context`: project root, launch cwd, executable, CLI
  version, codex home, profile, and API-key presence;
- the `account` projection;
- the `configured` model, effort, and provider, with the kind of layer
  (`user`, `project`, `system`, `mdm`, ...) that supplied the model and the
  effort, but never a file path or layer contents;
- the `managed_defaults` observation (`present`, `absent`, or `unknown`);
- the `native` resolution and the `routing` verdict;
- the normalized `catalog`: each entry's dispatch `model`, `catalog_id`,
  display name, description, `hidden`, `recommended`, default effort,
  advertised efforts with their descriptions, and upgrade target with
  retirement time.

The account projection reads exactly two fields, `type` and
`requires_openai_auth`. Email, plan, account ids, workspace routing, and
tokens are never read, so they cannot reach the snapshot, stdout, or stderr;
the tests plant sentinel values in every one of those fields and assert they
never appear.

`_read_snapshot` accepts only the private regular file `--discover` writes.
lstat refuses a symlink, special file, foreign owner, or group/other mode
bits, and the open adds `O_NOFOLLOW|O_NONBLOCK`. The content must be strict
JSON (no duplicate keys, no `NaN` or `Infinity`, at most 64 MiB) that matches
the schema field by field, with unique catalog models and a proven native
model present in the catalog.

The `--discover` summary is what Claude reads to choose selections. It holds
the status line with the `snapshot_id`, the native configuration with its
origins and managed defaults, the routing verdict with every reason, the
native-effort verdict, each visible model with its JSON-quoted description,
advertised efforts, and recommended, retirement, and upgrade notes, and the
hidden models by name. Every line passes through `_report_inline`, because
catalog text is untrusted data.

### Eligibility and native proof

`_build_snapshot` decides both verdicts once, so the summary, preflight, and
launch read the same answers and reasons.

`status` is `ok` only when `initialize` and all four sources answered with a
valid shape and no server request arrived. `routing.eligible` requires all of
these, and each failed condition adds one human-readable reason:

- routing mode `auto`;
- status `ok`;
- a complete, well-formed catalog;
- a signed-in account. An unauthenticated app-server still lists models, so a
  catalog alone is not entitlement evidence;
- a corresponding provider: the configured provider is unset or `openai`,
  and no managed `modelProvider`, `modelProviders`, or `modelCatalogJson` is
  set. Probes showed a custom provider's `config/read` answer is correct
  while `model/list` still returns OpenAI's catalog;
- no `CODEX_API_KEY`;
- managed new-thread defaults `absent`.

`native.resolution` is `proven` only when status is `ok`, managed new-thread
defaults are `absent`, the provider corresponds, `CODEX_API_KEY` is unset, a
model is configured, and a well-formed catalog entry exists whose `model` is
exactly that configured model. A hidden entry counts. The entry is needed so
the model's advertised efforts are known. Otherwise the resolution is
`unknown`, with the reason. A configured model is never filled in from the
catalog's recommendation, and a missing effort is never filled in from its
default.

Managed new-thread defaults block both verdicts because of a documented
coupling. An explicit override of either the model or the reasoning effort
makes Codex ignore both managed defaults, so an effort-only override could
silently change the model the user expected to inherit. `native_effort` also
sends the proven native model with the effort (`-m <native model>`) rather
than the effort alone, so the effort always travels with the exact model it
was validated against. Neither verdict consults the catalog's `isDefault`
marker (recorded as `recommended` for display), catalog order, or the shape
of ids.

### The roles.json selection contract

Role objects take `id`, `label`, `instruction`, and optionally `model`,
`effort`, and `selection`. `selection` has one of three shapes:

- `{"mode": "user"}`, optionally with a single-line `reason`. It needs
  `model` and/or `effort` and forbids `snapshot_id`.
- `{"mode": "routed", "snapshot_id", "reason"}`, which needs both `model`
  and `effort`.
- `{"mode": "native_effort", "snapshot_id", "reason"}`, which needs `effort`
  and forbids `model`, because the runner pins the proven native model.

`snapshot_id` must be the 16-hex id `--discover` printed. `reason` is a
non-empty single line with no length cap. Inheritance is omission of all
three keys.

`--skill-contract` marks the skill path, where a `model` or `effort` without
`selection` exits 2. Direct CLI use without it reads such an untagged pin as
`{"mode": "user"}`, which keeps earlier role files working.

Both values share
`SELECTION_VALUE_PATTERN = ^[A-Za-z0-9][A-Za-z0-9._:/@+-]*\Z`: no leading `-`,
and no whitespace, control characters, quotes, backslashes, or angle
brackets. That makes every consumer safe by construction. `-m <model>` stays
one argv item. The TOML basic string in `-c model_reasoning_effort="<effort>"`
needs no escaping and cannot be broken out of. Report lines and reply-file
headers stay single-line. Case is preserved, never folded, and no list of
valid values exists anywhere in code. `inherit` and `default`, in any case,
are refused as model values, since Codex would receive them as literal model
ids. The roles file goes through a strict JSON loader that also rejects
`NaN` and `Infinity`.

### Authoring versus evidence validation

Authoring validation (`_validate_selection_authoring`) asks whether an
automatic choice is supported by this run's planning snapshot,
`RUNDIR/model-snapshot.json`, where RUNDIR is the directory of the staged
inputs. It runs in preflight and again at launch, before any worker. A
violation exits 2 with the rewrite recovery. The violations are:

- a snapshot that is absent, unreadable, malformed, or not the private file
  `--discover` writes;
- a `snapshot_id` that does not match;
- routing ineligible, for a routed pair;
- a model that is not an advertised execution id, with a hint naming the
  execution id when the value is some entry's picker id or display name;
- a hidden model, or one whose advertised retirement has passed;
- an effort not advertised for that exact model;
- an unproven native model, for `native_effort`.

User pins are never validated against the catalog, since a custom provider's
models are not in it; they only collect advisories. With routing off,
automatic roles are not errors.

Evidence validation happens at launch. When at least one role is automatic
and routing is `auto`, the launch calls `_discover()` once, after the staging,
roles, context, codex-presence, and authoring checks. It freezes that launch
snapshot for the whole council. The launch snapshot lives in memory only and
never overwrites the planning snapshot. Councils of inherited and explicit
roles never pay that latency.

A role whose evidence has changed resolves to native inheritance with a
reason (`selection evidence changed since discovery: …`). That covers
discovery unavailable, routing ineligible for a routed pair, a model no longer
advertised, hidden, or retired, an effort no longer advertised, and a native
model no longer proven. The run continues: evidence never causes an exit 2.

`_resolve_selection(role, planning, launch, routing_mode, now)` is the single
pure resolver for both paths. Preflight passes no launch snapshot and prints
its decisions as the plan. Launch passes the fresh snapshot, whose evidence
wins. The resolver returns a frozen `SelectionDecision`, which
`dataclasses.replace` attaches to each `Role` before fan-out. `now` is
compared with advertised retirement times. Catalog order and the recommended
marker never change a decision; the tests permute both.

```mermaid
flowchart TD
    Role["role: model, effort, selection"] --> Any{"any of the three?"}
    Any -->|"none"| Native["native: send nothing"]
    Any -->|"yes"| Mode{"selection.mode"}
    Mode -->|"user, or untagged in direct CLI use"| User["user: send the pin unchanged<br/>advisories only"]
    Mode -->|"routed or native_effort"| Off{"routing mode off?"}
    Off -->|"yes"| Fallback["fallback: send nothing<br/>record the reason"]
    Off -->|"no"| Evidence{"launch snapshot if taken,<br/>else the planning snapshot:<br/>status ok?"}
    Evidence -->|"no"| Fallback
    Evidence -->|"yes, routed"| Pair{"routing eligible, model advertised,<br/>visible, not retired,<br/>effort advertised for it?"}
    Evidence -->|"yes, native_effort"| NativeProof{"native model proven,<br/>effort advertised for it?"}
    Pair -->|"yes"| Routed["routed: send -m model<br/>and the effort"]
    Pair -->|"no"| Fallback
    NativeProof -->|"yes"| NativeEffort["native_effort: send -m proven native model<br/>and the effort"]
    NativeProof -->|"no"| Fallback
```

### Provenance and reporting

`SelectionDecision` keeps the request and the dispatch apart. Its fields are:

- `mode`, what the role asked for: `inherit`, `user`, `routed`, or
  `native_effort`;
- `provenance`, what the council does: `native`, `user`, `routed`,
  `native_effort`, or `fallback`;
- `requested_model` and `requested_effort`;
- `dispatch_model` and `dispatch_effort`, where None means that override is
  not sent;
- the selection `reason`;
- a `note`, which holds a fallback reason or a user-pin advisory.

The command builders receive only the dispatch values. Session state never
stores a selection, so a routed choice cannot become a later run's default.
User-pin advisories are notes, never rejections:

- a model absent from the catalog;
- an effort the catalog does not advertise for the pinned model, or for the
  proven native model when only an effort is pinned;
- a partial pin while managed new-thread defaults are present or unknown.

Every surface reports what was sent, never what ran:

- **err.log.** After the unchanged dispatch line comes
  `[codex-council] model selection: routing=<auto|off>; discovery=<ok|unavailable|not-run>[ (<reason>)]; native=N user=N routed=N native_effort=N fallback=N`,
  then one `[codex-council:<id>] routing fell back to native inheritance: <reason>`
  line per fallback. The follower's dispatch detection depends on the
  dispatch line's prefix, which is why it stays unchanged.
- **Report Summary.** Each line notes what the role sent: ` (explicit: …)`,
  ` (routed: …)`, ` (routed effort: <e> on native model <m>)`, or
  ` (native inheritance; routing fell back)`. Inherited roles get no note.
- **Model selection paragraph.** After the Summary comes
  `Model selection: <discovery sentence>. codex exec does not report the model or effort that served a turn; …`.
  The discovery sentence is `discovery not run (<why>)`,
  `launch discovery ok (codex-cli <v>)`, or
  `launch discovery unavailable: <problems>`.
- **Role sections.** Every section opens with `_Model selection: …_` after
  its heading and before any warning. `_format_role_section` renders it for
  both `out.md` and the reply files, so the two stay byte-identical.
- **Reply-file headers.** They carry `selection=<provenance>`, the sent
  `model=`/`effort=`, and, for a fallback, `requested_model=` and
  `requested_effort=`.
- **Preflight.** It prints one `selection plan:` line per role.

All catalog- or Codex-derived text passes through `_report_inline`.

Requested, sent, and reported are three different things. The request is
what `roles.json` says. What was sent is the dispatch values. What was
reported is whatever Codex said, and `codex exec --json` names neither the
model nor the effort, so the council never claims one. A resume advisory
("This session was recorded with model `<recorded>` but is resuming with
`<current>`…") arrives as a Codex item-level error on a successful turn. It
is kept verbatim as a role warning (`codex reported: …`) and never parsed
into a stronger claim.

### Failure classification

Model selection adds two failure classes, `[quota]` and `[model-rejected]`;
the full table is in [Failure-class tagging](#failure-class-tagging). After
the structured stall verdict, `_failure_verdict` applies one order on both
the fresh and resume paths: auth → quota → anchored 429/5xx → model rejected
→ stale (resume only) → substring retriable fallback → untagged. Two
placements matter:

- **Quota comes before the anchored parser.** A provider can send a usage
  limit with HTTP 429, which would otherwise be retried as a rate limit.
- **Model rejection comes before stale recovery.** A rejection whose text
  also contains stale-thread words ("thread not found", "no rollout found")
  must never clear a valid saved thread.

A model rejection needs positive evidence. `_failure_records` reads only
`error` and `turn.failed` events, decodes JSON-in-message up to three levels,
and never looks at agent messages, reasoning, or tool output. The evidence
is either a structured `model_not_found` code with status 400, 404, or none,
or one of Codex's complete sentences about the model this invocation sent
(regex-escaped), found in those records or in the failure text (stderr plus
the same events' messages):

- "The '<m>' model is not supported when using Codex with …" (ChatGPT
  sign-in);
- "The model '<m>' does not exist or you do not have access to it".

These never qualify: bare "not found" or "not supported", the "Model
metadata for … not found" advisory, "Selected model is at capacity" (which
stays transient), and any failure naming `reasoning.effort`,
`model_reasoning_effort`, or `service_tier`.

The `[model-rejected]` message names what was rejected (the requested model,
or the natively configured one) and quotes Codex. It says no substitute was
tried; on the resume path it adds that the saved thread was kept. It then
gives one action by provenance: re-run without `model`, `effort`, and
`selection` for an automatic choice, change or remove the pin for a user pin,
or update the Codex configuration or pin an available model for native
inheritance. There is no automatic runner fallback: the host re-runs the
role.

### Evidence from codex-cli 0.157.1

These probes ran while this was designed (September 2026, macOS, ChatGPT
sign-in). They are historical evidence, not guarantees, and the names below
are placeholders:

- A whole discovery session (spawn, handshake, four requests, close) took
  about 0.65s; a full `--discover` took one to two seconds.
- An unauthenticated `CODEX_HOME` still returned a smaller model list, so a
  catalog is not entitlement evidence.
- A model the account cannot use made `codex exec` exit 1. It printed a
  `Model metadata for … not found` advisory, then an `error`/`turn.failed`
  pair whose message was
  `The '<model>' model is not supported when using Codex with a ChatGPT account.`
- An effort a model does not advertise was accepted (exit 0, normal reply).
  Whether the server honored or clamped it is unknown. Codex does not
  validate effort client-side, so the snapshot's exact-membership check is
  the only effort check for automatic modes, and an explicit pin gets an
  advisory only.
- Parent-placed `-m`/`-c` applied to resumed turns. A resume without them ran
  on the current configured model rather than the thread's recorded one, with
  the advisory
  "This session was recorded with model `<recorded>` but is resuming with `<current>`. Consider switching back to `<recorded>` as it may affect Codex performance."
- `codex --profile <name> app-server` is refused.

### Known limits

- **Planning, launch, and exec are not atomic.** Configuration, account, or
  catalog can still change after launch discovery. Separate processes cannot
  make that window atomic, so launch revalidation narrows it without closing
  it.
- **Legacy managed defaults can override the council.** Per Codex's managed
  configuration documentation, `managed_config.toml` and macOS managed
  preferences take precedence even over CLI `--config` overrides. The
  snapshot shows their layer kind in the configured origins, but routing
  eligibility does not treat them specially, so on such machines a sent value
  may not be the one that runs.
- **Resume under managed defaults is untested.** The resume evidence above
  covers ordinary configuration. How a resumed thread resolves under managed
  new-thread defaults has not been exercised; those defaults disable both
  automatic modes, so the council claims nothing there beyond sending no
  override.

### Deliberately not done

- **No `codex debug models` fallback.** It is a second, experimental
  raw-catalog format with different field names and completeness semantics
  (`--bundled` even skips the remote refresh). It cannot observe layered
  configuration, managed defaults, or auth context, so it would add weaker
  evidence without closing any gap. When app-server discovery is
  unavailable, roles inherit.
- **No post-run `thread/read` telemetry.** The protocol's thread model and
  effort fields describe the current configured or latest persisted values
  and state that they are not per-turn execution telemetry. Reading them
  after a run could not tell which model served a turn; it would only add
  another timeout and delay reply files.
- **No runner model-hopping.** After `[model-rejected]` the runner neither
  substitutes a model nor replays the role with inheritance. A replay could
  repeat a writer role's side effects, and the choice belongs to the host:
  Claude re-runs only that role, without `model`, `effort`, and `selection`.
- **No cross-run cache.** Every run discovers fresh evidence. A cache would
  need invalidation keyed on the executable, configuration layers, account,
  workspace, provider, and CLI version, and it could never use credentials as
  a key. The planning snapshot lives in the run's private directory and is
  revalidated at launch.
- **No profile forwarding.** The runner passes no `--profile`, and the
  app-server refuses one, so discovery could not describe a profiled worker.
  Flattening a profile into `-c` overrides would change provenance and could
  trigger the managed-default coupling.
- **No recommended-default routing.** The catalog's `isDefault` marker is
  recorded as `recommended` for display only. It is never read as the user's
  configured model and never picked automatically.
- **A future native-subagent adapter needs its own tests.** Claude Code
  resolves subagent models differently from Codex workers. Omitting a model,
  an explicit `inherit` (the main conversation's model), and the special
  `default` (which clears an override) are not interchangeable, and an
  environment default such as `CLAUDE_CODE_SUBAGENT_MODEL` can take part.
  Any such adapter must test omission, `inherit`, and `default` separately
  and must not reuse the Codex worker rule that refuses those words as model
  ids.

## Staging validation

```mermaid
flowchart TD
    Mktemp["Claude runs mktemp -d once"] --> Rundir["Private run dir"]
    Rundir --> DiscoverGate["--discover: private-dir gate<br/>roles.json and context.md need not exist yet"]
    DiscoverGate --> Snapshot["model-snapshot.json (0600)<br/>atomic, run-scoped, never cached"]
    Rundir --> Roles["roles.json"]
    Rundir --> Context["context.md"]
    Rundir --> Out["out.md"]
    Rundir --> Err["err.log"]
    Rundir --> Replies["replies/ (0700)<br/>per-role files (0600)"]

    Roles --> Preflight["--check-staging-dir<br/>private-dir gate: lstat, owner, 0700"]
    Context --> Preflight
    Preflight --> Exists{"both files exist?"}
    Exists -->|"no"| StageError["exit 2 with staging hint"]
    Exists -->|"yes"| SameDir{"same mktemp dir?"}
    SameDir -->|"no"| StageError
    SameDir -->|"yes"| Parse["parse roles + validate context"]
    Parse -->|"empty or non-UTF-8 context"| StageError
    Parse -->|"bad roles JSON, unknown or duplicate key,<br/>malformed selection"| RolesError["exit 2 with whole-file<br/>rewrite recovery"]
    Parse -->|"ok"| Authoring{"automatic selections supported<br/>by this run's snapshot?"}
    Snapshot --> Authoring
    Authoring -->|"no"| RolesError
    Authoring -->|"yes: staging OK + selection plan"| LaunchGate["launch path re-validates privacy<br/>lexical parent of every on-disk input<br/>before any content read"]
    LaunchGate -->|"public, symlinked, or foreign-owned parent"| StageError
    LaunchGate -->|"private"| Revalidate["re-parse, re-check authoring,<br/>one launch discovery when a role is automatic"]
    Revalidate --> Launch["launch fan-out"]

    Launch --> Replies
    Launch --> Out
    Launch --> Err
    Err --> Follow["--follow relays [codex-council lines<br/>drops reply= paths outside replies/"]
    Err --> Sentinel["CODEX_COUNCIL_DONE"]
```

## State key and locking

```mermaid
flowchart LR
    Root["project root"] --> RootHash["sha256 root prefix"]
    Env["explicit or auto session key"] --> SessionHash["optional sha256 session prefix"]
    Role["role id"] --> RoleKey["literal legacy id or sha256 key"]

    RootHash --> Filename
    SessionHash --> Filename
    RoleKey --> Filename
    Filename --> State["$XDG_STATE_HOME/codex-council/key__role.json"]
    State --> Lock["state-file lock"]
    Lock --> Load["load stored thread id"]
    Load --> Resume["codex exec resume"]
    Resume --> Match{"thread id matches?"}
    Match -->|yes| Save["save session metadata"]
    Match -->|no| Adopt["adopt new thread id + warn"]
    Adopt --> Save
    Resume -->|stale| Fresh["clear state + fresh codex exec"]
    Fresh --> Save
```

## Resume footgun mitigation

`codex exec resume <id>` parses `<id>` as a UUID first (UUIDs take
precedence if it parses). Verified against the installed codex-cli: a
valid-but-unknown UUID **errors** (`no rollout found for thread id ...
(code -32600)`, exit 1) and is handled by the stale-resume path (clear
state + restart fresh); only a value that is **not** a valid UUID is
treated as a thread *name* and silently starts a **new** thread (rc==0,
fresh `thread.started`). The council only ever stores real UUIDs emitted
by `thread.started`, so the silent-spawn case is unreachable via normal
state — the mismatch check is **defense-in-depth** against a
corrupt/hand-edited state file or future CLI drift. After every resume
the script extracts `thread.started.thread_id`; if it doesn't equal the
requested ID, it adopts the new ID and warns. It does **not** re-run —
the turn has already completed on the new thread; re-running burns
tokens for no benefit.

Model and effort overrides ride on the parent command
(`codex exec -C <root> [-m <model>] [-c model_reasoning_effort="<effort>"] resume <id>`),
so a resumed attempt carries the same dispatch values as a fresh one. A
resumed thread that sends none runs on the current native configuration, and
a `[model-rejected]` failure on resume keeps the stored thread even when its
text also looks stale (see
[Failure classification](#failure-classification)).

Per-role state is protected by a POSIX advisory lock keyed by
`(project, session key, role)`. Role IDs longer than the formerly accepted
32-character range use a deterministic SHA-256 filename component, avoiding
the operating system's filename-length limit while preserving the full role ID
in memory, reports, prompts, and state metadata. Short-role state filenames
remain unchanged for thread-continuity compatibility. The session key is explicit when
`CODEX_COUNCIL_SESSION_KEY` is set; otherwise the runner auto-detects common
host-session identifiers such as Claude session ids, `CODEX_THREAD_ID`,
`TERM_SESSION_ID`, `TMUX_PANE`, `STY`, and `VSCODE_PID`. That gives normal
multi-terminal isolation without requiring the user to export anything, while
calls from the same terminal/session keep continuity. `VSCODE_PID` is the
lowest-priority fallback and is **window-scoped**, not tab-scoped: multiple
integrated terminals in one VS Code window share it and therefore share role
threads — set `CODEX_COUNCIL_SESSION_KEY` (or rely on a finer identifier such as
`TERM_SESSION_ID`) to isolate those. The lock is held across
the whole load/resume-or-fresh/save retry loop, not just individual file reads
or writes, so two council processes cannot concurrently resume the same role
thread and then last-writer-wins the state file. Different roles still run in
parallel.

## Failure-class tagging

Recognized failure classes are tagged before they hit the report;
unrecognized failures carry the raw stderr untagged:

| Tag | Behavior |
|---|---|
| `[auth]` | Never clears state, never retries — caller must fix auth then re-run |
| `[quota]` | Terminal: never retried, never clears state. A usage, quota, or credit limit — a structured `error.code` or `error.type` such as `insufficient_quota`, `usage_limit_reached`, or `credit_balance_exhausted`, or codex's "hit your usage limit" prose — even when it carries HTTP 429 |
| `[retriable:rate-limit]` / `[retriable:5xx]` | One retry after a 5s backoff (MAX_RETRY_ATTEMPTS=2; bumping that adds 10s, 20s, … via `backoff *= 2`) |
| `[model-rejected]` | Terminal: never retried, no substitute model, and it never clears saved thread state, even when its text also looks stale. Codex rejected the model this invocation sent (or the natively configured one); the message quotes Codex and names one action for the role's provenance |
| `[retriable:stall]` | Output-inactivity watchdog fired before any side-effect-capable tool work began; replay is safe, so it retries through the same shared budget as rate-limit/5xx |
| `[stall]` | Watchdog fired after tool work began — terminal, because an automatic replay could duplicate side effects; a buffered agent_message without turn completion is quoted but never auto-promoted to success |
| `[orchestrator-exception]` | A role's coroutine raised — siblings still complete via `gather(..., return_exceptions=True)` |
| `[orchestrator-bug]` | A role task returned something other than a `RoleResult`; reported as a failure instead of crashing the report |
| (untagged stale) | Detected via `STALE_RESUME_MARKERS` on the resume path only; that role's state is cleared and a fresh thread is started for it only |

A stall verdict is structured (from the watchdog), not text-sniffed, and is
handled before any text classification: partial stale- or auth-looking stderr
in a killed run must neither classify the failure nor clear resume state. A
stalled attempt whose turn had already completed (final
agent_message buffered plus turn completion) is not a failure at all — the
kill hit a wedged shutdown, so the reply is kept as success with the warning
"codex wedged after completing its turn; process terminated" and state is
saved best-effort. Every other non-zero exit is classified by
`_failure_verdict` in one order, identical on the fresh and resume paths:
auth → quota → anchored 429/5xx → model rejected → stale (resume only) →
substring retriable fallback → untagged.

Classification uses stderr plus structured Codex JSONL stdout error
events (`type:error`, `turn.failed`). The **primary** retriable signal
is the numeric HTTP status parsed out of the JSONL error body
(`_extract_statuses`), recognized in any *anchored* form — the JSON
`"status"` key, a `HTTP NNN` / `status NNN` keyword, or a canonical
reason phrase like `NNN Too Many Requests` — but never a bare digit run
(so a `429` inside a thread id is ignored): status `429` → rate-limit,
`500–599` → 5xx (so a `529` "overloaded" is retried even though it is
not in the literal marker list). An anchored retriable status is trusted
ahead of the stale-resume check, so a transient `HTTP 429 … thread not
found` on resume backs off and retries instead of discarding the thread. A structured status is authoritative — when a
non-retriable status (e.g. `400`) is present, the looser substring
markers are **suppressed**, so a bare `429` or `service unavailable`
echoed inside a 400 body no longer forces a wrong retry. A non-retriable
error *type* (`invalid_request_error`) suppresses the fallback the same
way, covering the 4xx bodies codex sometimes surfaces without a numeric
status. The substring
markers (`RATE_LIMIT_MARKERS` / `TRANSIENT_5XX_MARKERS`) are a
**fallback** for failures that carry no parseable status — covering the
current codex-cli code-less rewrites such as `experiencing high demand`,
`server overloaded`, `selected model is at capacity`, and
`request was throttled` (`backend overloaded` is retained as a legacy
fallback for older codex/provider text). That coverage is deliberately
scoped: an echoed status phrase inside an `error.message` has no
provenance and remains a known limit, not something the markers try to
guess at. Usage/quota
limits are **not** retriable: a plan cap does not clear within a 5s
backoff. The recognized forms, structured codes read from the failure
records and codex's usage-limit prose, are tagged `[quota]` ahead of the
anchored parser, because a provider can send them with HTTP 429. JSONL
parsing intentionally skips malformed and non-object events while preserving
later valid agent messages.

## Liveness: no run-level deadline, output-inactivity watchdog

The council has no total elapsed-time or run-level deadline. A role may run
indefinitely while its codex subprocess continues producing output bytes —
the runner never ends it, though the host that launched it (for example a
Claude Code background task) bounds its lifetime — and `codex exec` itself
imposes no run-level timeout either. Separately, each codex subprocess has an
**output-inactivity
watchdog** based only on the time since its most recent stdout/stderr byte.
Incremental readers pump both pipes in fixed-size chunks (never
line-buffered reads, which would cap unrestricted JSONL line sizes) and stamp
a shared last-activity clock; raw bytes are buffered per stream and decoded
once after the pumps join, so a UTF-8 sequence split across chunks survives.
After `CODEX_COUNCIL_STALL_SECS` seconds of council-visible silence
(default 1800; positive integer override; 0 disables; anything else is a
usage error, exit 2), the watchdog terminates that attempt — SIGTERM, a short
grace, then SIGKILL to the process group, with every termination path
converging on a single idempotent owner so watchdog, cancellation, and error
teardowns never race — and the stall policy in the table above decides the
outcome. Setting 0 may again permit an indefinitely silent role.

The watchdog measures **bytes, not progress**: current codex `exec --json`
suppresses agent-message/reasoning `item.started` events and all
token/exec-output deltas, so a healthy role can be byte-silent for long
stretches — the claim is output-inactivity recovery only, never semantic
wedge detection. Codex's per-provider stream-idle timeout
(`model_providers.<id>.stream_idle_timeout_ms`, 5 min default, bounded
retries) is a separate provider-side control left to the user's Codex
configuration: it is provider-scoped and the active provider id
varies, so the council cannot target it portably. `start_new_session=True` on
each `codex exec` puts it in its own process group, so a Ctrl+C (or any
other cancellation) sends SIGTERM, waits briefly, then sends SIGKILL
to the group; any shell commands codex itself spawned for tool calls
are also reaped. SIGINT/SIGTERM/SIGHUP to the council process cancel
the fan-out first, then exit without emitting the final
`CODEX_COUNCIL_DONE` sentinel; reply files already written stay on disk.
POSIX-only.
