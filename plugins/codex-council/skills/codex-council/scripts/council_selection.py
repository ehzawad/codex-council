"""Per-role model selection: one resolver for preflight and launch.

A role inherits (no model, effort, or selection), carries an explicit user
pin, or carries a runtime-grounded choice ("routed" pair or
"native_effort") bound to this run's --discover snapshot. Authoring
defects exit 2 (_validate_selection_authoring); evidence that stops
supporting a valid automatic choice resolves it to native inheritance with
a reason (_resolve_selection). Nothing here ranks models or efforts: every
check is exact membership in the discovered data, which is untrusted text
and reaches output only through _report_inline (_log_inline in err.log).

This module also owns the Role object the resolver works on, the
`selection` object grammar of roles.json, the orchestration shared by the
preflight and the launch (_resolve_run_selections: authoring validation,
then at most one fresh discovery per council, at launch only), and the
text that reports each decision in err.log, out.md, reply files, and the
preflight plan.
"""

import collections
import re
import time
from dataclasses import dataclass, replace
from typing import Optional

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
)

# One grammar for model and effort values. Shape checks only: no value list
# is hardcoded — whether an automatic choice is advertised comes from this
# run's discovery snapshot, and an explicit pin reaches Codex unchanged. No
# leading "-" and no whitespace, control characters, quotes, backslashes,
# or angle brackets, so argv, the TOML string in
# -c model_reasoning_effort="<effort>", report lines, and reply-file headers
# are safe by construction. Case is preserved, never folded. \Z (not $) for
# the same trailing-newline reason as ROLE_ID_PATTERN in codex_council.py.
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
    "means no override was sent."
)


@dataclass(frozen=True)
class Selection:
    """A role's validated `selection` object (see _parse_selection).

    mode "user" is an explicit user pin; "routed" and "native_effort" are
    runtime-grounded choices bound to this run's discovery snapshot.
    """
    mode: str
    snapshot_id: Optional[str] = None
    reason: Optional[str] = None


@dataclass(frozen=True)
class SelectionDecision:
    """How one role's model and effort resolve (see _resolve_selection).

    `mode` is what the role asked for (inherit, user, routed,
    native_effort); `provenance` is what the council does (native, user,
    routed, native_effort, or fallback when an automatic request resolved
    to native inheritance). Dispatch uses ONLY dispatch_model and
    dispatch_effort (None = that override is not sent); the requested
    values are kept for reporting. `note` is a fallback reason or a user-pin
    advisory. `native_model` is the native model the resolving evidence
    proves (None when unproven or absent): it is never sent, and only tells
    a refusal of a sent model that equals it (a routed or pinned model that
    is the native one) that an inheriting re-run would send it again.
    """
    mode: str
    provenance: str
    requested_model: Optional[str] = None
    requested_effort: Optional[str] = None
    dispatch_model: Optional[str] = None
    dispatch_effort: Optional[str] = None
    reason: Optional[str] = None
    note: Optional[str] = None
    native_model: Optional[str] = None


@dataclass(frozen=True)
class Role:
    """One council role from roles.json: the object this module resolves.

    `model` and `effort` are the requested per-role Codex overrides exactly
    as authored (None = not requested; omitting model, effort, and
    selection inherits Codex's native configuration), and `selection`
    declares their provenance. What is actually sent is `decision`,
    resolved once at launch before fan-out (see _resolve_run_selections
    and _role_decision).
    """
    id: str
    label: str
    instruction: str
    model: Optional[str] = None
    effort: Optional[str] = None
    selection: Optional[Selection] = None
    decision: Optional[SelectionDecision] = None


_INHERIT_DECISION = SelectionDecision("inherit", "native")


# ---------- the roles.json selection object ----------

