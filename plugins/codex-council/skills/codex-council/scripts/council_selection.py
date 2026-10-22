"""Per-role model selection: one resolver for preflight and launch.

A role inherits its model settings (no model, effort, or selection), carries
an explicit user pin, or carries a runtime-grounded choice ("routed" pair or
"native_effort") bound to this run's --discover snapshot. Authoring
defects exit 2 (_validate_selection_authoring); evidence that stops
supporting a valid automatic choice resolves it to native inheritance with
a reason (_resolve_selection). Nothing here ranks models or efforts: every
check is exact membership in the discovered data, which is untrusted text
and reaches output only through _report_inline (_log_inline in err.log).

This module also owns the Role object the resolver works on, the
`selection` object grammar of roles.json, the per-role orchestration shared
by the preflight and the launch (_resolve_run_selections: authoring
validation, then, at launch only, a fresh discovery in the role's own
scratch cwd for an automatically selected role), and the
text that reports each decision in err.log, out.md, reply files, and the
preflight plan. An independently requested service tier is opt-in and checked
against the resolved model; it is never selected or discarded by routing.
"""

import collections
import re
import time
from dataclasses import dataclass, replace

from council_common import (
    LINEBREAK_CHARS,
    _log_inline,
    _report_inline,
    _roles_usage_exit,
    _utc_iso,
)
from council_discovery import (
    MODEL_ROUTING_ENV,
    _SNAPSHOT_ID_RE,
    _discover,
    _read_snapshot,
    _retirement_passed,
    _service_tier_id,
)

# Shape only (no value list; the snapshot decides membership): safe in argv,
# the -c TOML string, reports and reply headers; case preserved. \Z (not $)
# for the same trailing-newline reason as ROLE_ID_PATTERN in codex_council.py.
SELECTION_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]*\Z")
# Model values a human might mean as "inherit", but which Codex would
# receive as a literal model id (compared case-insensitively). Inheritance
# is omission. Efforts are not checked against these words: they are opaque
# per-model values, so an automatic effort must be one the catalog
# advertises and a pinned effort is forwarded as written.
RESERVED_MODEL_VALUES = ("inherit", "default")
SELECTION_MODES = ("user", "routed", "native_effort")
# The runtime-grounded modes, bound to this run's --discover snapshot.
AUTOMATIC_MODES = ("routed", "native_effort")
INHERIT_HINT = "omit model, effort, and selection to inherit native configuration"
# A malformed value in an explicit user pin (for example a display name
# with a space) is repaired with the user, never by dropping the pin.
USER_PIN_VALUE_HINT = (
    "for an explicit user pin write the exact value the --discover summary "
    "lists for what the user named (the execution id beside a display "
    "name), or ask the user for it; keep the pin (selection.mode 'user') "
    "and never drop it to inherit"
)
ROUTING_OFF_NOTE = f"{MODEL_ROUTING_ENV}=off"
PROVENANCES = ("native", "user", "routed", "native_effort", "fallback")
UNVERIFIED_MODEL_ADVISORY = (
    "unverified: not in the discovered catalog; forwarded unchanged"
)
PARTIAL_PIN_ADVISORY = (
    "partial pin: Codex ignores managed new-thread model and effort "
    "defaults when either is overridden"
)
# The report states only what the council SENT: codex exec's JSON stream
# names neither the model nor the effort that served a turn.
NO_AUTOMATIC_SELECTIONS = "no runtime-grounded selections"
MODEL_SELECTION_CAVEAT = (
    "codex exec does not report the model or effort that served a turn; "
    "values above are what the council sent, and \"native inheritance\" "
    "means no model or effort override was sent."
)


