# codex-council

Independent cross-model verification and collaboration for Claude Code.
Claude identifies the claims, decisions, and results worth checking,
supplies the requirements and evidence, and briefs **one or more role-framed
OpenAI Codex agents** to challenge them from task-specific lenses. Roles can
also investigate, reproduce, test, research, or implement authorized work.
Claude checks their evidence, reconciles their contributions into one
result, and stays responsible for it.

The plugin is general-purpose, with a programmatic center of gravity:
project implementation, computer science, software and ML/AI engineering,
DevSecOps, debugging and testing, and technical research. There is no
built-in role catalog: Claude composes each panel from the work in front of
it, and one role is often enough. Model routing is adaptive: each Codex role
runs on a model and reasoning effort discovered at runtime for its lens, or
on your native Codex configuration. Claude itself keeps the host session's
model and effort; council routing controls only the external Codex workers.

![d00-context: the user, Claude Code with the skill, the council runner, the Codex workers, and the shared workspace, top to bottom](docs/diagrams/d00-context.png)

*d00-context — Council in context. Claude briefs the runner, the runner
dispatches one Codex worker per role, and Claude reconciles what comes back.
Source: [d00-context.mmd](docs/diagrams/d00-context.mmd).*

## Requirements

- [Claude Code](https://claude.ai/code) 2.1.x, signed in (`claude` works in
  your terminal).
