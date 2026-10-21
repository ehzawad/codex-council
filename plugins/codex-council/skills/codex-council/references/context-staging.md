# Context at an invocation boundary

A fresh invocation may occur after one turn, ten turns, or a material tool result.
No turn counter determines usefulness. Capture the current decision and relevant
artifacts, not an arbitrary number of recent messages.

Write context.md with the current objective, acceptance criteria and authorization
first. Then describe the contribution that could change the next decision, the
claims to challenge, strongest contrary evidence, relevant artifacts and setup,
in-flight work, and unknowns. Keep source facts, inference and proposals distinct.
Previous council conclusions retain their original snapshot and limitations.

A Codex thread does not inherit the host conversation. After host compaction,
reread durable decisions and relevant live artifacts; acknowledge missing history.
Provide file-relative locations within the captured input scope rather than asking
a worker to follow historical absolute paths back into the changing live tree.

Choose capture scope from the question, including the tests, configurations and
fixtures needed to decide it. Git tracked/untracked selection is a convenience,
not proof that all dependencies were included. Non-Git capture uses explicit paths.
Omitted credentials/configuration/caches are recorded. A missing dependency is a
coverage gap; do not fabricate a passing or failing empirical result.

Pause overlapping managed writers only during preparation. Capture refuses observed
changes, but cannot make arbitrary external writers atomic. After preparation,
Claude can continue independent live work while roles experiment in their copies.
The baseline and role contracts are frozen; changing the context or authorization
requires another invocation. Model selection fields alone can be filled after
actual-cwd discovery.

At reconciliation, --freshness checks captured filesystem scope, not the user's
current intention or external service state. Compare both the current objective
and artifacts. Recheck affected findings after changes; if the dependency scope is
uncertain, recapture rather than claiming an old result proves current correctness.

Exact input shapes and capture limitations are in
[invocation-contract.md](invocation-contract.md).