@dataclass(frozen=True)
class Selection:
    """A role's validated `selection` object (see _parse_selection).

    mode "user" is an explicit user pin; "routed" and "native_effort" are
    runtime-grounded choices bound to this run's discovery snapshot.
    """
    mode: str
    snapshot_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class SelectionDecision:
    """How one role's model and effort resolve (see _resolve_selection).

    `provenance` is what the council does (native, user, routed,
    native_effort, or fallback when an automatic request resolved to native
    inheritance). Dispatch uses ONLY dispatch_model and
    dispatch_effort and dispatch_service_tier (None = that override is not
    sent); the requested values are kept for reporting. `note` is a fallback reason or a user-pin
    advisory. `native_model` is the native model the resolving evidence
    proves (None when unproven or absent). The field is advisory and is not
    independently dispatched (native_effort resolution can place the same
    value in dispatch_model); it tells a refusal of a sent model that equals
    it (a routed or pinned model that is the native one) that an inheriting
    re-run would send it again.
    """
    provenance: str
    requested_model: str | None = None
    requested_effort: str | None = None
    dispatch_model: str | None = None
    dispatch_effort: str | None = None
    reason: str | None = None
    note: str | None = None
    native_model: str | None = None
    dispatch_service_tier: str | None = None


@dataclass(frozen=True)
class Role:
    """One council role from roles.json: the object this module resolves.

    `model` and `effort` are the requested per-role Codex overrides exactly
    as authored (None = not requested; omitting model, effort, and
    selection inherits Codex's native model settings), and `selection`
    declares their provenance. What is actually sent is `decision`,
    resolved once at launch before fan-out (see _resolve_run_selections
    and _role_decision).
    """
    id: str
    label: str
    instruction: str
    model: str | None = None
    effort: str | None = None
    selection: Selection | None = None
    decision: SelectionDecision | None = None
    execution: dict | None = None
    workdir: str | None = None
    evidence_dir: str | None = None
    execution_overrides: tuple = ()
    task_id: str | None = None
    input_snapshot: str | None = None
    # Independent of model/effort provenance: only an explicit user request
    # may populate this field, including alongside a routed selection.
    service_tier: str | None = None


_INHERIT_DECISION = SelectionDecision("native")


# ---------- the roles.json selection object ----------

def _parse_role_selection(entry, ctx):
    """Validate a role's model, effort, and selection; return
    (model, effort, Selection or None).

    A 'model' or 'effort' key with no 'selection' is refused first, before
    its value is checked, so the first error for an untagged malformed pin
    asks for its provenance instead of pointing at inheritance.
    """
    if "selection" not in entry and ("model" in entry or "effort" in entry):
        _roles_usage_exit(
            f"--roles-file {ctx}: 'model'/'effort' without 'selection': "
            "declare selection.mode: 'user' for an explicit user "
            "request, 'routed' or 'native_effort' for a runtime-grounded "
            f"choice; or {INHERIT_HINT}."
        )
    model = _validate_optional_role_field(entry, "model", ctx)
    effort = _validate_optional_role_field(entry, "effort", ctx)
    return model, effort, _parse_selection(entry, model, effort, ctx)


def _validate_optional_role_field(entry, field, ctx):
    """Return a validated model/effort/tier value, or None when omitted.

    A grammar failure in an explicit user pin (selection.mode 'user') says
    how to repair the pin; any other says how to inherit.
    """
    if field not in entry:
        return None
    value = entry[field]
    if not isinstance(value, str) or not SELECTION_VALUE_PATTERN.match(value):
        selection = entry.get("selection")
        user_pin = (isinstance(selection, dict)
                    and selection.get("mode") == "user")
        hint = ("use the exact service tier the user requested; "
                "never silently drop the request" if field == "service_tier" else
                USER_PIN_VALUE_HINT if user_pin else INHERIT_HINT)
        _roles_usage_exit(
            f"--roles-file {ctx}: optional field {field!r} must be a "
            "non-empty string matching "
            f"{SELECTION_VALUE_PATTERN.pattern} (got {value!r}); "
            f"{hint}."
        )
    if field == "model" and value.lower() in RESERVED_MODEL_VALUES:
        _roles_usage_exit(
            f"--roles-file {ctx}: model {value!r} is not an inheritance "
            f"value; {INHERIT_HINT}."
        )
    return value


def _validate_selection_reason(reason, ctx):
    """selection.reason: a non-empty, single-line string (no length cap)."""
    if not isinstance(reason, str) or not reason.strip():
        _roles_usage_exit(
            f"--roles-file {ctx}: selection.reason must be a non-empty "
            "single-line string."
        )
    if any(ch in reason for ch in LINEBREAK_CHARS):
        _roles_usage_exit(
            f"--roles-file {ctx}: selection.reason must not contain newlines."
        )