- [OpenAI Codex CLI](https://learn.chatgpt.com/docs/codex/cli) 0.158 or
  later, signed in (`codex` works in your terminal).
- Python 3.12 or later on macOS or Linux. The runner uses only the standard
  library and POSIX process groups.

## Install

```bash
claude plugins marketplace update
claude plugins marketplace add ehzawad/codex-council
claude plugins install codex-council@codex-council
```

The plugin stays installed across sessions.

## Usage

Invoke the skill directly:

```
/codex-council:codex-council
```

or ask in plain words:

```
ask codex council
codex council review
ask the codex coterie
codex coterie review
ask the codex team
reconcile with the codex team
```

Nearby wording such as "agent team" or "fan out to agents" is read from the
conversation around it. Only a request that could equally mean the Codex
council or Claude Code's own subagents gets one short question; under
`claude -p` Claude states the ambiguity instead of asking.

On every invocation Claude:

1. reads the live work: the goal, what is in flight, what is failing or
   uncertain, and which of its own claims most need an independent check;
2. sizes the panel to the work, with no default count: one role for a
   focused bug, review, or question, more only when each added role brings a
   lens the others would not;
3. runs metadata-only model discovery and picks each role's model and effort
   (see [Model and effort per role](#model-and-effort-per-role));
4. announces the panel and launches it, with no approval gate;
5. follows the run, reads each role's reply as it lands, and reconciles one
   answer once the council has ended.

Roles do not message each other: Claude gives every role the same context,
reconciles their replies, and stages material findings into a follow-up
round when one is needed. All roles share your working tree, so when several
roles may edit files, one role owns the writes and the others inspect, test,
or propose. The council runs detached from Claude Code, so a long one is not
cut off by the host's limits on background tasks; it keeps running (and
spending) until it finishes or Claude cancels it, and Claude follows it
until it ends.

Claude's operating procedure is
[`SKILL.md`](plugins/codex-council/skills/codex-council/SKILL.md). Its
references cover
[panel design](plugins/codex-council/skills/codex-council/references/panel-design.md),
[context staging](plugins/codex-council/skills/codex-council/references/context-staging.md),
and [runtime behavior](plugins/codex-council/skills/codex-council/references/runtime-behavior.md).

## How a run works

![d10-components: Claude Code, the runner detached by --start, the follower, the host task tracker (attached runs only), the run directory with its supervisor lock, saved threads, codex app-server, codex exec, and the workspace](docs/diagrams/d10-components.png)

*d10-components — Runtime components and ownership. The runner talks to
Codex; Claude and the runner hand work to each other through the run
directory; a released supervisor lock and a vanished runner (or, for an
attached run, the end of its launch command) say the run is over. Source:
[d10-components.mmd](docs/diagrams/d10-components.mmd).*

1. **Stage.** Claude creates a private directory with `mktemp -d`, runs
   `--discover` there (it writes `model-snapshot.json`), then writes
   `roles.json` and `context.md`.
2. **Preflight.** `--check-staging-dir` runs as its own foreground call and
   refuses anything the launch would refuse, before any worker exists.
3. **Launch.** A separate foreground call runs `--start`, which checks the
   directory again, claims it (`supervisor.lock`, `err.log`, and `out.md`,
   each created exclusively), starts the runner detached in a session of
   its own, and returns within seconds with the exact `--follow`,
   `--status`, and `--cancel` commands. The runner holds `supervisor.lock`
   locked for its whole life, writes `supervisor.json` about itself,
   resolves each role's model and effort, and runs one `codex exec` per
   role, at most `CODEX_COUNCIL_MAX_PARALLEL` at a time.
4. **Follow.** A read-only follower (`--follow`) relays the actionable lines
   of `err.log` to Claude, and each role's reply lands in `replies/` as the
   role settles. The runner also keeps `status.json` current, so the
   follower reports within seconds a runner that has died; for a runner
   that is still present but has stopped publishing status ticks, it warns
   after 120 seconds and stops at 300. After ten quiet minutes it prints a
   `still running` line, so a long council keeps showing progress.
   `--cancel` stops the council.
5. **Reconcile.** When the follower exits and `--status` confirms the
   runner has ended (its lock is free and its process gone), Claude reads
   `out.md` and reconciles one result for you.

| Run directory entry | Written by | Holds |
|---|---|---|
| `model-snapshot.json` | `--discover` | this run's model catalog and what routing may do |
| `roles.json`, `context.md` | Claude | the panel and the shared brief |
| `supervisor.lock` | `--start`; held locked by the runner while it lives | the runner's lifetime: held means running; never removed |
| `supervisor.json` | the runner, before any other work | who the runner is (pid, start time, process group), for `--status` and `--cancel` |
| `out.md` | `--start` creates it; the runner's stdout | the final report |
| `err.log` | `--start` creates it; the runner's stderr | progress lines, the heartbeat, and the final `CODEX_COUNCIL_DONE` line |
| `replies/<role>.md` | the runner, as each role settles | that role's section of the report |
| `status.json` | the runner | runner and role liveness, for `--follow`, `--status`, `--cancel`, and `--reap` |

The skill has no fallback launch. The runner still accepts an attached
launch (`--roles-file` and `--context-file`, with `out.md` and `err.log`
created by shell redirects and no supervisor files) for direct CLI use, and
`--follow`, `--status`, and `--reap` still read the directories such runs,
or earlier versions of the skill, left behind. Inside a Claude Code
background task an attached launch is stopped at the host's time limit, so
the skill never uses it (see
[runtime behavior](plugins/codex-council/skills/codex-council/references/runtime-behavior.md)).

Every mechanism has its own section and diagram in [DESIGN.md](DESIGN.md);
the [diagram index](#diagrams) below lists them all.

## Configuration

### Environment variables

| Variable | Default | Effect |
|---|---|---|
| `CODEX_COUNCIL_MODEL_ROUTING` | `auto` | `off` turns automatic model and effort selection off; explicit pins still apply. Any other value exits 2. |
| `CODEX_COUNCIL_MAX_PARALLEL` | `6` | How many roles run at once (a positive integer). Larger panels queue; Codex's own configuration does not change this. |
| `CODEX_COUNCIL_STALL_SECS` | `1800` | The output-inactivity watchdog: seconds of silence on a role's stdout and stderr before that attempt is stopped. A positive integer overrides it; 0 disables it (runner monitoring and the post-exit drain still apply). |
| `CODEX_COUNCIL_SESSION_KEY` | unset | An explicit scope for saved role threads (see [Saved role threads](#saved-role-threads)). |
| `XDG_STATE_HOME` | `~/.local/state` | Saved role threads live in `$XDG_STATE_HOME/codex-council/`. |

The council has no total elapsed-time or run-level deadline: a role may run
as long as its Codex process keeps writing output, and a council takes as
long as its slowest role. The watchdog measures output bytes, not progress.

Claude Code bounds background tasks, which is why a council does not run as
one. Since Claude Code 2.1.285 a background command stops at its time
limit, 30 minutes by default and 2 hours at most, however much it prints
(see the [changelog](https://code.claude.com/docs/en/changelog)), and
Claude Code can also stop background shells under memory pressure once a
session has sat idle for half an hour; that stop is yours to turn off with
`CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP` (see the
[environment variables](https://code.claude.com/docs/en/env-vars)), and the
plugin never sets it. A council started with `--start` is outside both: it
ends when it finishes, when Claude runs `--cancel`, or when something
outside Claude Code stops its processes (a reboot, for example). The
skill always uses `--start` and never falls back to a background launch.
An attached run (the runner started directly with `--roles-file`, or one
an earlier version launched) inside a background task is subject to both,
and so lasts at most 2 hours.

### Model and effort per role

Before writing `roles.json`, Claude runs a bounded, metadata-only discovery
that asks your installed Codex which models it advertises and how your
native configuration resolves. It starts no Codex thread or turn. Claude
then picks, per role, the first step that applies:

1. the model or effort you named, pinned exactly as given;
2. a model and effort pair from this run's catalog whose descriptions fit
   the role;
3. an effort adjustment on your proven native model;
4. your native configuration unchanged.

Each role records where its values came from in a `selection` object:

| Role JSON | What the runner sends | Checked against |
|---|---|---|
| no `model`, `effort`, or `selection` | nothing (native inheritance) | nothing |
| `model` and/or `effort` with `"selection": {"mode": "user"}` | the pin, unchanged | nothing; advisory notes only |
| `model` and `effort` with `"selection": {"mode": "routed", "snapshot_id": ..., "reason": ...}` | `-m <model>` and the effort | this run's snapshot, then the launch's fresh discovery |
| `effort` only, with `"selection": {"mode": "native_effort", "snapshot_id": ..., "reason": ...}` | `-m <proven native model>` and the effort | this run's snapshot, then the launch's fresh discovery |

For example, one role object (synthetic ids; real ones are copied from the
discovery summary):

```json
{
  "id": "parser-boundary-checks",
  "label": "Boundary checks",
  "instruction": [
    "Verify the claim that every parser path rejects an empty header.",
    "If nothing material falls in your lens, say so clearly.",
    "Thoroughness beats speed."
  ],
  "model": "future-vega-2033",
  "effort": "brisk",
  "selection": {
    "mode": "routed",
    "snapshot_id": "0ddc08d1899d8bb5",
    "reason": "narrow bounded check; the catalog describes this model for fast checks"
  }
}
```

A model and an effort share one grammar, `^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$`,
with case preserved. `inherit` and `default`, in any case, are refused as
model values: inheritance is omission. A `model` or `effort` without a
`selection` is refused (exit 2). A pin you asked for is never replaced or
rejected for being absent from the catalog, so a custom provider's model ids
keep working; discovery only adds advisory notes to it. If Codex cannot
answer discovery, the summary then says to write no automatic selections:
explicit pins still apply, and every other role inherits. `codex exec`
reports neither the model nor the effort that served a turn, so every report
states what the council sent, never what ran.

### Native configuration

A role that inherits sends no `-m` and no `-c model_reasoning_effort=...`,
so Codex resolves the model and effort itself from its own configuration
layers (see [Config basics](https://learn.chatgpt.com/docs/config-file/config-basic)).
No model is hardcoded anywhere in the plugin.

- **Where workers run.** Every worker runs as `codex exec -C <root>`, where
  `<root>` is the Git top level of the directory the council was launched
  from (or that directory outside Git), with the `codex` on your `PATH`, the
  runner's own environment including `CODEX_HOME`, and no `--profile`.
  Launch the council from the project whose Codex configuration you want it
  to use.
- **Subdirectory layers.** Discovery reads the configuration Codex resolves
  at that same root; that was verified live to ignore a
  `.codex/config.toml` below the root, even from a subdirectory launch.
  That workers ignore it too follows from Codex's documentation of `-C` but
  is not verified live.
- **`CODEX_API_KEY`.** `codex exec` honors it but the app-server that
  discovery talks to does not, so while it is set only explicit pins and
  inheritance are used.
- **Managed configuration.** Managed new-thread defaults, a managed layer
  that outranks command-line flags and sets the model or effort, and a
  managed provider or catalog setting each turn automatic selection off,
  because Codex would replace or reinterpret what the council sends. An
  explicit pin is still sent, and on such a machine it may not be the value
  that runs.

### Saved role threads

Each role keeps its Codex thread per project, host session, and role id, in
`$XDG_STATE_HOME/codex-council/`. Claude names each role id for its task, so
a later council starts fresh roles and sees only the context Claude stages
for it; Claude reuses a role id only when that role's own earlier work
helps, and the reused id resumes the role's thread. A saved thread Codex no
longer has restarts the role fresh, with a `prior continuity lost` warning
in its reply. The scope is chosen in this order:

| When | Scope |
|---|---|
| `CODEX_COUNCIL_SESSION_KEY` is set | that key: every terminal with the same value shares the role threads |
| a host session id is detected (Claude Code's session, `CODEX_THREAD_ID`, `TERM_SESSION_ID`, `TMUX_PANE`, `STY`, or `VSCODE_PID`) | that session |
| none is detectable | the whole project |

Integrated terminals in the same VS Code window share `VSCODE_PID`, so set
`CODEX_COUNCIL_SESSION_KEY` to keep them apart. A role id of 32 characters
or fewer names its state and reply files directly; a longer one is hashed.
State holds the thread id and bookkeeping, never a model, effort, or
selection, so a routed choice never becomes a role's default later. Saved
threads do not expire, and leaving a role out of a council does not retire
its thread.

## Results and failures

The launch exits `0` when at least one role responded and the report was
written, `1` when every role failed or the runner could not finish, and `2`
for a usage or staging error. `out.md` opens with a Summary line per role,
then each role's section; `replies/<role>.md` holds the same section, so an
early read and the final report cannot disagree. After an interruption no
complete report is guaranteed, but every reply already written survives.

A failed role's message starts with a tag:

| Tag | Meaning | What happens |
|---|---|---|
| `[auth]` | Codex could not authenticate | Not retried; the saved thread is kept. Sign in again, then re-run. |
| `[quota]` | a usage, quota, or credit limit, even one sent as HTTP 429 | Not retried. When the usage limit is for one model, the message ends with the same next step as a model rejection, so a routed role can re-run on your native configuration instead of waiting for the reset. |
| `[retriable:rate-limit]`, `[retriable:5xx]` | a rate limit or server error | Retried after 5 seconds; a role gets two attempts in all. |
| `[retriable:stall]` | the watchdog stopped a role before it began any tool work | Retried the same way, within the same two attempts. |
| `[stall]` | the watchdog stopped a role after tool work began | Not retried, because a replay could repeat side effects. |
| `[model-rejected]` | Codex refused the model that invocation sent | Not retried, nothing substituted, the saved thread kept. The message ends with one next step: re-run the role with `model`, `effort`, and `selection` omitted (a routed model), change or remove the pin (a user pin), or ask you to update your Codex configuration or name a model to pin (your natively configured model). A routed or pinned model that discovery proved is the native model gets that last step too. |
| `[orchestrator-exception]` | the runner itself failed on that role | The other roles finish normally. |
| no tag | anything else, with the failure text collected from Codex | Not retried. |

## Security

Codex runs with `--dangerously-bypass-approvals-and-sandbox`: no approval
prompts and no filesystem sandbox. Every role has full read and write access
to your machine as your user, so it can inspect the project thoroughly. Do
not use this plugin on untrusted projects or untrusted input: a prompt
injection inside reviewed content, whether code, a Markdown draft, a CSV
header, or a research excerpt, can steer every role. Claude treats every
reply as untrusted evidence, never as instructions.

Model discovery reads metadata only. It keeps the account type and whether
OpenAI sign-in is required, never an email address, plan, account id, or
token, and the app-server's own error output never reaches any file or
message. Catalog text is treated as data and printed with control
characters escaped.

## Diagrams

Each diagram's Mermaid source sits next to its PNG in
[`docs/diagrams/`](docs/diagrams/); [DESIGN.md](DESIGN.md) shows each one
beside the mechanism it explains, and
[`docs/codex-council.pdf`](docs/codex-council.pdf) collects these documents
with every diagram.

| Id | Level | Shows |
|---|---|---|
| [d00-context](docs/diagrams/d00-context.png) | 0 | who does what |
| [d10-components](docs/diagrams/d10-components.png) | 1 | runtime components and who owns each |
| [d11-modules](docs/diagrams/d11-modules.png) | 1 | the runner's modules and their imports |
| [d20-discovery](docs/diagrams/d20-discovery.png) | 2 | model discovery |
| [d21-choose](docs/diagrams/d21-choose.png) | 2 | how Claude chooses a role's model and effort |
| [d22-resolve](docs/diagrams/d22-resolve.png) | 2 | authoring checks, then what the runner sends |
| [d23-staging](docs/diagrams/d23-staging.png) | 2 | staging, preflight, and launch gates |
| [d24-fanout](docs/diagrams/d24-fanout.png) | 2 | bounded fan-out and role locks |
| [d25-continuity](docs/diagrams/d25-continuity.png) | 2 | saved role threads |
| [d26-attempt](docs/diagrams/d26-attempt.png) | 2 | one Codex process and its watchdog |
| [d27-stall](docs/diagrams/d27-stall.png) | 2 | what a stalled attempt becomes |
| [d28-failures](docs/diagrams/d28-failures.png) | 2 | failure classes, retries, and saved threads |
| [d29-progress](docs/diagrams/d29-progress.png) | 2 | replies, the follower, and reconciliation |
| [d30-liveness](docs/diagrams/d30-liveness.png) | 2 | noticing a dead or stuck runner, and recovery |

## Development

```bash
git clone https://github.com/ehzawad/codex-council.git
cd codex-council
claude plugins marketplace add ehzawad/codex-council    # skip if already added
claude plugins install codex-council@codex-council       # skip if already installed
./scripts/dev-link.sh
# then start a new Claude Code session
```

`scripts/dev-link.sh` makes the installed plugin run from this checkout:

1. It links `~/.claude/plugins/cache/codex-council/codex-council/<version>/`
   to this repository's `plugins/codex-council/`, so edits there are live.
   The version must be one safe path component, or the script refuses.
2. It points `installPath` and `version` in
   `~/.claude/plugins/installed_plugins.json` at that link, because Claude
   Code loads whatever `installPath` says, not whatever sits in the cache
   (skipped, with a note, before the plugin is installed).
3. It removes cache entries for other versions.

Claude Code may replace the link with a fresh copy when a session starts, so
re-run the script after each session start, `claude plugins update`, or
version bump, or run it from a `SessionStart` hook in
`~/.claude/settings.json` (merge it into any existing `hooks.SessionStart`
list):

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "bash -lc 'mkdir -p \"$HOME/.claude/logs\"; log=\"$HOME/.claude/logs/codex-council-dev-link.log\"; \"/absolute/path/to/codex-council/scripts/dev-link.sh\" >>\"$log\" 2>&1; rc=$?; if [ \"$rc\" -ne 0 ]; then printf \"%s dev-link failed exit=%s\\n\" \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\" \"$rc\" >>\"$log\"; fi; exit 0'"
          }
        ]
      }
    ]
  }
}
```

The hook always exits 0, so a missing checkout never blocks a session, and
it logs failures to `~/.claude/logs/codex-council-dev-link.log`. Only this
startup hook fails open; the skill's launch and context recipes fail closed.

Two guards catch a stale pairing. Every discovery summary, preflight,
dispatch, heartbeat, and `CODEX_COUNCIL_DONE` line carries
`version=<plugin version>`, which shows which plugin actually ran. And
SKILL.md's commands pass `--skill-contract 4`: when the script's contract
epoch differs, the command is refused as a stale SKILL/script pair. In a
checkout, re-run `scripts/dev-link.sh` and restart the session; never change
the epoch to get past it.

The tests need no Codex install and no network: `tests/fake_codex.py` puts a
scripted `codex` on `PATH`. CI runs them on Python 3.12 through 3.15, with a
pinned ruff:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
uvx --from 'ruff==0.15.21' ruff check .
python3 tests/liveness_scenarios.py      # end-to-end liveness scenarios
CODEX_COUNCIL_LIVE_TESTS=1 python3 -m unittest tests.test_live_codex -v   # real Codex, a few turns
```

`scripts/build-docs.sh` rebuilds `docs/codex-council.pdf` from this README,
DESIGN.md, SKILL.md, and its references (it needs `uvx` and Chrome or
Chromium). In the PDF, links between these documents jump within it, other
repository links point at GitHub, and the headings are bookmarks.
`scripts/build-docs.sh --diagrams` first re-renders every
`docs/diagrams/<id>.png` from its `.mmd` source through mermaid.ink, on an
opaque white background.

## License

MIT