def _parse_role_selection(entry, ctx, require_selection):
    """Validate a role's model, effort, and selection; return
    (model, effort, Selection or None).

    On the skill path (require_selection, i.e. --skill-contract was passed)
    a 'model' or 'effort' key with no 'selection' is refused first, before
    its value is checked, so the first error for an untagged malformed pin
    asks for its provenance instead of pointing at inheritance.
    """
    if require_selection and "selection" not in entry and (
        "model" in entry or "effort" in entry
    ):
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
    """Return a validated model/effort value, or None when omitted.

    A grammar failure in an explicit user pin (selection.mode 'user', or
    no selection at all, which direct CLI use reads as a user pin) says how
    to repair the pin; any other says how to inherit.
    """
    if field not in entry:
        return None
    value = entry[field]
    if not isinstance(value, str) or not SELECTION_VALUE_PATTERN.match(value):
        selection = entry.get("selection")
        user_pin = "selection" not in entry or (
            isinstance(selection, dict) and selection.get("mode") == "user"
        )
        _roles_usage_exit(
            f"--roles-file {ctx}: optional field {field!r} must be a "
            "non-empty string matching "
            f"{SELECTION_VALUE_PATTERN.pattern} (got {value!r}); "
            f"{USER_PIN_VALUE_HINT if user_pin else INHERIT_HINT}."
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
    needs model and/or effort. {"mode": "routed", "snapshot_id", "reason"}
    needs both; {"mode": "native_effort", "snapshot_id", "reason"} needs
    effort and forbids model (the runner pins the proven native model). A
    model or effort with no selection is read as an explicit user pin; the
    skill path has already refused it (see _parse_role_selection).
    """
    if "selection" not in entry:
        if model is None and effort is None:
            return None
        return Selection("user")
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
    if mode == "user" and model is None and effort is None:
        _roles_usage_exit(
            f"--roles-file {ctx}: selection.mode 'user' needs 'model' and/or "
            "'effort'; to inherit, omit model, effort, and selection."
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


def _native_effort_gap(snapshot, effort, planned_model=None):
    """Why `snapshot` does not support `effort` on its proven native model.

    `planned_model` (launch only) is the native model of the planning
    snapshot, whose effort descriptions the choice was made from. An effort
    is never carried onto another model, even one that advertises the same
    spelling: the same name need not mean the same behavior.
    """
    gap = _native_model_gap(snapshot)
    if gap:
        return gap
    model = snapshot["native"]["model"]
    if planned_model is not None and model != planned_model:
        return f"native model changed from '{planned_model}' to '{model}'"
    entry = _catalog_entry(snapshot, model)
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


def _decision(role, mode, provenance, model=None, effort=None, note=None,
              native_model=None):
    """A SelectionDecision that keeps the role's request for reporting."""
    reason = role.selection.reason if role.selection else None
    return SelectionDecision(
        mode, provenance, role.model, role.effort, model, effort, reason, note,
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
    if selection is None and role.model is None and role.effort is None:
        return _INHERIT_DECISION
    evidence = launch if launch is not None else planning
    if selection is None or selection.mode == "user":
        # An untagged pin gets here only from direct CLI use.
        return _decision(role, "user", "user", role.model, role.effort,
                         _user_pin_advisory(role, evidence, now),
                         _proven_native_model(evidence))
    mode = selection.mode
    if routing_mode == "off":
        return _decision(role, mode, "fallback", note=ROUTING_OFF_NOTE)
    if evidence is None:
        return _decision(role, mode, "fallback",
                         note="no discovery snapshot for this run")
    stage = "launch discovery" if launch is not None else "discovery snapshot"
    if evidence["status"] != "ok":
        problems = ", ".join(evidence["problems"]) or "no detail recorded"
        return _decision(role, mode, "fallback",
                         note=f"{stage} unavailable: {problems}")
    if mode == "routed":
        if not evidence["routing"]["eligible"]:
            reasons = "; ".join(evidence["routing"]["reasons"])
            return _decision(
                role, mode, "fallback",
                note=f"{stage} reports routing unavailable: {reasons}",
            )
        # At launch the snapshot id is in memory only: name the stage.
        gap = _routed_evidence_gap(evidence, role.model, role.effort, now,
                                   stage if launch is not None else None)
        model = role.model
    else:
        # native_effort pins the native model THIS evidence proves, and
        # only when it is still the one discovery planned with.
        planned = None
        if launch is not None and planning is not None:
            planned = planning["native"]["model"]
        gap = _native_effort_gap(evidence, role.effort, planned)
        model = evidence["native"]["model"]
    if gap:
        if launch is not None:
            gap = f"selection evidence changed since discovery: {gap}"
        return _decision(role, mode, "fallback", note=gap)
    return _decision(role, mode, mode, model, role.effort,
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
        return _native_effort_gap(planning, role.effort)
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
    _resolve_run_selections). Explicit pins are never checked against
    the catalog, and with routing off automatic roles are not errors: they
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


def _resolve_run_selections(roles, run_dir, routing_mode, at_launch):
    """Validate authoring and attach every role's decision: the one
    orchestration behind the preflight plan and the launch.

    Reads this run's planning snapshot from `run_dir`; an authoring defect
    exits 2 before any discovery. Returns (roles with `decision` set, the
    launch snapshot or None). Only the launch (`at_launch`) discovers, and
    only when a role carries an automatic selection and routing is on —
    councils of inherited and explicit roles pay no discovery latency. The
    launch snapshot is frozen for the whole council and never written over
    the planning snapshot. The preflight passes the resolver no launch
    snapshot, so its decisions are the plan.

    The preflight judges authoring, and resolves, at the current time. The
    launch judges authoring as of the planning snapshot's creation, so a
    choice already retired at discovery stays an exit-2 defect, while a
    retirement that passes after discovery is changed evidence: the
    resolver falls back to inheritance. Its clock is read only after launch
    discovery has finished, so a retirement that passes while discovery
    runs is already in effect when the evidence is judged.
    """
    planning, planning_problem = _read_snapshot(run_dir)
    if at_launch:
        authoring_now = planning["created_at"] if planning is not None else None
    else:
        authoring_now = _utc_iso(time.time())
    _validate_selection_authoring(
        roles, planning, planning_problem, routing_mode, authoring_now
    )
    launch = None
    if (at_launch and routing_mode == "auto"
            and any(_is_automatic(r) for r in roles)):
        launch = _discover(routing_mode)
    now = _utc_iso(time.time()) if at_launch else authoring_now
    resolved = [
        replace(role, decision=_resolve_selection(
            role, planning, launch, routing_mode, now
        ))
        for role in roles
    ]
    return resolved, launch


# ---------- selection text for reports and the preflight plan ----------

def _sent_values(model, effort):
    """"model X, effort Y" for whichever of the two is present."""
    parts = []
    if model:
        parts.append(f"model {model}")
    if effort:
        parts.append(f"effort {effort}")
    return ", ".join(parts)


def _selection_summary_note(decision):
    """The Summary-line note for what one role sent ("" = inherited)."""
    sent = _sent_values(decision.dispatch_model, decision.dispatch_effort)
    if decision.provenance == "user":
        return f" (explicit: {sent})"
    if decision.provenance == "routed":
        return f" (routed: {sent})"
    if decision.provenance == "native_effort":
        return (f" (routed effort: {decision.dispatch_effort} on native "
                f"model {decision.dispatch_model})")
    if decision.provenance == "fallback":
        return " (native inheritance; routing fell back)"
    return ""


def _selection_section_text(decision):
    """The `_Model selection: ..._` text every role section carries."""
    sent = _sent_values(decision.dispatch_model, decision.dispatch_effort)
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
    """One role's preflight plan; automatic choices are revalidated."""
    sent = _sent_values(decision.dispatch_model, decision.dispatch_effort)
    if decision.provenance == "user":
        text = f"explicit override ({sent})"
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