def _parse_selection(entry, model, effort, ctx):
    """Validate a role's `selection` object; return a Selection, or None to
    inherit.

    {"mode": "user"} (optionally with "reason") is an explicit pin and
    needs model, effort, or a separate service_tier. A routed selection
    needs model and effort; {"mode": "native_effort", "snapshot_id", "reason"} needs
    effort and forbids model (the runner pins the proven native model). No
    selection means inheritance: a model or effort without one was already
    refused (see _parse_role_selection).
    """
    if "selection" not in entry:
        return None
    value = entry["selection"]
    if not isinstance(value, dict):
        _roles_usage_exit(
            f"--roles-file {ctx}: 'selection' must be an object with a "
            "'mode'."
        )
    mode = value.get("mode")
    if mode not in SELECTION_MODES:
        _roles_usage_exit(
            f"--roles-file {ctx}: selection.mode must be 'user', 'routed', "
            f"or 'native_effort' (got {mode!r}); to inherit, omit model, "
            "effort, and selection."
        )
    if mode == "user" and "snapshot_id" in value:
        _roles_usage_exit(
            f"--roles-file {ctx}: selection.snapshot_id is only for 'routed' "
            "and 'native_effort'; an explicit user pin is not bound to a "
            "snapshot."
        )
    if mode == "user":
        allowed, shape = {"mode", "reason"}, "'mode' and an optional 'reason'"
    else:
        allowed = {"mode", "snapshot_id", "reason"}
        shape = "'mode', 'snapshot_id', and 'reason'"
    unknown = sorted(set(value) - allowed)
    if unknown:
        _roles_usage_exit(
            f"--roles-file {ctx}: selection has unknown field(s) "
            f"{', '.join(repr(k) for k in unknown)}; selection.mode "
            f"'{mode}' takes only {shape}."
        )
    if (mode == "user" and model is None and effort is None
            and "service_tier" not in entry):
        _roles_usage_exit(
            f"--roles-file {ctx}: selection.mode 'user' needs 'model' and/or "
            "'effort' or 'service_tier'; to inherit, omit model, effort, and selection."
        )
    if mode == "routed" and (model is None or effort is None):
        _roles_usage_exit(
            f"--roles-file {ctx}: selection.mode 'routed' needs both 'model' "
            "and 'effort', copied from this run's discovery snapshot."
        )
    if mode == "native_effort" and (model is not None or effort is None):
        _roles_usage_exit(
            f"--roles-file {ctx}: selection.mode 'native_effort' takes "
            "'effort' and no 'model' (the runner pins the proven native "
            "model at launch)."
        )
    snapshot_id = value.get("snapshot_id")
    if mode in AUTOMATIC_MODES and not (
        isinstance(snapshot_id, str) and _SNAPSHOT_ID_RE.fullmatch(snapshot_id)
    ):
        _roles_usage_exit(
            f"--roles-file {ctx}: selection.mode '{mode}' needs "
            "'snapshot_id', the snapshot_id --discover printed for this run "
            f"(got {snapshot_id!r})."
        )
    reason = value.get("reason")
    if mode in AUTOMATIC_MODES or "reason" in value:
        _validate_selection_reason(reason, ctx)
    return Selection(mode, snapshot_id, reason)


# ---------- one resolver for preflight and launch ----------

def _is_automatic(role):
    return role.selection is not None and role.selection.mode in AUTOMATIC_MODES


def _role_decision(role):
    """The decision attached at launch; without one (a Role built directly),
    the decision its request implies when no discovery evidence exists."""
    if role.decision is not None:
        return role.decision
    return _resolve_selection(role, None, None, "auto", None)


def _catalog_entry(snapshot, model):
    """The snapshot catalog entry whose dispatch id is exactly `model`."""
    for entry in snapshot["catalog"]["models"]:
        if entry["model"] == model:
            return entry
    return None


def _effort_advertised(entry, effort):
    """Exact, case-sensitive membership in the entry's advertised efforts."""
    return any(option["effort"] == effort for option in entry["efforts"])


