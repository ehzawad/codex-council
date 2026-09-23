# Runtime behavior

Read this reference when following a long run, deciding what to do with a
reply that arrived early, recovering a lost or stuck run, reusing role IDs,
or diagnosing retries, quota, authentication, stalls, or continuity.

## Contents

- Launch mechanics and why they are strict
- Reply files and the completion line
- Following a run
- Recovery triage
- Exit code, report, and failure tags
- Session continuity
- Retries and long runs
- Output-inactivity watchdog and stall policy
- Progress lines, heartbeat, and version visibility

## Launch mechanics and why they are strict

The run directory comes from one `mktemp -d` call and is private (mode 0700,
owned by the user). The report and context can hold sensitive reviewed
content, and predictable `/tmp/council_*` names are world-readable under a
typical umask and can be pre-created or symlinked by another local user, who
could then read the report or plant a fake `CODEX_COUNCIL_DONE` line. The
pre-flight (`--check-staging-dir`) and the launch both check that each
input's parent directory is private, and that `roles.json` and `context.md`
are regular, non-symlink files, before reading any content. A rejected
directory is abandoned, never repaired with chmod or mkdir.

The private directory keeps out other local users, not the roles. Roles run
unsandboxed as the same user, so they can write to `err.log`, `out.md`, and
`replies/` directly, and no check running as that user can authenticate the
runner's lines. That is inherent to giving roles full workspace access. The
mitigations are: the follower drops completion lines whose `reply=` path is
not directly inside `ABS_RUNDIR/replies/`; reply files and role output are
treated as untrusted data; and the final reconciliation waits for Claude
Code's background-task completion notification, which no role can emit.

The launch uses exactly one backgrounding layer, the Bash tool's
`run_in_background: true`. That wrapper is a shell Claude Code tracks; any
inner detach (`&`, `nohup`, `setsid`, `disown`, a supervisor, and so on)
makes the wrapper exit immediately with empty output, reparents
`codex_council.py` to `launchd` or PID 1, and loses the real completion
notification. Redirecting stdout and stderr to files in the run directory
keeps the run observable and recoverable from disk.

Bare invocation (no `--roles-file`) exits 2, as a guard against accidental
fan-out.

## Reply files and the completion line

When a role settles (ok, failed, or crashed), the runner writes that role's
report section to `ABS_RUNDIR/replies/<key>.md` and only then logs its
completion line:

```
[codex-council] 2/5 architect: ok (812.4s) reply=/abs/run/dir/replies/architect.md
```

The status is `ok (<secs>s)`, `FAILED (<secs>s)`, or `crashed (<ExcType>)`.
Any of them can carry `reply=`; a failed or crashed role's file carries its
failure tag, so read it rather than waiting for `out.md`. A line without
`reply=` means no file was written for that role (see the best-effort note
below).

`<key>` is the role id when it is a short safe filename and a deterministic
hash otherwise, so always use the path printed after `reply=`. Each file
starts with a one-line status header and then holds exactly the section
`out.md` will contain for that role, so reading it early and reading the
final report cannot disagree. Files are written atomically (temporary file,
fsync, rename) with mode 0600 inside a `replies/` directory of mode 0700.

Reply files are best-effort: if `replies/` cannot be created safely (for
example it already exists as a symlink or with loose permissions), the runner
logs one warning, omits the `reply=` suffix, and the council still completes
with the full `out.md`. Files already written survive Ctrl+C or SIGTERM, so
finished work is not lost when a run is interrupted; an interrupted run
still has no `CODEX_COUNCIL_DONE` sentinel and no report in `out.md`.

## Following a run

The follower is a read-only command designed for Claude Code's Monitor tool:

```
python3 "${CLAUDE_PLUGIN_ROOT}/skills/codex-council/scripts/codex_council.py" \
  --follow 'ABS_RUNDIR' --skill-contract 2
```

It checks that `ABS_RUNDIR` is a private directory, waits for `err.log` to
appear, and prints every `[codex-council` line (start, completion, retry,
stall, heartbeat, sentinel, interruption) as it is written, one line per
event. It only reads, so it cannot change the run. A completion line whose
`reply=` path is not directly inside `ABS_RUNDIR/replies/` is dropped,
because the runner never prints one. Its exit codes:

