# Local acceptance gate

The release candidate must remain unmerged until the actual installed Claude Code,
Codex, native configuration and OS behavior have been checked. Fake-protocol tests
prove the runner integration, not model quality or real sandbox enforcement.

## Checkout and offline verification

Use a clean clone or an unrelated worktree for reviewing the PR; do not overwrite
uncommitted user work. Check out the PR branch with GitHub CLI or Git. From its root:

```zsh
python3 --version
git --version
claude --version
codex --version
python3 -m unittest discover -s tests -p 'test_*.py' -v
uvx --from 'ruff==0.15.21' ruff check .
```

Record the OS version, actual host model/effort, CLI versions, plugin version,
contract epoch, and the exact commit tested. Do not record credentials or publish
raw configuration. Repeat on macOS and Ubuntu where available. Missing environments
remain unverified; one OS pass is not evidence for another.

Tests cover Git dirty/unstaged/untracked/deleted inputs, non-Git explicit scope,
source/scratch independence, symlinks/hardlinks/special files, context mutation,
actual-cwd model routing, explicit continuity, partial coverage, raw evidence,
transient failure after a side effect and detached cancellation. Existing process
liveness scenarios are included through unittest discovery.

## Real command boundaries (explicit inference opt-in)

These tests use the native signed-in Codex account and consume usage. They create
only private temporary fixtures and retain logs for local inspection. The probe
tries a write inside its scratch directory, a write to its own external canary,
and a connection to its own host-local TCP listener. No production files or remote
services are targets. Run only after the offline suite passes:

```zsh
CODEX_COUNCIL_LIVE_WORKFLOW=1 python3 -m unittest discover -s tests -p 'test_live_workflow.py' -v
```

Workspace-write should permit its scratch canary while denying the external write
and command-network connection. Read-only should deny both writes. Both require
actual command-protocol output from the probe, unchanged probe source, and intact
external canary. A model declining or failing to execute the probe is **not a pass**.
A config/read refusal is **not a reason to enable bypass**. Investigate documented
CLI/API compatibility and retain the failed gate until repaired.

The command sandbox is not a whole-Codex sandbox and does not promise hidden HOME
or restriction of native integrations. Verify external-tool inventory separately:
requested denied features and MCP entries must be absent/disabled, with no silent
fallback or native-tool activation. Managed policy overrides should refuse before
inference. Inspect scratch-native versus source-project model settings explicitly.

## Claude Code behavior

Start a fresh host session with this plugin checkout, without changing global
configuration:

```zsh
claude --plugin-dir ./plugins/codex-council
```

Use `tests/fixtures/invocation_cases.json` as behavioral scenarios. It is a small
reviewable case set, not an assumed native plugin-eval schema. A simple typo must
not trigger a council merely because a turn ended. A meaningful investigation or
explicit request should. Repeat after one, two and ten turns; after compaction;
with changed requirements; while live code changes after capture; and for a pure
context-only collaboration. Roles and prompts should follow the task, not a roster.

For a coding fixture, require a baseline, a discriminating experiment in scratch,
actual outputs, retained candidate artifacts and a freshness check. Missing setup
must become a coverage gap. A writer endorsing its own patch is not independent
verification. Deliberate integration requires a new check against current inputs.

Use Claude Code's installed plugin-evaluation tooling where suitable, including a
no-plugin baseline; inspect its current help/schema rather than assuming this JSON
is native eval input. Compare useful contributions, false findings, unnecessary
invocations, stale-result acceptance and unauthorized writes. Do not benchmark by
counting roles or praising a particular model name.

## Copy-pastable validation prompt

```text
Validate this Codex Council PR as a release candidate. Do not merge it, push to
main, change my native Claude/Codex configuration, or enable unrestricted bypass.
Record the exact commit, OS, CLI versions, host model/effort and plugin epoch.

First run the full offline unit/protocol/CLI/liveness suite and the pinned lint
check. Investigate failures against the implementation, not by weakening tests.
Then run the explicitly opt-in temporary-canary live workflow tests with my
signed-in Codex, recording actual commands/results and preserving private evidence.
Never count a fake-protocol pass as real sandbox validation, and never bypass a
refused policy to make a test pass.

Treat Codex as an adaptive collaborator. Invoke only when explicitly requested or
when an independent contribution could materially advance the current task,
resolve a consequential uncertainty, test competing explanations, or produce a
useful artifact. Do not invoke merely because a turn ended. Use no fixed role
catalog, model ranking, or mandatory debate.

Exercise current-context capture after one, two and ten turns, after compaction,
and while the live project changes after capture. Verify dirty/staged/untracked
Git inputs and non-Git scope, independent scratch writes, explicit fresh/resume
continuity, actual-cwd configuration discovery, evidence/claim separation, required
coverage, stale-result rejection, transient retry safety and cancellation cleanup.
Check a coding experiment and a context-only collaboration. No automatic patch
application is allowed.

Preserve useful dissent and label blocked or untested behavior explicitly. Report
confirmed defects with file:line evidence and minimal reproductions, what passed
on each actual host, remaining limitations, and a merge recommendation. Any fixes
belong on this PR branch, followed by rerunning the affected and full checks.
```

Retained run directories contain sensitive evidence. Inspect them locally and remove
them only after verified process exit. Never delete a live lock. Do not publish
transcripts, account data or filesystem paths without reviewing them.