def _catalog_alias(snapshot, model):
    """(kind, execution id) when `model` is exactly an entry's display name
    or picker id — neither is what `-m` receives — else None.

    Only an execution id that matches SELECTION_VALUE_PATTERN is offered: an
    id that could not be dispatched is no help, and the grammar keeps
    catalog text from carrying spaces, control characters, or a " reply="
    marker into a note.
    """
    for entry in snapshot["catalog"]["models"]:
        if not SELECTION_VALUE_PATTERN.match(entry["model"]):
            continue
        if model == entry["display_name"]:
            return "display name", entry["model"]
        if model == entry["catalog_id"]:
            return "picker id", entry["model"]
    return None


def _routed_evidence_gap(snapshot, model, effort, now, source=None):
    """Why `snapshot` does not support routing to (model, effort), or None.

    Exact checks only: the dispatch id is advertised and visible, its
    advertised retirement (if any) has not passed at `now` (retired when
    retirement_at <= now), and the effort is one that entry advertises.
    Catalog order and the recommended marker never matter. `source` names
    the evidence in the not-advertised text; the default, "snapshot <id>",
    is the planning snapshot an author can look up.
    """
    entry = _catalog_entry(snapshot, model)
    if entry is None:
        alias = _catalog_alias(snapshot, model)
        hint = f"; use the execution id '{alias[1]}'" if alias else ""
        source = source or f"snapshot {snapshot['snapshot_id']}"
        return (f"model '{model}' is not an advertised execution id in "
                f"{source}{hint}")
    if entry["hidden"]:
        return (f"cannot route to hidden model '{model}' (hidden models are "
                "for explicit user pins)")
    retirement = _retirement_passed(entry, now)
    if retirement:
        return f"model '{model}' advertised retirement passed ({retirement})"
    if not _effort_advertised(entry, effort):
        return f"effort '{effort}' is not advertised for model '{model}'"
    return None


def _native_model_gap(snapshot):
    """Why the snapshot proves no native model the runner could pin."""
    native = snapshot["native"]
    model = native["model"]
    if native["resolution"] != "proven":
        reason = native["reason"] or "native model not proven"
    elif not (isinstance(model, str) and SELECTION_VALUE_PATTERN.match(model)):
        reason = (f"native model id {model!r} is not a dispatchable "
                  "selection value")
    else:
        return None
    return f"cannot adjust effort on the native model: {reason}"


def _native_effort_gap(snapshot, effort, now, planned_model=None):
    """Why `snapshot` does not support `effort` on its proven native model.

    `planned_model` (launch only) is the native model of the planning
    snapshot, whose effort descriptions the choice was made from. An effort
    is never carried onto another model, even one that advertises the same
    spelling: the same name need not mean the same behavior. As for a
    routed choice, the native model's advertised retirement (if any) must
    not have passed at `now` (retired when retirement_at <= now; None
    skips the check): the runner would otherwise send that model with -m.
    """
    gap = _native_model_gap(snapshot)
    if gap:
        return gap
    model = snapshot["native"]["model"]
    if planned_model is not None and model != planned_model:
        return f"native model changed from '{planned_model}' to '{model}'"
    entry = _catalog_entry(snapshot, model)
    if entry is not None:
        retirement = _retirement_passed(entry, now)
        if retirement:
            return (f"native model '{model}' advertised retirement passed "
                    f"({retirement})")
    if entry is None or not _effort_advertised(entry, effort):
        return f"effort '{effort}' is not advertised for model '{model}'"
    return None


