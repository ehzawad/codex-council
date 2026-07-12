# Runtime continuity and failure behavior

Read this reference when deciding whether to reuse role IDs, isolating sessions,
or diagnosing retry, quota, authentication, timeout, or continuity behavior.

## Session continuity

The runner stores one Codex thread per `(project, host session, role)` at
`$XDG_STATE_HOME/codex-council/{project-hash}-{session-hash}__{role-key}.json`
when a stable host-session ID is available. It detects common identifiers such
as Claude session IDs, `CODEX_THREAD_ID`, `TERM_SESSION_ID`, `TMUX_PANE`, `STY`,
and `VSCODE_PID`. Multiple integrated terminals in the same VS Code window share
`VSCODE_PID`; set `CODEX_COUNCIL_SESSION_KEY` when they need isolation.

Stale resumes restart only the affected role. Reuse a role ID only when its lens
and task remain semantically continuous; otherwise mint a new task-specific ID.
Current staged context and verified workspace evidence override thread memory.
Formerly accepted short IDs remain literal filename components; longer IDs use
a deterministic SHA-256 role key to avoid filesystem component limits.

`CODEX_COUNCIL_SESSION_KEY` explicitly overrides automatic scoping. Set
`CODEX_COUNCIL_DISABLE_AUTO_SESSION_KEY=1` only to request the older
project-wide `{project-hash}__{role-key}.json` state shape.

## Retries and long runs

- Rate-limit (429) and 5xx failures retry once with exponential backoff. Numeric
  HTTP status in the JSONL error body wins; substring markers are fallback only,
  and a definite non-retriable 4xx suppresses that fallback.
- **Usage/quota-limit** and authentication failures do not retry. Fix the plan
  cap or authentication and invoke the council again.
- No wall-clock timeout is enforced. Codex may run for hours or days; its
  provider stream-idle guard covers a stalled connection, not a run-level
  deadline. The runner emits 30-minute heartbeats, and Ctrl+C tears down every
  in-flight Codex process group.
- A role waiting for another council's same-role continuity lock remains queued.
  Each failed nonblocking probe closes its file descriptor and releases the
  subprocess permit before sleeping, so the waiter neither appears active nor
  exhausts permits or file descriptors in a large panel. The probe interval
  backs off from 0.1s to a 2s cap, since the lock holder has no wall-clock
  limit.
