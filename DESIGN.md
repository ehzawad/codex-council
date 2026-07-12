# codex-council internals

Implementation details for contributors. User-facing docs live in
[README.md](README.md).

## No catalog, no defaults

The script accepts roles **only** via `--roles-file` (a path to a JSON
file holding the list of `{id, label, instruction}` objects), and the
preferred launch path supplies context via `--context-file` in the same
private staging directory. `instruction` is a **list of sentence-sized
strings** — the only accepted form — that the script
whitespace-normalizes and joins into the single paragraph Codex sees.
The list form exists because the
only production writer of roles.json is an LLM file-Write: multi-KB
single-line JSON string literals are where its writes corrupt (GH issue
#2). For the same reason, unknown keys in a role object are **rejected**
with a rewrite-the-whole-file message — stray filler fields like
`"_": ""` are the signature of a glitched write, not harmless extras.
The staging-dir gate (`--check-staging-dir`) lstats the directory:
symlinks, non-dirs, foreign-owned dirs, and group/other-accessible
modes are all rejected with an action-first recovery hint that forbids
chmod/mkdir/reuse of the rejected path and demands a fresh `mktemp -d`
(GH issue #1: the old "Create it with mktemp -d" hint was satisfiable
by chmod on the same predictable path). The staged input files themselves must
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

The reasoning: every hardcoded catalog is a bias. The original 6
coding roles biased Claude toward coding panels. A later expansion to
15 roles across four thematic groups (coding/writing/data/research)
helped non-coding work but still biased Claude toward
"pick-from-this-shelf" rather than "compose-from-context." Pulling
the catalog out entirely forces Claude (the orchestrator) to
ultrathink about the user's task, design role ids/labels/instructions
on-the-fly, announce the composed panel, and then fan out. The
script's job is fan-out, retry, and aggregation — Claude owns reconciliation
into the user's shared goal rather than relaying disconnected role opinions.

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

The intended product shape is an adaptive, general-purpose, AGI-style
collaboration pattern, not a claim that the active model is proven AGI. Claude
derives roles from the current goal and orchestrates them through shared
context, workspace access, persisted role threads, and final reconciliation.
The same machinery can support implementation, diagnosis, creation, planning,
research, review, or other domains as far as the active model and tools allow.
It deliberately leans toward programmatic problem-solving—computer science,
software and ML/AI engineering, DevSecOps, platform/security automation,
debugging/testing, project implementation, and evidence-based technical
research—without reinstating a domain catalog or fixed role shelf.

Codex itself now has a stable in-process `multi_agent` capability and a
separate under-development `multi_agent_v2` feature. The council deliberately
uses external `codex exec` fan-out because each role needs an independently
persisted thread id and process-level failure/cancellation isolation. Codex's
documented `agents.max_threads` setting (default 6) is still used as a
conservative concurrency signal; it is not treated as proof of provider
capacity because these are separate processes.

A single fan-out is parallel contribution, not direct peer messaging. Claude
mediates collaboration by giving every role the same situational context,
reconciling the report, and staging material findings into selective follow-up
rounds. Because all subprocesses share the working directory, implementation
panels assign one write-owning executor/integrator by default; multiple writers
must use isolated worktrees or serialized phases. This also limits duplicate
side effects when a transient failure causes a role retry.

## End-to-end fan-out

```mermaid
sequenceDiagram
    participant C as Claude Code
    participant T as codex_council.py
    participant A as codex exec (role A)
    participant B as codex exec (role B)
    participant N as codex exec (role N)

    C->>T: Staged context via --context-file + --roles-file
    par up to effective max_parallel
        T->>A: role framing + collaboration brief + shared context
        T->>B: role framing + collaboration brief + shared context
        T->>N: role framing + collaboration brief + shared context
    end
    A-->>T: JSONL events
    B-->>T: JSONL events
    N-->>T: JSONL events
    T->>T: per-role extract_final_message
    T-->>C: aggregated markdown report
    C-->>C: reconcile across roles
```

## Context working set

Claude, not the Python runner, decides what conversation context to stage. For
long host sessions it constructs a decision-complete working set: the current
problem/project and goal-directed or exploratory trajectory; in-flight modules,
files, objects, drafts, queries, experiments, tests, deployments, and research;
active bugs/errors, symptoms, attempted fixes, hypotheses, and evidence; recent
working context at high fidelity; live primary evidence from disk; known
unknowns, blind spots, assumptions, and provenance; and older still-relevant
decisions, invariants, and rejected approaches as a faithful summary.
Conversation age alone never controls inclusion. Superseded state, duplicate
discussion, and irrelevant history are omitted. A compacted host summary is an
index that must be reconciled with current live state before launch.

The runner accepts that staged context without a byte cap or truncation. It
does not attempt token counting because the active model/provider owns the real
context window and can change independently of this plugin. `_compose_prompt`
keeps the shared context intact, labels it, adds a compact collaboration brief
that tells each role how to interpret the situational map, and bookends it with
the role-specific instruction.

## Adaptive concurrency and progress

Panels have no count cap, but `run_council` wraps role execution in an
`asyncio.Semaphore`. The active limit resolves in this order:

1. positive `CODEX_COUNCIL_MAX_PARALLEL` override;
2. positive user-level Codex `agents.max_threads` from
   `$CODEX_HOME/config.toml` (or `~/.codex/config.toml`);
3. `DEFAULT_MAX_PARALLEL=6`, matching Codex's current documented default.

Each queued role makes a nonblocking continuity-lock probe while it briefly
holds a subprocess permit. If another council owns the same persisted thread,
the probe closes its file descriptor, releases the permit immediately, sleeps
outside the permit, and retries; unrelated roles can run, and arbitrarily large
panels do not accumulate one open lock file per queued role. Only a role that
holds both its continuity lock and permit appears active or launches Codex. A
30-minute heartbeat records completed, active, and queued counts in stderr;
Claude Code redirects it to `err.log` and uses native task notifications plus a
one-shot session cron when available.

## Staging validation

```mermaid
flowchart TD
    Mktemp["Claude runs mktemp -d once"] --> Rundir["Private run dir"]
    Rundir --> Roles["roles.json"]
    Rundir --> Context["context.md"]
    Rundir --> Out["out.md"]
    Rundir --> Err["err.log"]

    Roles --> Preflight["--check-staging-dir"]
    Context --> Preflight
    Preflight --> Exists{"both files exist?"}
    Exists -->|no| StageError["exit 2 with staging hint"]
    Exists -->|yes| SameDir{"same mktemp dir?"}
    SameDir -->|no| StageError
    SameDir -->|yes| Parse["parse roles + validate context"]
    Parse -->|bad JSON/empty context| StageError
    Parse -->|ok| Launch["launch fan-out"]

    Launch --> Out
    Launch --> Err
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

Per-role errors are tagged before they hit the report:

| Tag | Behavior |
|---|---|
| `[auth]` | Never clears state, never retries — caller must fix auth then re-run |
| `[retriable:rate-limit]` / `[retriable:5xx]` | One retry after a 5s backoff (MAX_RETRY_ATTEMPTS=2; bumping that adds 10s, 20s, … via `backoff *= 2`) |
| `[orchestrator-exception]` | A role's coroutine raised — siblings still complete via `gather(..., return_exceptions=True)` |
| (untagged stale) | Detected via `STALE_RESUME_MARKERS`; that role's state is cleared and a fresh thread is started for it only |

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
**fallback** for failures that carry no parseable status — including the
current codex-cli code-less prose `experiencing high demand` and
`server overloaded` (`backend overloaded` is retained as a legacy
fallback for older codex/provider text). Usage/quota
limits are **not** retriable: a plan cap does not clear within a 5s
backoff, so it is surfaced terminal rather than retried. JSONL parsing
intentionally skips malformed and non-object events while preserving
later valid agent messages.

No wall-clock cap is applied to roles or to the council as a whole —
each role runs as long as Codex takes (hours or days is fine).
`codex exec` itself has no run-level timeout. Its only default that
could end a long-*quiet* run is the per-provider stream-idle timeout
(`model_providers.<id>.stream_idle_timeout_ms`, 5 min, then a bounded
retry count), which an actively-streaming role never trips. Widening
it is left to the user's `~/.codex/config.toml` rather than overridden
here: it is provider-scoped and the active provider id varies, so the
council cannot target it portably. `start_new_session=True` on each
`codex exec` puts it in its own process group, so a Ctrl+C (or any
other cancellation) sends SIGTERM, waits briefly, then sends SIGKILL
to the group; any shell commands codex itself spawned for tool calls
are also reaped. SIGINT/SIGTERM/SIGHUP to the council process cancel
the fan-out first, then exit without emitting the final
`CODEX_COUNCIL_DONE` sentinel.
POSIX-only.