def _user_pin_advisory(role, evidence, now):
    """Advisory text for an explicit pin, or None; never a rejection.

    A pin reaches Codex unchanged whatever the catalog says (a custom
    provider's models are not in it); the snapshot only annotates it.
    `now` (or None to skip) is compared with the pinned model's advertised
    retirement. The effort checked against the model's advertised efforts
    is the pinned one, or, for a model-only pin with no managed defaults,
    the configured native effort Codex keeps (no effort override is sent,
    and Codex does not validate effort on the client).
    """
    if evidence is None:
        return None
    notes = []
    if evidence["status"] == "ok":
        entry = None
        if role.model is not None:
            entry = _catalog_entry(evidence, role.model)
            if entry is None:
                notes.append(UNVERIFIED_MODEL_ADVISORY)
                alias = _catalog_alias(evidence, role.model)
                if alias:
                    notes.append(f"'{role.model}' is the catalog {alias[0]} "
                                 f"of execution id '{alias[1]}'")
            else:
                retirement = _retirement_passed(entry, now)
                if retirement:
                    notes.append(
                        f"model '{role.model}' advertised retirement passed "
                        f"({retirement}); forwarded unchanged")
        elif evidence["native"]["resolution"] == "proven":
            entry = _catalog_entry(evidence, evidence["native"]["model"])
        if entry is not None and role.effort is not None:
            if not _effort_advertised(entry, role.effort):
                notes.append(
                    f"unverified effort: '{role.effort}' is not advertised "
                    f"for model '{entry['model']}'; forwarded unchanged"
                )
        elif entry is not None:  # a model-only pin
            inherited = evidence["configured"]["effort"]
            # With managed defaults present or unknown, the effort Codex
            # keeps is not known; a null configured effort means the
            # model's own default. The grammar keeps configuration text
            # out of the note, as for the ids _catalog_alias offers.
            if (
                inherited is not None
                and evidence["managed_defaults"]["status"] == "absent"
                and SELECTION_VALUE_PATTERN.match(inherited)
                and not _effort_advertised(entry, inherited)
            ):
                notes.append(
                    "unverified effort: inherited native effort "
                    f"'{inherited}' is not advertised for model "
                    f"'{entry['model']}'; no effort override sent"
                )
    partial = (role.model is None) != (role.effort is None)
    if partial and evidence["managed_defaults"]["status"] != "absent":
        notes.append(PARTIAL_PIN_ADVISORY)
    return "; ".join(notes) or None


def _proven_native_model(evidence):
    """The native model `evidence` proves, or None (no evidence, or the
    native model is not proven there)."""
    if evidence is None or evidence["native"]["resolution"] != "proven":
        return None
    return evidence["native"]["model"]


def _decision(role, provenance, model=None, effort=None, note=None,
              native_model=None):
    """A SelectionDecision that keeps the role's request for reporting."""
    reason = role.selection.reason if role.selection else None
    return SelectionDecision(
        provenance, role.model, role.effort, model, effort, reason, note,
        native_model,
    )


def _resolve_selection(role, planning, launch, routing_mode, now):
    """Decide what one role sends: the single resolver for preflight and
    launch. Pure — the same inputs always give the same decision.

    `planning` is this run's --discover snapshot and `launch` the fresh
    launch-time one (either may be None; preflight passes no launch
    snapshot, so its decisions are the plan). Launch evidence wins when
    present; a native_effort choice also falls back when the launch native
    model is not the planning one its effort was chosen for. `now`
    ("YYYY-MM-DDTHH:MM:SSZ", or None to skip) is compared with advertised
    retirements. Authoring defects never reach here (see
    _validate_selection_authoring): an automatic choice that the evidence
    does not support resolves to native inheritance, with the reason, and
    an explicit pin is forwarded unchanged with advisories at most. A
    decision that sends a model also records the native model the evidence
    proves, so a refusal can tell whether that model was the native one.
    """
    selection = role.selection
    if selection is None:
        return _INHERIT_DECISION
    evidence = launch if launch is not None else planning
    if selection.mode == "user":
        return _decision(role, "user", role.model, role.effort,
                         _user_pin_advisory(role, evidence, now),
                         _proven_native_model(evidence))
    mode = selection.mode
    if routing_mode == "off":
        return _decision(role, "fallback", note=ROUTING_OFF_NOTE)
    if evidence is None:
        return _decision(role, "fallback",
                         note="no discovery snapshot for this run")
    stage = "launch discovery" if launch is not None else "discovery snapshot"
    if evidence["status"] != "ok":
        problems = ", ".join(evidence["problems"]) or "no detail recorded"
        return _decision(role, "fallback",
                         note=f"{stage} unavailable: {problems}")
    if mode == "routed":
        if not evidence["routing"]["eligible"]:
            reasons = "; ".join(evidence["routing"]["reasons"])
            return _decision(
                role, "fallback",
                note=f"{stage} reports routing unavailable: {reasons}",
            )
        # The launch snapshot is retained as launch-model-snapshot.json
        # evidence, not as a planning snapshot id: name the stage.
        gap = _routed_evidence_gap(evidence, role.model, role.effort, now,
                                   stage if launch is not None else None)
        model = role.model
    else:
        # native_effort pins the native model THIS evidence proves, and
        # only when it is still the one discovery planned with.
        planned = None
        if launch is not None and planning is not None:
            planned = planning["native"]["model"]
        gap = _native_effort_gap(evidence, role.effort, now, planned)
        model = evidence["native"]["model"]
    if gap:
        if launch is not None:
            gap = f"selection evidence changed since discovery: {gap}"
        return _decision(role, "fallback", note=gap)
    return _decision(role, mode, model, role.effort,
                     native_model=_proven_native_model(evidence))


