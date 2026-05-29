# codex-council internals

Implementation details for contributors. User-facing docs live in
[README.md](README.md).

## No catalog, no defaults

The script accepts roles **only** via `--roles-file` (a path to a JSON
file holding the list of `{id, label, instruction}` objects). Keeping
the panel in a file keeps a large role array out of the shell, where a
stray quote or brace would otherwise break the call. There is no
built-in role catalog, no positional shortcuts, and no `--list-roles`
flag. Bare invocation (no `--roles-file`, with context piped) exits
2 — the script's way of telling Claude to go compose a panel.

The reasoning: every hardcoded catalog is a bias. The original 6
coding roles biased Claude toward coding panels. A later expansion to
15 roles across four thematic groups (coding/writing/data/research)
helped non-coding work but still biased Claude toward
"pick-from-this-shelf" rather than "compose-from-context." Pulling
the catalog out entirely forces Claude (the orchestrator) to
ultrathink about the user's task, design role ids/labels/instructions
on-the-fly, confirm via AskUserQuestion, and only then fan out. The
script's job is fan-out, retry, and reconciliation — not role
opinions.

Practical consequence: every invocation requires Claude to compose
the full role JSON. That's more tokens per panel proposal, but it
matches the actual design intent (adaptive in-context selection) and
removes any pull toward formulaic coding-flavored panels.

Codex itself has an in-process multi-agent capability behind the
`multi_agent_v2` feature flag (verified live: `--enable multi_agent_v2`
opens up `spawn_agent`/`wait_agent`/`close_agent` tools). The council
deliberately does **not** use it — its v1 stage is "under
development" and its v1 surface is gated behind `tool_search` deferral
plus prose discouragement. External fan-out gives us failure
isolation, distinct thread_ids on disk, and ~20% the dependency
surface.

```mermaid
sequenceDiagram
    participant C as Claude Code
    participant T as codex_council.py
    participant A as codex exec (role A)
    participant B as codex exec (role B)
    participant N as codex exec (role N)

    C->>T: Pipe context via stdin + --roles-file
    par
        T->>A: bookended prompt (role A framing)
        T->>B: bookended prompt (role B framing)
        T->>N: bookended prompt (role N framing)
    end
    A-->>T: JSONL events
    B-->>T: JSONL events
    N-->>T: JSONL events
    T->>T: per-role extract_final_message
    T-->>C: aggregated markdown report
    C-->>C: reconcile across roles
```

## Resume footgun mitigation

Per Codex source, `codex exec resume <bogus_or_invalid_uuid>` silently
falls through to creating a **new** thread instead of erroring. Without
a check, we'd persist an unrelated thread ID under the role's state
key. After every resume the council script extracts
`thread.started.thread_id` from the JSONL stream; if it doesn't equal
the requested ID, it adopts the new ID and warns. It does **not**
re-run — the turn has already completed on the new thread; re-running
burns tokens for no benefit.

Per-role state is protected by a POSIX advisory lock keyed by
`(project, session key, role)`. The lock is held across the whole
load/resume-or-fresh/save retry loop, not just individual file reads
or writes, so two council processes cannot concurrently resume the
same role thread and then last-writer-wins the state file. Different
roles still run in parallel.

## Failure-class tagging

Per-role errors are tagged before they hit the report:

| Tag | Behavior |
|---|---|
| `[auth]` | Never clears state, never retries — caller must fix auth then re-run |
| `[retriable:rate-limit]` / `[retriable:5xx]` | One retry after a 5s backoff (MAX_RETRY_ATTEMPTS=2; bumping that adds 10s, 20s, … via `backoff *= 2`) |
| `[orchestrator-exception]` | A role's coroutine raised — siblings still complete via `gather(..., return_exceptions=True)` |
| (untagged stale) | Detected via `STALE_RESUME_MARKERS`; that role's state is cleared and a fresh thread is started for it only |

Classification uses stderr plus structured Codex JSONL stdout error
events (`type:error`, `turn.failed`). JSONL parsing intentionally
skips malformed and non-object events while preserving later valid
agent messages.

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
