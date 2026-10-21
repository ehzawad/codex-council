# Task-shaped contributions and model selection

Identify what is uncertain, what evidence would decide it, and which independent
contribution could materially improve the next step. One contribution is enough
when it covers the question. Add another only for distinct useful work, not to fill
a persona roster or manufacture a majority. No fixed roles or model ranking exists.

A collaborator may investigate, reproduce, test, compare alternatives, research,
or build an authorized candidate. Frame its objective, likely failure modes,
discriminating evidence, and stopping condition. Keep methodological independence
where needed: implementing a candidate and endorsing that same candidate is not
independent verification. Preserve dissent until evidence resolves it.

Capabilities are not role names. An experimental test writer usually needs
workspace-write in its own captured copy. Read-only is appropriate for inspection;
network, native integrations and bypass require explicit authorization. A label
such as “reviewer” never grants or revokes permissions. Separate scratch copies
avoid overlapping file mutations but do not supply a complete OS security boundary.

Choose models from each role's actual-cwd discovery snapshot. An explicit user pin
wins and is forwarded unchanged. Otherwise a routed model+effort needs the current
catalog descriptions to support both values. Native-effort selection needs a
proven native model; otherwise omit model, effort and selection. Do not infer
capability from product names, id spelling, version numbers, catalog order,
recommended markers or remembered reputations. The runner validates evidence,
not the semantic quality of Claude's choice.

The existing selection JSON shapes remain:

```json
{"mode": "user"}
```

```json
{"mode": "routed", "snapshot_id": "0123456789abcdef", "reason": "The advertised capabilities fit this bounded contribution."}
```

```json
{"mode": "native_effort", "snapshot_id": "0123456789abcdef", "reason": "The native model's advertised effort fits this question."}
```

User mode needs model and/or effort. Routed mode needs both; native_effort needs
only effort and the runner pins the proven native model. These examples illustrate
shapes; copy the actual role-specific snapshot_id and advertised values at runtime.
Inheritance is omission, never literal inherit/default placeholders. Source `.codex`
configuration is not copied into scratch, so inspect the actual native
configuration rather than assuming source-layer defaults survived relocation.

See [invocation-contract.md](invocation-contract.md) for full input/capability fields
and [runtime-behavior.md](runtime-behavior.md) for refusals and recovery. The user
receives one reconciled result, not a vote count or an unexamined transcript dump.