def _authoring_problem(role, planning, planning_problem, now):
    """Why an automatic selection is an authoring defect against this run's
    planning snapshot, or None."""
    selection = role.selection
    if planning is None:
        return (
            f"selection.mode '{selection.mode}' requires this run's "
            f"discovery snapshot ({planning_problem}); run --discover on "
            "this run directory first, or omit model, effort, and "
            "selection to inherit"
        )
    if selection.snapshot_id != planning["snapshot_id"]:
        return (
            f"snapshot_id '{selection.snapshot_id}' does not identify this "
            f"run's discovery snapshot '{planning['snapshot_id']}'"
        )
    if selection.mode == "native_effort":
        gap = _native_model_gap(planning)
        if gap:
            return f"{gap}; omit effort and selection to inherit"
        return _native_effort_gap(planning, role.effort, now)
    if not planning["routing"]["eligible"]:
        return (
            "discovery reported routing unavailable "
            f"({'; '.join(planning['routing']['reasons'])}); omit model, "
            "effort, and selection to inherit"
        )
    return _routed_evidence_gap(planning, role.model, role.effort, now)


def _validate_selection_authoring(roles, planning, planning_problem,
                                  routing_mode, now):
    """Exit 2 (uniform rewrite recovery) on an automatic selection this
    run's planning snapshot does not support.

    Runs in preflight and at launch, before any worker. `now` is compared
    with advertised retirements: the current time in preflight, the
    planning snapshot's creation time at launch (see
    _resolve_run_selections). This check never rejects explicit model/effort
    pins against the catalog, and with routing off automatic roles are not errors: they
    resolve to native inheritance.
    """
    if routing_mode == "off":
        return
    for idx, role in enumerate(roles):
        if not _is_automatic(role):
            continue
        problem = _authoring_problem(role, planning, planning_problem, now)
        if problem:
            _roles_usage_exit(_report_inline(
                f"--roles-file entry {idx} (id '{role.id}'): {problem}."
            ))


# ---------- launch-time resolution ----------