| Exit | Last line | Meaning and action |
|---|---|---|
| 0 | `CODEX_COUNCIL_DONE`, `interrupted by ...`, or `runner aborted exit=N: ...` | The run ended. Read `out.md` (absent after an interruption or abort) and `err.log`. |
| 2 | usage error on stderr | `ABS_RUNDIR` is wrong or not private. Fix the path; do not re-arm unchanged. |
| 3 | `[codex-council-follow] no council activity: ...` | Within 120s either `err.log` never appeared or it has no dispatch line. The launch failed or never happened: read `err.log` and the background task output. |
| 4 | `[codex-council-follow] runner presumed gone: ...` | A dispatched run's `err.log` has not changed for about an hour (3660s: twice the 30-minute maximum heartbeat interval plus 60s, measured from the file's mtime). Stop re-arming: a new follower would exit 4 again at once. Check the background task, then use the recovery triage below. |

When `err.log` shows a Python traceback, the follower also prints one
advisory `[codex-council-follow]` line and keeps following, since the runner
may continue. A system suspend is detected and restarts the silence count.

Monitors expire after at most 30 minutes (`timeout_ms` 1800000). Re-arm the
same command on that expiry, and only then. A re-armed follower starts from
the top of `err.log` and replays earlier lines; skip completions already
handled.

Without the Monitor tool, schedule a one-shot 30-minute wake-up (session
cron) whose prompt names the background task id and the exact `ABS_RUNDIR`.
At each wake-up, read the new lines of `err.log`, read any new reply files,
update the user (completed, active, queued), and schedule another wake-up
only if the run continues. If crons are unavailable too, use the native
background-task wait (`TaskOutput` when exposed) with its supported horizon.
Never poll with a shell `sleep` loop. In every mode the `run_in_background`
completion notification is the final backstop.

What to do with an early reply:

- Read it and give the user a one-line update.
- Act on independent work: verify its claims read-only, or make edits that
  cannot collide with a still-running role that may write.
- Wait for the full report before the final verdict, before resolving a
  question another pending role could answer differently, and before writes
  that overlap a running writer role.
- Present early findings as provisional until reconciliation.

## Recovery triage

If a run is lost, orphaned, or looks stuck, recover from disk:

```
pgrep -fl 'codex_council[.]py'      # any council alive?
pgrep -fl 'ABS_RUNDIR/roles.json'   # this run specifically
tail -n 40 'ABS_RUNDIR/err.log'     # last line CODEX_COUNCIL_DONE -> finished
ls 'ABS_RUNDIR/replies'             # replies that already settled
```

Work through these in order; the first match wins. `active` is scheduling
state, not proof of health, and `quiet=Ns` measures time since the last
output byte, not semantic progress, so do not describe a role as healthy only
because it is active or quiet is low.

1. `CODEX_COUNCIL_DONE` present → finished; read `out.md`; do not re-invoke.
   (If the background task still shows as running, trust the task state.)
2. A `[retriable:stall]` or stall-termination line present → the runner is
   handling it; do not launch another council.
3. Active roles with `quiet` below the printed `watchdog=` value → keep
   following; report the run as "output-active", not healthy.
4. `quiet` at or past the watchdog with no stall line after a short grace and
   a fresh read → the watchdog itself is suspect: stop the tracked background
   task, confirm the council process is gone, inspect `err.log`, then
   re-invoke once. Replies already in `replies/` are still valid.
5. `watchdog=disabled` → no automatic liveness recovery; rising quiet is
   indeterminate; ask the user before acting.
6. No sentinel and no process → it crashed or was interrupted; read
   `err.log`, any reply files, and any partial `out.md` before re-invoking
   only the roles that did not finish.

## Exit code, report, and failure tags

The exit code is council-level and tolerant of partial failure: `0` when at
least one role responds, `1` only when every role fails, `2` for usage or
staging errors. Treat the shell status as transport status and read the
report Summary and the sentinel's `ok=N total=M exit=X` fields.

Failed-role messages for recognized classes start with a bracketed tag:
`[auth]`, `[retriable:rate-limit]`, `[retriable:5xx]`, `[retriable:stall]`,
`[stall]`, `[orchestrator-exception]`, or `[orchestrator-bug]`. Unrecognized
failures carry the raw stderr untagged.

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
[codex-council] still running after 1240s: completed=1/3; active=2 (architect quiet=41s, prober retry-wait); queued=0; watchdog=1800s; version=0.10.0.
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
