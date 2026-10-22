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

Choose models from each role's actual-cwd discovery snapshot. A syntactically
valid explicit user pin wins and is forwarded unchanged, even when it is absent
from the catalog. Otherwise a routed model+effort needs the current
catalog descriptions to support both values. Native-effort selection needs a
proven native model whose advertised retirement has not passed; otherwise omit
model, effort and selection. Do not infer capability from product names, id
spelling, version numbers, catalog order, recommended markers or remembered
reputations. The runner validates evidence,
not the semantic quality of Claude's choice.

The selection JSON shapes are:

```json
{"mode": "user"}
```

```json
{"mode": "routed", "snapshot_id": "0123456789abcdef", "reason": "The advertised capabilities fit this bounded contribution."}
```

```json
{"mode": "native_effort", "snapshot_id": "0123456789abcdef", "reason": "The native model's advertised effort fits this question."}
```

User mode needs model, effort, or service_tier. Routed mode needs model and effort;
native_effort needs only effort and the runner pins the proven native model. Model and effort values
must fully match `[A-Za-z0-9][A-Za-z0-9._:/@+-]*`; the model values `inherit` and
`default` (any case) are rejected. Routed choices must not name a model whose
advertised retirement has passed, and native_effort is refused when the proven
native model's advertised retirement has passed. These examples illustrate
shapes; copy the actual role-specific snapshot_id and advertised values at runtime.
Inheritance is omission, never literal inherit/default placeholders. Source `.codex`
configuration is not copied into scratch, so inspect the actual native
configuration rather than assuming source-layer defaults survived relocation.

Fast mode is an independent, explicit user request: add `"service_tier": "fast"`
to the role only when asked. It can accompany any model-selection mode, or stand
alone with no model/effort/selection to preserve the native model settings. A
routed model/effort never authorizes a tier change. Omission preserves the user's
native service tier; Fast consumes more usage, subject to model/account availability.

Tier values use the same safe grammar as model/effort. The runner checks exact
advertised tier ids in that role's snapshot; Codex's config value `fast` maps to
the request id `priority` (display names such as `Fast` are not ids). Missing tier
metadata proves no support. Unlike a model-only user pin, a tier request needs
a catalog entry for the resolved model, or a proven native model when no model
override is sent. Preflight and launch refuse unsupported or unprovable requests
with exit 2, including when routing falls back. Launch also reads effective
configuration with the worker's overrides to detect managed layers that replace
its model or tier. These checks observe configuration, not the tier that serves
the eventual turn. Reply headers and reports record the literal sent value
(`fast`, not `priority`); `result.json` uses `sent_service_tier`. All overrides are
per invocation, including resume, and never change the user's `config.toml`.

See [invocation-contract.md](invocation-contract.md) for full input/capability fields
and [runtime-behavior.md](runtime-behavior.md) for refusals and recovery. The user
receives one reconciled result, not a vote count or an unexamined transcript dump.