def _validate_service_tier(role, decision, evidence, problem=None):
    """Fail closed on an opt-in tier; never drop it through routing fallback.

    Check the resolved dispatch model, or the proven current native model.
    Catalog gaps elsewhere and routing=off do not prevent this exact check.
    A pin suppresses managed new-thread defaults; effective-config inspection
    separately verifies that higher-precedence layers let its model/tier hold.
    A snapshot from before tiers were recorded has no service_tiers key: that
    is missing evidence, so it asks for a new --discover. A launch snapshot
    is not retained on this exit, so the refusal names discovery's problems.
    """
    if evidence is None or evidence["status"] != "ok":
        if evidence is not None:
            problem = ", ".join(evidence["problems"])
        gap = f"discovery unavailable ({problem or 'no detail recorded'})"
    else:
        blockers = [reason for reason in evidence["routing"]["reasons"]
                    if reason != ROUTING_OFF_NOTE and not reason.startswith((
                        "catalog incomplete: ", "managed new-thread defaults ",
                        "managed layer overrides CLI flags ("))]
        model = decision.dispatch_model or _proven_native_model(evidence)
        entry = _catalog_entry(evidence, model) if model else None
        if blockers:
            gap = "; ".join(blockers)
        elif model is None:
            gap = ("model cannot be established: "
                   + (evidence["native"]["reason"] or "native model unproven"))
        elif entry is None:
            alias = _catalog_alias(evidence, model)
            hint = f"; use the execution id '{alias[1]}'" if alias else ""
            gap = (f"model '{model}' is not an advertised execution id in "
                   f"this role's discovered catalog{hint}")
            if not evidence["catalog"]["complete"]:
                # A dropped malformed record looks absent; name the cause.
                gap += "".join(f" ({reason})" for reason in evidence["routing"]["reasons"]
                               if reason.startswith("catalog incomplete: "))
        elif "service_tiers" not in entry:
            gap = ("this role's model snapshot has no service-tier metadata; "
                   "rerun --discover on this run directory")
        elif _service_tier_id(role.service_tier) not in entry["service_tiers"]:
            gap = (f"not advertised for model '{model}' "
                   f"(request tier '{_service_tier_id(role.service_tier)}')")
        else:
            return
    _roles_usage_exit(_report_inline(
        f"--roles-file role '{role.id}': requested service tier "
        f"'{role.service_tier}': {gap}; no worker started. "
        "Refresh this role's discovery or choose a supported model/tier; "
        "do not silently remove an explicit user request."
    ))


def _launch_discovery_state(routing_mode, automatic, launch):
    """(state, reason) of launch discovery for err.log and the report.

    state is "ok", "unavailable", or "not-run"; reason is None or why.
    """
    if launch is None:
        if automatic and routing_mode == "off":
            return "not-run", ROUTING_OFF_NOTE
        return "not-run", NO_AUTOMATIC_SELECTIONS
    if launch["status"] != "ok":
        return "unavailable", (
            ", ".join(launch["problems"]) or "no detail recorded"
        )
    if not launch["routing"]["eligible"]:
        return "ok", launch["routing"]["reasons"][0]
    return "ok", None


def _discovery_sentence(state, reason, launch):
    """The discovery half of the report's Model selection paragraph."""
    if state == "not-run":
        return f"launch discovery not run ({reason})"
    if state == "unavailable":
        return f"launch discovery unavailable: {reason}"
    version = launch["context"]["codex_cli_version"] or "version unknown"
    return f"launch discovery ok (codex-cli {version})"


def _model_selection_lines(roles, routing_mode, state, reason):
    """The err.log lines that follow the dispatch line: one summary, then
    one line per role whose automatic choice fell back to inheritance."""
    decisions = [(role.id, _role_decision(role)) for role in roles]
    counts = collections.Counter(d.provenance for _, d in decisions)
    discovery = f"{state} ({reason})" if reason else state
    lines = [
        f"[codex-council] model selection: routing={routing_mode}; "
        f"discovery={discovery}; "
        + " ".join(f"{name}={counts[name]}" for name in PROVENANCES)
    ]
    lines += [
        f"[codex-council:{role_id}] routing fell back to native "
        f"inheritance: {decision.note}"
        for role_id, decision in decisions
        if decision.provenance == "fallback"
    ]
    return [_log_inline(line) for line in lines]


def _resolve_run_selections(roles, run_dir, routing_mode, at_launch, project_root=None):
    """Validate authoring, then attach every role's decision, for the
    preflight plan and the launch; returns (roles, launch snapshot or None).
    Callers pass one role, with its catalog directory as `run_dir`.

    The launch judges authoring as of the planning snapshot's creation (a
    retirement already passed then stays exit 2; a later one falls back to
    inheritance) and reads its resolve clock only after launch discovery.
    """
    planning, planning_problem = _read_snapshot(run_dir)
    if at_launch:
        authoring_now = planning["created_at"] if planning is not None else None
    else:
        authoring_now = _utc_iso(time.time())
    _validate_selection_authoring(
        roles, planning, planning_problem, routing_mode, authoring_now
    )
    for role in roles:
        if role.service_tier is not None:
            _validate_service_tier(role, _resolve_selection(
                role, planning, None, routing_mode, authoring_now
            ), planning, planning_problem)
    launch = None
    tier_requested = any(r.service_tier is not None for r in roles)
    automatic = routing_mode == "auto" and any(_is_automatic(r) for r in roles)
    if at_launch and (tier_requested or automatic):
        launch = (_discover(routing_mode) if project_root is None else
                  _discover(routing_mode, project_root=project_root))
    now = _utc_iso(time.time()) if at_launch else authoring_now
    resolved = []
    evidence = launch if at_launch else planning
    for role in roles:
        decision = _resolve_selection(role, planning, launch, routing_mode, now)
        if role.service_tier is not None:
            _validate_service_tier(role, decision, evidence)
            decision = replace(decision, dispatch_service_tier=role.service_tier,
                               native_model=_proven_native_model(evidence))
        resolved.append(replace(role, decision=decision))
    return resolved, launch


