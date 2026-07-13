# Runtime continuity and failure behavior

Read this reference when deciding whether to reuse role IDs, isolating sessions,
or diagnosing retry, quota, authentication, liveness/stall, or continuity
behavior.

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
- `[retriable:stall]` — a watchdog-terminated attempt with no
  side-effect-capable tool work — retries through the same shared budget as
  rate-limit/5xx; there is no separate stall budget. `[stall]` is terminal:
  tool work had begun, so an automatic replay could duplicate side effects —
  re-invoke the role manually if needed.
- **Usage/quota-limit** and authentication failures do not retry. Fix the plan
  cap or authentication and invoke the council again.
- The council has no total elapsed-time or run-level deadline: a role may run
  indefinitely while its codex subprocess keeps producing output bytes — hours
  or days is fine. The only liveness control is the per-subprocess
  output-inactivity watchdog below; Codex's provider stream-idle guard covers
  a stalled connection, not a run-level deadline. Ctrl+C tears down every
  in-flight Codex process group.
- A role waiting for another council's same-role continuity lock remains queued.
  Each failed nonblocking probe closes its file descriptor and releases the
  subprocess permit before sleeping, so the waiter neither appears active nor
  exhausts permits or file descriptors in a large panel. The probe interval
  backs off from 0.1s to a 2s cap, since the lock holder has no run-level
  deadline. Lock acquisition is probe-based, not FIFO-fair: a long-waiting
  role can lose a probe race to a newer waiter. Each role has exactly one
  lock file (no striping); both are known, low-impact limitations.

## Output-inactivity watchdog and stall policy

Each codex subprocess has an output-inactivity watchdog based **only** on the
time since its most recent stdout/stderr byte. After
`CODEX_COUNCIL_STALL_SECS` seconds of council-visible silence, the runner
terminates that attempt (SIGTERM, short grace, SIGKILL to the process group)
and applies the stall policy:

- Turn already completed and the final agent_message is buffered: the kill hit
  a wedged shutdown, not lost work. The reply is kept as **success** with the
  warning "codex wedged after completing its turn; process terminated"; state
  is saved best-effort; no retry.
- No side-effect-capable tool work had begun (only pure-text
  agent_message/reasoning items, or nothing): replay is safe —
  **`[retriable:stall]`**, retried through the shared retry budget.
- Otherwise: **terminal `[stall]`** — tool work had begun and replaying could
  duplicate side effects. A buffered agent_message without turn completion is
  quoted in the error but never auto-promoted to success.

`CODEX_COUNCIL_STALL_SECS` semantics: unset → 1800 (the default); `0`
disables the watchdog (which may again permit an indefinitely silent role);
a positive integer overrides the threshold; anything else refuses the launch
with a usage error (exit 2). The stall verdict is structured and handled
before any text classification, so stale- or auth-looking fragments in a
killed run's stderr neither classify the failure nor clear resume state.

The watchdog's claim is **output-inactivity recovery only**; semantic wedge
detection is out of scope. Current codex `exec --json` suppresses
agent-message/reasoning `item.started` events and all token/exec-output
deltas, so a healthy role can be byte-silent for long stretches.

## Progress lines, heartbeat, and version visibility

All progress is advisory stderr (redirected to `err.log` by the launch
command); its loss never changes role results or the exit code. Per-attempt
start lines look like
`[codex-council] <role>: started (fresh|resume) attempt=1/2 watchdog=1800s`
(`watchdog=disabled` when the env var is 0). A stall termination logs
`[codex-council:<role>] stall threshold reached (quiet=Ns, watchdog=Ns);
terminating attempt` before the policy above is applied.

While work remains, a heartbeat is emitted every `min(1800, stall_secs // 3)`
seconds with a 300s floor while the watchdog is enabled (600s at the default
threshold; 1800s when disabled):

```
[codex-council] still running after 1240s: completed=1/3; active=2 (architect quiet=41s, prober retry-wait); queued=0; watchdog=1800s; version=0.9.0.
```

`active` is scheduling state, not proof of health. `quiet=Ns` measures time
since the last stdout/stderr byte, not semantic progress. Never describe a
role as working normally solely because it is active or has low quiet; a
wedged process emitting keepalive bytes resets quiet without progressing.
Roles sleeping out a retry backoff report `retry-wait` instead of a stale
quiet value.

The preflight "staging OK" line, the dispatch line, the heartbeat, and the
final `CODEX_COUNCIL_DONE` sentinel all carry `version=<plugin version>` for
postmortem visibility (knowing which plugin version ran), not skew
prevention. The complementary `--skill-contract <int>` flag is the skew
guard: SKILL.md's command templates pass the epoch they were written against,
and a mismatch with the script refuses the launch as a stale SKILL/script
pair.