# ---------- selection text for reports and the preflight plan ----------

def _sent_values(model, effort, service_tier=None):
    """The model, effort and tier overrides present in a decision."""
    parts = []
    if model:
        parts.append(f"model {model}")
    if effort:
        parts.append(f"effort {effort}")
    if service_tier:
        parts.append(f"service tier {service_tier}")
    return ", ".join(parts)


def _selection_summary_note(decision):
    """The Summary-line note for what one role sent ("" = nothing sent)."""
    sent = _sent_values(decision.dispatch_model, decision.dispatch_effort,
                        decision.dispatch_service_tier)
    tier = (f"; service tier {decision.dispatch_service_tier}"
            if decision.dispatch_service_tier else "")
    if decision.provenance == "user":
        return f" (explicit: {sent})"
    if decision.provenance == "routed":
        return f" (routed: {sent})"
    if decision.provenance == "native_effort":
        return (f" (routed effort: {decision.dispatch_effort} on native "
                f"model {decision.dispatch_model}{tier})")
    if decision.provenance == "fallback":
        return f" (native inheritance; routing fell back{tier})"
    return f" (native model/effort inheritance{tier})" if tier else ""


def _selection_section_text(decision):
    """The `_Model selection: ..._` text every role section carries."""
    sent = _sent_values(decision.dispatch_model, decision.dispatch_effort,
                        decision.dispatch_service_tier)
    if decision.provenance == "user":
        text = f"explicit override — sent {sent}"
    elif decision.provenance == "routed":
        text = f"routed — sent {sent}"
    elif decision.provenance == "native_effort":
        text = (
            "routed effort on the native model — sent model "
            f"{decision.dispatch_model} (pinned native model), effort "
            f"{decision.dispatch_effort}"
        )
    elif decision.provenance == "fallback":
        requested = _sent_values(
            decision.requested_model, decision.requested_effort
        )
        return (f"native inheritance — routing fell back: {decision.note}; "
                f"requested {requested}")
    else:
        return "native inheritance (no model or effort override sent)"
    if decision.reason:
        text += f"; reason: {decision.reason}"
    if decision.note:  # a user-pin advisory
        text += f"; {decision.note}"
    return text


def _selection_plan_text(decision):
    text = _model_selection_plan_text(decision)
    if decision.dispatch_service_tier:
        revalidated = text.endswith("revalidated at launch")
        text += f"; explicit service tier {decision.dispatch_service_tier}"
        if not revalidated:
            text += "; revalidated at launch"
    return text


def _model_selection_plan_text(decision):
    """One role's preflight plan; automatic choices are revalidated."""
    sent = _sent_values(decision.dispatch_model, decision.dispatch_effort)
    if decision.provenance == "user":
        text = f"explicit override ({sent})" if sent else "native model/effort inheritance"
        return f"{text}; {decision.note}" if decision.note else text
    if decision.provenance == "routed":
        return f"routed ({sent}); revalidated at launch"
    if decision.provenance == "native_effort":
        return (f"native-model effort (effort {decision.dispatch_effort} on "
                f"native model {decision.dispatch_model}); revalidated at "
                "launch")
    if decision.provenance == "fallback":
        if decision.note == ROUTING_OFF_NOTE:
            return "native inheritance (routing off)"
        return f"native inheritance (routing fell back: {decision.note})"
    return "native inheritance"
