"""Model selection: the roles.json contract, the resolver, launch
revalidation, failure classification, and reporting.

Unit tests drive codex_council's parser, the pure resolver
(_resolve_selection), authoring validation, the failure classifier, and the
report renderers directly with synthetic snapshots. End-to-end tests run the
REAL script (--discover, --check-staging-dir, launch, --follow) as a
subprocess with the fake `codex` from tests/fake_codex.py on PATH (no
network, no real Codex). Model ids and efforts are synthetic only.

Lives outside the plugin subtree so end-user installs don't bundle it.
Run from repo root:
    python3 -m unittest discover -s tests -p 'test_*.py'
"""

import calendar
import contextlib
import copy
import dataclasses
import glob
import io
import json
import os
import random
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import tomllib
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# council_testlib puts the runner's scripts directory on sys.path, so it is
# imported before the runner modules.
import council_testlib  # noqa: E402
import codex_council  # noqa: E402
import council_common  # noqa: E402
import council_discovery  # noqa: E402
import council_failures  # noqa: E402
import council_liveness  # noqa: E402
import council_selection  # noqa: E402
import fake_codex  # noqa: E402
from council_testlib import (  # noqa: E402
    EPOCH,
    SCRIPT,
    SNAPSHOT_ID,
    assert_usage_exit as _assert_usage_exit,
    catalog as _catalog,
    clean_env as _clean_env,
)

ROUTING_ENV = council_discovery.MODEL_ROUTING_ENV
NATIVE = fake_codex.NATIVE_MODEL          # future-orion-2032, the native model
VEGA = "future-vega-2033"                 # visible, recommended
LYRA = "future-lyra-2030"                 # visible, retires 2031-01-01
HIDDEN = "future-hidden-2031"             # hidden
CUSTOM = "acme/future-review-2034:rev2"   # a custom-provider id, never listed
NOW = "2026-09-27T12:00:00Z"
LYRA_RETIRES = "2031-01-01T00:00:00Z"      # fake_codex.RETIREMENT_AT
BEFORE_LYRA_RETIRES = "2030-12-31T23:59:59Z"
AFTER_LYRA_RETIRES = "2031-06-01T00:00:00Z"
PAST_RETIREMENT_EPOCH = 1600000000          # an advertised retirement ...
PAST_RETIREMENT = "2020-09-13T12:26:40Z"    # ... that has already passed
SENTINELS = fake_codex.EXEC_SENTINELS
REWRITE = "rewrite the entire file passed to --roles-file"
INHERIT_HINT = ("omit model, effort, and selection to inherit native "
                "configuration")


def _instruction(text="Review"):
    return [f"{text}; if nothing material, say so clearly.",
            "Thoroughness beats speed."]


def _entry(rid="architect", label="Architect", text="Review", **extra):
    """One roles.json role object (JSON-shaped)."""
    entry = {"id": rid, "label": label, "instruction": _instruction(text)}
    entry.update(extra)
    return entry


def _present(mapping):
    """mapping without its None values (None here means "omit the key")."""
    return {key: value for key, value in mapping.items() if value is not None}


def _routed(rid="architect", model=VEGA, effort="brisk",
            snapshot_id=SNAPSHOT_ID, **extra):
    selection = _present({"mode": "routed", "snapshot_id": snapshot_id,
                          "reason": "narrow checks fit the fast model"})
    return _entry(rid, selection=selection,
                  **_present({"model": model, "effort": effort}), **extra)


def _native_effort(rid="architect", effort="adaptive-v2",
                   snapshot_id=SNAPSHOT_ID, **extra):
    selection = _present({"mode": "native_effort", "snapshot_id": snapshot_id,
                          "reason": "the hardest judgment in the panel"})
    return _entry(rid, selection=selection, **_present({"effort": effort}),
                  **extra)


def _parse(entries):
    raw = entries if isinstance(entries, str) else json.dumps(entries)
    return codex_council._parse_roles_json(raw)


def _tag(text, rc, phase, records=(),
         decision=council_selection._INHERIT_DECISION):
    """The runner's tagged failure text: one verdict for the attempt, then
    the formatter, as _run_role_invocation does."""
    verdict = council_failures._failure_verdict(
        text, records, decision.dispatch_model, resume=phase == "resume")
    return council_failures._classify_failure(text, rc, phase, decision,
                                              verdict)


def _role(rid="architect", model=None, effort=None, mode=None,
          snapshot_id=SNAPSHOT_ID, reason="grounded in the snapshot"):
    """A parsed Role (selection attached) without going through JSON; a
    model or effort without a mode is an explicit user pin."""
    if mode is None and (model is not None or effort is not None):
        mode = "user"
    selection = None
    if mode == "user":
        selection = council_selection.Selection("user")
    elif mode is not None:
        selection = council_selection.Selection(mode, snapshot_id, reason)
    return council_selection.Role(
        rid, rid.title(), " ".join(_instruction()), model, effort, selection)


# ---------- synthetic snapshots (built by the real snapshot builder) ----------

def _snapshot(snapshot_id=SNAPSHOT_ID, routing_mode="auto", entries=None,
              **observed):
    """A discovery snapshot; `entries` is the catalog's wire entries, and
    keyword args override the observations."""
    if entries is not None:
        observed.setdefault("catalog", _catalog(entries))
    return council_testlib.snapshot(snapshot_id, routing_mode, **observed)


def _unavailable(problem="rpc_error:model/list:-32601"):
    return _snapshot(conclusive=False, problems=[problem], account=None,
                     configured=None, managed=None, catalog=None)


def _managed_present():
    return {"status": "present", "model": "future-managed-2035",
            "effort": "deliberate", "provider_keys": []}


def _resolve(role, planning=None, launch=None, routing_mode="auto", now=NOW):
    return council_selection._resolve_selection(
        role, planning, launch, routing_mode, now)


# ---------- the value grammar (model and effort) ----------

class SelectionValueGrammarTests(unittest.TestCase):
    def test_future_values_and_case_are_preserved(self):
        user = {"mode": "user"}
        for model in (NATIVE, CUSTOM, "Future.Model@2+exp", "x"):
            with self.subTest(model=model):
                self.assertEqual(
                    _parse([_entry(model=model, selection=user)])[0].model,
                    model)
        for effort in ("adaptive-v2", "deliberate", "High", "x-high",
                       "X.High:2", "ultra"):
            with self.subTest(effort=effort):
                self.assertEqual(
                    _parse([_entry(effort=effort, selection=user)])[0].effort,
                    effort)

    def test_unsafe_values_are_rejected_for_both_fields(self):
        bad_values = (
            "", "-x", ".x", "_x", "@x", "a b", "a\tb", "a\nb", "a b",
            'a"b', "a'b", "a\\b", "a<b", "a>b", "a=b", "a;b", "a$b", "a`b",
            "café", "a\x00b", None, 3, True, ["x"], {"x": 1},
        )
        for field in ("model", "effort"):
            for value in bad_values:
                with self.subTest(field=field, value=value):
                    err = _assert_usage_exit(
                        self, lambda f=field, v=value: _parse([_entry(
                            **{f: v, "selection": {"mode": "user"}})]),
                        expect_in_stderr=f"optional field '{field}'",
                    )
                    self.assertIn(REWRITE, err)
                    self.assertIn(council_selection.USER_PIN_VALUE_HINT, err)

    def test_reserved_inheritance_words_are_not_model_ids(self):
        for value in ("inherit", "default", "INHERIT", "Default", "InHeRiT"):
            with self.subTest(value=value):
                extra = {"model": value, "selection": {"mode": "user"}}
                err = _assert_usage_exit(
                    self, lambda extra=extra: _parse([_entry(**extra)]),
                    expect_in_stderr=(
                        f"model '{value}' is not an inheritance value; "
                        f"{INHERIT_HINT}"),
                )
                self.assertIn(REWRITE, err)

    def test_a_malformed_user_pin_is_repaired_never_dropped(self):
        """A display name with a space is not an execution id; the error
        for an explicit pin says to map or ask, not to inherit."""
        for field, value in (("model", "Future Orion"),
                             ("effort", "Extra High")):
            with self.subTest(field=field):
                extra = {field: value, "selection": {"mode": "user"}}
                err = _assert_usage_exit(
                    self, lambda e=extra: _parse([_entry(**e)]),
                    expect_in_stderr=council_selection.USER_PIN_VALUE_HINT)
                self.assertNotIn(INHERIT_HINT, err)
                self.assertIn(REWRITE, err)
        # An automatic value came from the summary: inheriting is fine.
        err = _assert_usage_exit(
            self, lambda: _parse([_routed(model="Future Orion")]),
            expect_in_stderr=INHERIT_HINT)
        self.assertNotIn(council_selection.USER_PIN_VALUE_HINT, err)

    def test_reserved_words_are_ordinary_effort_values(self):
        """Only a MODEL of inherit/default is refused. Efforts are opaque
        per-model values: a pinned one is forwarded as written, and an
        automatic one must be advertised like any other."""
        for effort in ("inherit", "default", "Default", "INHERIT"):
            with self.subTest(effort=effort):
                role = _parse([_entry(effort=effort,
                                      selection={"mode": "user"})])[0]
                decision = _resolve(role, _snapshot())
                self.assertEqual(decision.dispatch_effort, effort)
                cmd = codex_council._fresh_cmd("/r", None, effort)
                self.assertEqual(cmd[cmd.index("-c") + 1],
                                 f'model_reasoning_effort="{effort}"')
                self.assertEqual(
                    council_selection._routed_evidence_gap(
                        _snapshot(), VEGA, effort, NOW),
                    f"effort '{effort}' is not advertised for model '{VEGA}'")

    def test_accepted_values_stay_single_argv_items_and_toml_strings(self):
        for effort in ("adaptive-v2", "X.High:2", "a/b@c+d"):
            with self.subTest(effort=effort):
                cmd = codex_council._fresh_cmd("/r", CUSTOM, effort)
                self.assertEqual(cmd[cmd.index("-m") + 1], CUSTOM)
                setting = cmd[cmd.index("-c") + 1]
                self.assertEqual(setting, f'model_reasoning_effort="{effort}"')
                self.assertEqual(
                    tomllib.loads(setting)["model_reasoning_effort"], effort)


# ---------- the selection object ----------

class SelectionObjectParsingTests(unittest.TestCase):
    def test_inheritance_is_omission(self):
        role = _parse([_entry()])[0]
        self.assertIsNone(role.model)
        self.assertIsNone(role.effort)
        self.assertIsNone(role.selection)
        decision = council_selection._role_decision(role)
        self.assertEqual(decision.provenance, "native")
        self.assertIsNone(decision.dispatch_model)
        self.assertIsNone(decision.dispatch_effort)

    def test_user_mode_takes_either_or_both_values_and_an_optional_reason(self):
        for extra in ({"model": CUSTOM}, {"effort": "brisk"},
                      {"model": CUSTOM, "effort": "brisk"}):
            with self.subTest(extra=extra):
                role = _parse([_entry(selection={"mode": "user"}, **extra)])[0]
                self.assertEqual(role.selection,
                                 council_selection.Selection("user"))
        role = _parse([_entry(model=CUSTOM, selection={
            "mode": "user", "reason": "the user asked for this model"})])[0]
        self.assertEqual(role.selection.reason, "the user asked for this model")
        self.assertIsNone(role.selection.snapshot_id)

    def test_user_mode_needs_a_value_and_no_snapshot_id(self):
        _assert_usage_exit(
            self, lambda: _parse([_entry(selection={"mode": "user"})]),
            expect_in_stderr="selection.mode 'user' needs 'model' and/or "
                             "'effort'")
        _assert_usage_exit(
            self, lambda: _parse([_entry(model=CUSTOM, selection={
                "mode": "user", "snapshot_id": SNAPSHOT_ID})]),
            expect_in_stderr="selection.snapshot_id is only for 'routed' and "
                             "'native_effort'")

    def test_routed_parses_its_snapshot_binding(self):
        role = _parse([_routed()])[0]
        self.assertEqual(role.model, VEGA)
        self.assertEqual(role.effort, "brisk")
        self.assertEqual(role.selection, council_selection.Selection(
            "routed", SNAPSHOT_ID, "narrow checks fit the fast model"))

    def test_routed_needs_both_values_a_snapshot_id_and_a_reason(self):
        cases = (
            (_routed(model=None), "needs both 'model' and 'effort'"),
            (_routed(effort=None), "needs both 'model' and 'effort'"),
            (_routed(snapshot_id=None), "needs 'snapshot_id'"),
            (_routed(snapshot_id="0123456789ABCDEF"), "needs 'snapshot_id'"),
            # fullmatch: a valid id with anything after it is not an id.
            (_routed(snapshot_id=SNAPSHOT_ID + "XYZ"), "needs 'snapshot_id'"),
            (_routed(snapshot_id=SNAPSHOT_ID + "\n"), "needs 'snapshot_id'"),
            (_routed(snapshot_id="0123"), "needs 'snapshot_id'"),
            (_routed(snapshot_id=7), "needs 'snapshot_id'"),
        )
        for entry, expected in cases:
            with self.subTest(entry=entry):
                _assert_usage_exit(self, lambda e=entry: _parse([e]),
                                   expect_in_stderr=expected)
        for reason in (None, "", "   ", 5):
            entry = _routed()
            if reason is None:
                del entry["selection"]["reason"]
            else:
                entry["selection"]["reason"] = reason
            with self.subTest(reason=reason):
                _assert_usage_exit(
                    self, lambda e=entry: _parse([e]),
                    expect_in_stderr="selection.reason must be a non-empty "
                                     "single-line string")

    def test_native_effort_needs_effort_and_forbids_model(self):
        role = _parse([_native_effort()])[0]
        self.assertIsNone(role.model)
        self.assertEqual(role.selection.mode, "native_effort")
        for entry in (_native_effort(model=NATIVE), _native_effort(effort=None)):
            with self.subTest(entry=entry):
                _assert_usage_exit(
                    self, lambda e=entry: _parse([e]),
                    expect_in_stderr="selection.mode 'native_effort' takes "
                                     "'effort' and no 'model'")

    def test_unknown_selection_keys_are_rejected(self):
        entry = _routed()
        entry["selection"]["confidence"] = "high"
        _assert_usage_exit(
            self, lambda: _parse([entry]),
            expect_in_stderr="selection has unknown field(s) 'confidence'")
        _assert_usage_exit(
            self, lambda: _parse([_entry(model=CUSTOM, selection={
                "mode": "user", "why": "x"})]),
            expect_in_stderr="selection has unknown field(s) 'why'")

    def test_invalid_modes_are_rejected(self):
        for mode in ("inherit", "auto", "Routed", "native-effort", "", None,
                     3, ["user"]):
            with self.subTest(mode=mode):
                _assert_usage_exit(
                    self, lambda m=mode: _parse([_entry(
                        model=CUSTOM, selection={"mode": m})]),
                    expect_in_stderr="selection.mode must be 'user', "
                                     "'routed', or 'native_effort'")
        _assert_usage_exit(
            self, lambda: _parse([_entry(model=CUSTOM, selection={})]),
            expect_in_stderr="selection.mode must be")

    def test_selection_must_be_an_object(self):
        for value in ("user", ["user"], None, 1):
            with self.subTest(value=value):
                _assert_usage_exit(
                    self, lambda v=value: _parse([_entry(
                        model=CUSTOM, selection=v)]),
                    expect_in_stderr="'selection' must be an object")

    def test_reason_is_single_line_with_no_length_cap(self):
        for ch in council_common.LINEBREAK_CHARS:
            entry = _routed()
            entry["selection"]["reason"] = f"first{ch}second"
            with self.subTest(char=hex(ord(ch))):
                _assert_usage_exit(
                    self, lambda e=entry: _parse([e]),
                    expect_in_stderr="selection.reason must not contain "
                                     "newlines")
        entry = _routed()
        entry["selection"]["reason"] = "r" * 100_000
        self.assertEqual(len(_parse([entry])[0].selection.reason), 100_000)

    def test_untagged_pins_are_refused(self):
        # The missing selection is reported before the value grammar, so a
        # malformed untagged pin is first asked for its provenance rather
        # than told how to inherit.
        for extra in ({"model": CUSTOM}, {"effort": "brisk"},
                      {"model": CUSTOM, "effort": "brisk"},
                      {"model": "Future Orion"}, {"effort": "Extra High"},
                      {"model": "inherit"}):
            with self.subTest(extra=extra):
                err = _assert_usage_exit(
                    self, lambda e=extra: _parse([_entry(**e)]),
                    expect_in_stderr="declare selection.mode: 'user' for an "
                                     "explicit user request, 'routed' or "
                                     "'native_effort' for a runtime-grounded "
                                     "choice")
                self.assertIn(REWRITE, err)
                self.assertNotIn("optional field", err)
                self.assertNotIn("not an inheritance value", err)
        # Inheritance and tagged selections are fine.
        roles = _parse([_entry("a"), _routed("b"),
                        _entry("c", model=CUSTOM, selection={"mode": "user"})])
        self.assertEqual([r.id for r in roles], ["a", "b", "c"])

    def test_duplicate_keys_are_rejected_at_every_level(self):
        instruction = json.dumps(_instruction())
        cases = {
            "role": ('[{"id": "a", "label": "A", "instruction": %s, '
                     '"model": "future-vega-2033", "model": "x"}]'
                     % instruction),
            "selection": ('[{"id": "a", "label": "A", "instruction": %s, '
                          '"model": "x", "selection": {"mode": "user", '
                          '"mode": "routed"}}]' % instruction),
            "id": ('[{"id": "a", "id": "b", "label": "A", '
                   '"instruction": %s}]' % instruction),
        }
        for level, raw in cases.items():
            with self.subTest(level=level):
                err = _assert_usage_exit(
                    self, lambda raw=raw: _parse(raw),
                    expect_in_stderr="invalid JSON (duplicate JSON key")
                self.assertIn(REWRITE, err)

    def test_non_finite_numbers_are_rejected(self):
        for constant in ("NaN", "Infinity", "-Infinity"):
            raw = ('[{"id": "a", "label": "A", "instruction": ["x"], '
                   '"selection": {"mode": %s}}]' % constant)
            with self.subTest(constant=constant):
                _assert_usage_exit(self, lambda raw=raw: _parse(raw),
                                   expect_in_stderr="non-finite number")

    def test_every_selection_defect_names_the_shared_recovery_once(self):
        defects = {
            "bad-model": [_entry(model="a b", selection={"mode": "user"})],
            "reserved-model": [_entry(model="inherit",
                                      selection={"mode": "user"})],
            "untagged-bad-model": [_entry(model="a b")],
            "untagged": [_entry(model=CUSTOM)],
            "selection-not-object": [_entry(model=CUSTOM, selection="user")],
            "bad-mode": [_entry(model=CUSTOM, selection={"mode": "x"})],
            "unknown-selection-key": [_entry(model=CUSTOM, selection={
                "mode": "user", "x": 1})],
            "routed-half-pair": [_routed(effort=None)],
            "native-effort-with-model": [_native_effort(model=NATIVE)],
        }
        for name, entries in defects.items():
            with self.subTest(defect=name):
                buf = io.StringIO()
                with contextlib.redirect_stderr(buf):
                    with self.assertRaises(SystemExit) as ctx:
                        _parse(entries)
                self.assertEqual(ctx.exception.code, 2)
                self.assertEqual(buf.getvalue().count(REWRITE), 1)


# ---------- the pure resolver ----------

class ResolverTests(unittest.TestCase):
    def test_inheritance_sends_nothing_whatever_the_evidence(self):
        for planning, launch, mode in (
            (None, None, "auto"), (_snapshot(), _snapshot(), "auto"),
            (_snapshot(), None, "off"), (_unavailable(), None, "auto"),
        ):
            with self.subTest(mode=mode):
                self.assertEqual(
                    _resolve(_role(), planning, launch, mode),
                    council_selection.SelectionDecision("native"))

    def test_explicit_pin_is_forwarded_unchanged_even_off_catalog(self):
        """Catalog absence never rejects or replaces an explicit pin."""
        for planning in (None, _snapshot(), _unavailable()):
            decision = _resolve(_role(model=CUSTOM, effort="brisk",
                                      mode="user"), planning,
                                routing_mode="off")
            with self.subTest(planning=planning and planning["status"]):
                self.assertEqual(decision.provenance, "user")
                self.assertEqual(
                    (decision.dispatch_model, decision.dispatch_effort),
                    (CUSTOM, "brisk"))
        decision = _resolve(_role(model=CUSTOM, mode="user"), _snapshot())
        self.assertEqual(decision.note, council_selection.UNVERIFIED_MODEL_ADVISORY)

    def test_a_pin_naming_a_catalog_alias_points_at_the_execution_id(self):
        """A user who names a model as the picker shows it gets the id -m
        takes; the pin itself is still forwarded unchanged."""
        for name, kind in (("Orion", "display name"),
                           ("picker-orion", "picker id")):
            decision = _resolve(_role(model=name, mode="user"), _snapshot())
            with self.subTest(name=name):
                self.assertEqual(decision.provenance, "user")
                self.assertEqual(decision.dispatch_model, name)
                self.assertEqual(
                    decision.note,
                    f"{council_selection.UNVERIFIED_MODEL_ADVISORY}; "
                    f"'{name}' is the catalog {kind} of execution id "
                    f"'{NATIVE}'")

    def test_only_a_dispatchable_execution_id_is_ever_suggested(self):
        """Catalog text cannot reach a note through the alias hint: an
        execution id outside the value grammar (spaces, controls, a
        ' reply=' marker) is never offered."""
        hostile = "x\x1b]0;owned\x07 reply=/tmp/x"
        snapshot = _snapshot(entries=fake_codex.default_catalog() + [
            fake_codex.model_entry(hostile, id="picker-nova",
                                   displayName="Nova")])
        pin = _resolve(_role(model="Nova", mode="user"), snapshot)
        self.assertEqual(pin.note, council_selection.UNVERIFIED_MODEL_ADVISORY)
        routed = _resolve(_role(model="picker-nova", effort="brisk",
                                mode="routed"), _snapshot(), snapshot)
        self.assertEqual(
            routed.note, "selection evidence changed since discovery: model "
                         "'picker-nova' is not an advertised execution id in "
                         "launch discovery")

    def test_explicit_effort_advisories_name_the_model_they_checked(self):
        pinned = _resolve(_role(model=VEGA, effort="adaptive-v2", mode="user"),
                          _snapshot())
        self.assertEqual(
            pinned.note,
            "unverified effort: 'adaptive-v2' is not advertised for model "
            f"'{VEGA}'; forwarded unchanged")
        effort_only = _resolve(_role(effort="Brisk", mode="user"), _snapshot())
        self.assertEqual(
            effort_only.note,
            "unverified effort: 'Brisk' is not advertised for model "
            f"'{NATIVE}'; forwarded unchanged")
        self.assertIsNone(_resolve(_role(effort="brisk", mode="user"),
                                   _snapshot()).note)

    def test_a_model_only_pin_notes_an_unadvertised_inherited_effort(self):
        """A model-only pin keeps the configured native effort, which Codex
        does not validate on the client, so it gets the note the same pair
        written out would get, worded as inherited rather than sent."""
        def configured(effort):
            return dict(council_testlib.observed()["configured"],
                        effort=effort)

        snapshot = _snapshot(configured=configured("adaptive-v2"))
        expected = ("unverified effort: inherited native effort "
                    f"'adaptive-v2' is not advertised for model '{VEGA}'; "
                    "no effort override sent")
        for mode in ("user", None):  # None: untagged, direct CLI use
            with self.subTest(mode=mode):
                decision = _resolve(_role(model=VEGA, mode=mode), snapshot)
                self.assertEqual(
                    (decision.dispatch_model, decision.dispatch_effort),
                    (VEGA, None))
                self.assertEqual(decision.note, expected)
        # Advertised for the pinned model, or no configured effort (Codex
        # then uses the model's own default): nothing to note.
        self.assertIsNone(_resolve(_role(model=NATIVE, mode="user"),
                                   snapshot).note)
        self.assertIsNone(_resolve(_role(model=VEGA, mode="user"),
                                   _snapshot(configured=configured(None))).note)
        # Managed defaults present or unknown: the kept effort is unknown,
        # and the partial-pin note already covers it.
        for managed in (_managed_present(), None):
            with self.subTest(managed=managed):
                note = _resolve(_role(model=VEGA, mode="user"), _snapshot(
                    configured=configured("adaptive-v2"),
                    managed=managed)).note
                self.assertEqual(note, council_selection.PARTIAL_PIN_ADVISORY)
        # A model outside the catalog has no efforts to compare with, and
        # configuration text outside the value grammar never reaches a note.
        self.assertEqual(
            _resolve(_role(model=CUSTOM, mode="user"), snapshot).note,
            council_selection.UNVERIFIED_MODEL_ADVISORY)
        self.assertIsNone(_resolve(_role(model=VEGA, mode="user"), _snapshot(
            configured=configured("x\x1b]0;owned\x07 reply=/tmp/x"))).note)

    def test_a_pin_on_a_retired_model_is_noted_and_forwarded(self):
        """A pin is never refused, but a model whose advertised retirement
        has passed (retirement_at <= now) is flagged, as routing would be."""
        expected = (f"model '{LYRA}' advertised retirement passed "
                    f"({LYRA_RETIRES}); forwarded unchanged")
        for now, note in ((AFTER_LYRA_RETIRES, expected),
                          (LYRA_RETIRES, expected),
                          (BEFORE_LYRA_RETIRES, None), (None, None)):
            decision = _resolve(_role(model=LYRA, effort="brisk",
                                      mode="user"), _snapshot(), now=now)
            with self.subTest(now=now):
                self.assertEqual(decision.provenance, "user")
                self.assertEqual(
                    (decision.dispatch_model, decision.dispatch_effort),
                    (LYRA, "brisk"))
                self.assertEqual(decision.note, note)
        both = _resolve(_role(model=LYRA, effort="adaptive-v2", mode="user"),
                        _snapshot(), now=AFTER_LYRA_RETIRES)
        self.assertEqual(
            both.note, f"{expected}; unverified effort: 'adaptive-v2' is not "
                       f"advertised for model '{LYRA}'; forwarded unchanged")

    def test_partial_pin_advisory_follows_managed_defaults(self):
        """A partial pin while managed defaults exist is flagged."""
        partial = _role(effort="brisk", mode="user")
        for managed, flagged in (
            (_managed_present(), True),
            ({"status": "unknown", "model": None, "effort": None,
              "provider_keys": []}, True),
            (None, True),
            ({"status": "absent", "model": None, "effort": None,
              "provider_keys": []}, False),
        ):
            snapshot = _snapshot(managed=managed)
            with self.subTest(status=snapshot["managed_defaults"]["status"]):
                note = _resolve(partial, snapshot).note or ""
                self.assertEqual(
                    council_selection.PARTIAL_PIN_ADVISORY in note, flagged)
        both = _role(model=VEGA, effort="brisk", mode="user")
        self.assertIsNone(_resolve(both, _snapshot(
            managed=_managed_present())).note)
        self.assertIsNone(_resolve(partial, None).note)

    def test_routed_pair_is_dispatched_exactly_as_authored(self):
        role = _role(model=VEGA, effort="deliberate", mode="routed")
        decision = _resolve(role, _snapshot())
        self.assertEqual(decision, council_selection.SelectionDecision(
            "routed", VEGA, "deliberate", VEGA, "deliberate",
            "grounded in the snapshot", None, NATIVE))

    def test_a_sent_model_carries_the_native_model_its_evidence_proves(self):
        """A decision that sends a model records the native model the
        resolving evidence proves (the launch evidence when present), so a
        refusal can tell whether the sent model was the native one; it is
        never sent, and it is None without proof."""
        unproven = _snapshot(configured={
            "model": None, "effort": None, "provider": None,
            "model_origin": None, "effort_origin": None,
            "endpoint_overrides": [], "catalog_override": False})
        self.assertNotEqual(unproven["native"]["resolution"], "proven")
        routed = _role(model=NATIVE, effort="brisk", mode="routed")
        pinned = _role(model=NATIVE, mode="user")
        for role, planning, launch, native in (
            (routed, _snapshot(), None, NATIVE),
            (routed, _snapshot(), _snapshot("fedcba9876543210"), NATIVE),
            (pinned, _snapshot(), None, NATIVE),
            (pinned, None, None, None),
            (pinned, unproven, None, None),
        ):
            with self.subTest(mode=role.selection.mode,
                              planning=planning is not None,
                              launch=launch is not None):
                decision = _resolve(role, planning, launch)
                self.assertEqual(decision.provenance, role.selection.mode)
                self.assertEqual(decision.dispatch_model, NATIVE)
                self.assertEqual(decision.native_model, native)

    def test_native_effort_pins_the_native_model_the_evidence_proves(self):
        role = _role(effort="brisk", mode="native_effort")
        planned = _resolve(role, _snapshot())
        self.assertEqual((planned.provenance, planned.dispatch_model,
                          planned.dispatch_effort),
                         ("native_effort", NATIVE, "brisk"))
        self.assertIsNone(planned.requested_model)
        launched = _resolve(role, _snapshot(), _snapshot("fedcba9876543210"))
        self.assertEqual((launched.provenance, launched.dispatch_model,
                          launched.dispatch_effort),
                         ("native_effort", NATIVE, "brisk"))

    def test_native_effort_never_moves_its_effort_to_another_model(self):
        """The effort was chosen from the PLANNING native model's
        descriptions; a different launch native model falls back even when
        it advertises an effort with the same spelling."""
        role = _role(effort="brisk", mode="native_effort")
        launch = _snapshot(configured={
            "model": VEGA, "effort": None, "provider": None,
            "model_origin": "project", "effort_origin": None,
            "endpoint_overrides": [], "catalog_override": False})
        self.assertEqual(launch["native"],
                         {"resolution": "proven", "model": VEGA,
                          "reason": None})
        launched = _resolve(role, _snapshot(), launch)
        self.assertEqual(launched.provenance, "fallback")
        self.assertIsNone(launched.dispatch_model)
        self.assertIsNone(launched.dispatch_effort)
        self.assertEqual(
            launched.note, "selection evidence changed since discovery: "
                           f"native model changed from '{NATIVE}' to "
                           f"'{VEGA}'")

    def test_routing_off_resolves_every_automatic_role_to_inheritance(self):
        for role in (_role(model=VEGA, effort="brisk", mode="routed"),
                     _role(effort="brisk", mode="native_effort")):
            decision = _resolve(role, _snapshot(), _snapshot(),
                                routing_mode="off")
            with self.subTest(mode=role.selection.mode):
                self.assertEqual(decision.provenance, "fallback")
                self.assertEqual(decision.note, "CODEX_COUNCIL_MODEL_ROUTING=off")
                self.assertIsNone(decision.dispatch_model)
                self.assertIsNone(decision.dispatch_effort)
                self.assertEqual(decision.requested_effort, "brisk")

    def test_unavailable_launch_discovery_falls_back_to_inheritance(self):
        """A failed launch discovery never blocks; it inherits."""
        for role in (_role(model=VEGA, effort="brisk", mode="routed"),
                     _role(effort="brisk", mode="native_effort")):
            decision = _resolve(role, _snapshot(),
                                _unavailable("timeout:model/list"))
            with self.subTest(mode=role.selection.mode):
                self.assertEqual(decision.provenance, "fallback")
                self.assertEqual(decision.note, "launch discovery unavailable: "
                                                "timeout:model/list")
        no_evidence = _resolve(_role(model=VEGA, effort="brisk",
                                     mode="routed"))
        self.assertEqual(no_evidence.note, "no discovery snapshot for this run")

    def test_ineligible_launch_routing_blocks_routed_but_not_native_effort(self):
        """An incomplete catalog blocks routing but not an effort on a
        native model whose own entry is usable."""
        incomplete = _catalog(fake_codex.default_catalog())
        incomplete["gaps"].append("stopped at the 10-page bound")
        launch = _snapshot(catalog=incomplete)
        routed = _resolve(_role(model=VEGA, effort="brisk", mode="routed"),
                          _snapshot(), launch)
        self.assertEqual(
            routed.note, "launch discovery reports routing unavailable: "
                         "catalog incomplete: stopped at the 10-page bound")
        native = _resolve(_role(effort="brisk", mode="native_effort"),
                          _snapshot(), launch)
        self.assertEqual(native.provenance, "native_effort")

    def test_signed_out_launch_discovery_blocks_both_automatic_modes(self):
        """A signed-out catalog proves no native model either: neither a
        routed pair nor a native-model effort is sent."""
        signed_out = _snapshot(account={"type": None,
                                        "requires_openai_auth": True})
        routed = _resolve(_role(model=VEGA, effort="brisk", mode="routed"),
                          _snapshot(), signed_out)
        self.assertEqual(
            routed.note, "launch discovery reports routing unavailable: not "
                         "signed in: catalog is not account-grounded")
        native = _resolve(_role(effort="brisk", mode="native_effort"),
                          _snapshot(), signed_out)
        self.assertEqual(native.provenance, "fallback")
        self.assertIsNone(native.dispatch_model)
        self.assertIsNone(native.dispatch_effort)
        self.assertEqual(
            native.note, "selection evidence changed since discovery: cannot "
                         "adjust effort on the native model: not signed in: "
                         "catalog is not account-grounded")

    def test_evidence_change_since_planning_falls_back_with_the_detail(self):
        """Launch evidence that no longer supports the pair inherits."""
        prefix = "selection evidence changed since discovery: "
        without_vega = [e for e in fake_codex.default_catalog()
                        if e["model"] != VEGA]
        hidden_vega = [fake_codex.model_entry(VEGA, hidden=True)] + without_vega
        narrowed_vega = [fake_codex.model_entry(VEGA, supportedReasoningEfforts=[
            {"reasoningEffort": "deliberate", "description": "d"}],
            defaultReasoningEffort="deliberate")] + without_vega
        cases = (
            # The launch snapshot is never written anywhere, so the note
            # names the stage, not an id no reader could look up.
            (without_vega, VEGA, NOW,
             f"model '{VEGA}' is not an advertised execution id in launch "
             "discovery"),
            (hidden_vega, VEGA, NOW,
             f"cannot route to hidden model '{VEGA}' (hidden models are for "
             "explicit user pins)"),
            (narrowed_vega, VEGA, NOW,
             f"effort 'brisk' is not advertised for model '{VEGA}'"),
            (None, LYRA, AFTER_LYRA_RETIRES,
             f"model '{LYRA}' advertised retirement passed "
             "(2031-01-01T00:00:00Z)"),
            # Retired when retirement_at <= now: the boundary is retired.
            (None, LYRA, LYRA_RETIRES,
             f"model '{LYRA}' advertised retirement passed "
             "(2031-01-01T00:00:00Z)"),
        )
        for entries, model, now, detail in cases:
            launch = _snapshot(snapshot_id="fedcba9876543210", entries=entries)
            decision = _resolve(
                _role(model=model, effort="brisk", mode="routed"),
                _snapshot(), launch, now=now)
            with self.subTest(detail=detail, now=now):
                self.assertEqual(decision.provenance, "fallback")
                self.assertEqual(decision.note, prefix + detail)
                self.assertIsNone(decision.dispatch_model)
        # One second before the advertised retirement the pair still routes.
        before = _resolve(_role(model=LYRA, effort="brisk", mode="routed"),
                          _snapshot(), _snapshot(), now=BEFORE_LYRA_RETIRES)
        self.assertEqual(before.provenance, "routed")

    def test_native_effort_evidence_change_falls_back(self):
        prefix = "selection evidence changed since discovery: "
        role = _role(effort="adaptive-v2", mode="native_effort")
        managed = _resolve(role, _snapshot(),
                           _snapshot(managed=_managed_present()))
        self.assertEqual(
            managed.note, prefix + "cannot adjust effort on the native "
                                   "model: managed new-thread defaults present")
        moved = _resolve(role, _snapshot(), _snapshot(configured={
            "model": VEGA, "effort": None, "provider": None,
            "model_origin": "user", "effort_origin": None,
            "endpoint_overrides": [], "catalog_override": False}))
        self.assertEqual(
            moved.note,
            prefix + f"native model changed from '{NATIVE}' to '{VEGA}'")
        narrowed = [fake_codex.model_entry(NATIVE)] + [
            e for e in fake_codex.default_catalog() if e["model"] != NATIVE]
        dropped = _resolve(role, _snapshot(), _snapshot(entries=narrowed))
        self.assertEqual(
            dropped.note,
            prefix + f"effort 'adaptive-v2' is not advertised for model "
                     f"'{NATIVE}'")

    def test_a_native_model_id_outside_the_grammar_is_never_pinned(self):
        odd = "odd model id"
        snapshot = _snapshot(
            entries=[fake_codex.model_entry(odd)],
            configured={"model": odd, "effort": None, "provider": None,
                        "model_origin": "user", "effort_origin": None,
                        "endpoint_overrides": [], "catalog_override": False})
        self.assertEqual(snapshot["native"]["resolution"], "proven")
        decision = _resolve(_role(effort="brisk", mode="native_effort"),
                            None, snapshot)
        self.assertEqual(decision.provenance, "fallback")
        self.assertIn(f"native model id {odd!r} is not a dispatchable "
                      "selection value", decision.note)

    def test_catalog_order_and_recommendation_never_change_decisions(self):
        """No ranking by position, id spelling, or isDefault."""
        roles = [
            _role("a"),
            _role("b", model=VEGA, effort="brisk", mode="routed"),
            _role("c", model=LYRA, effort="deliberate", mode="routed"),
            _role("d", effort="adaptive-v2", mode="native_effort"),
            _role("e", model=CUSTOM, mode="user"),
            _role("f", model=HIDDEN, effort="brisk", mode="routed"),
        ]
        automatic = [r for r in roles if council_selection._is_automatic(r)]

        def verdicts(snapshot):
            return (
                [_resolve(r, snapshot) for r in roles],
                [council_selection._authoring_problem(r, snapshot, None, NOW)
                 for r in automatic],
            )

        baseline = verdicts(_snapshot())
        self.assertEqual(baseline[1][:3], [None, None, None])
        self.assertIn("cannot route to hidden model", baseline[1][3])
        rng = random.Random(20260927)
        for trial in range(12):
            entries = copy.deepcopy(fake_codex.default_catalog())
            rng.shuffle(entries)
            recommended = rng.randrange(len(entries))
            for index, entry in enumerate(entries):
                entry["isDefault"] = index == recommended
                rng.shuffle(entry["supportedReasoningEfforts"])
            with self.subTest(trial=trial):
                self.assertEqual(verdicts(_snapshot(entries=entries)),
                                 baseline)

    def test_configured_model_that_is_not_recommended_stays_inherited(self):
        """Native orion vs recommended vega; nothing auto-picks vega."""
        snapshot = _snapshot()
        recommended = [m["model"] for m in snapshot["catalog"]["models"]
                       if m["recommended"]]
        self.assertEqual(recommended, [VEGA])
        for planning, launch, mode in (
            (snapshot, snapshot, "auto"), (snapshot, None, "off"),
            (snapshot, _unavailable(), "auto"),
        ):
            with self.subTest(mode=mode, launch=bool(launch)):
                for role in (_role(), _role(effort="brisk", mode="routed",
                                            model=VEGA)):
                    decision = _resolve(role, planning, launch, mode)
                    if decision.provenance != "routed":
                        self.assertIsNone(decision.dispatch_model)

    def test_future_ids_and_new_effort_vocabulary_are_data(self):
        """An unseen model and effort route with no code change."""
        nova = fake_codex.model_entry(
            "future-nova-2040", id="picker-nova",
            defaultReasoningEffort="quantum-deliberation",
            supportedReasoningEfforts=[
                {"reasoningEffort": "quantum-deliberation",
                 "description": "A brand-new effort."}])
        snapshot = _snapshot(entries=fake_codex.default_catalog() + [nova])
        decision = _resolve(_role(model="future-nova-2040",
                                  effort="quantum-deliberation",
                                  mode="routed"), snapshot)
        self.assertEqual(decision.provenance, "routed")
        cmd = codex_council._fresh_cmd(
            "/r", decision.dispatch_model, decision.dispatch_effort)
        self.assertEqual(cmd[4:8], [
            "-m", "future-nova-2040",
            "-c", 'model_reasoning_effort="quantum-deliberation"'])

    def test_resolver_is_pure(self):
        planning, launch = _snapshot(), _snapshot(snapshot_id="fedcba9876543210")
        frozen = copy.deepcopy((planning, launch))
        role = _role(model=VEGA, effort="brisk", mode="routed")
        first = _resolve(role, planning, launch)
        self.assertEqual(first, _resolve(role, planning, launch))
        self.assertEqual((planning, launch), frozen)

    def test_decision_without_attachment_follows_the_request(self):
        self.assertEqual(
            council_selection._role_decision(_role(model=VEGA)).provenance,
            "user")
        routed = council_selection._role_decision(
            _role(model=VEGA, effort="brisk", mode="routed"))
        self.assertEqual(routed.provenance, "fallback")
        attached = council_selection.Role(
            "x", "X", "i", decision=council_selection.SelectionDecision(
                "routed", VEGA, "brisk", VEGA, "brisk", "r"))
        self.assertIs(council_selection._role_decision(attached),
                      attached.decision)


# ---------- authoring validation against the planning snapshot ----------

class AuthoringValidationTests(unittest.TestCase):
    def _validate(self, roles, planning=None, problem=None,
                  routing_mode="auto", now=NOW):
        council_selection._validate_selection_authoring(
            roles, planning, problem, routing_mode, now)

    def _rejects(self, roles, expected, **kwargs):
        err = _assert_usage_exit(
            self, lambda: self._validate(roles, **kwargs),
            expect_in_stderr=expected)
        self.assertEqual(err.count(REWRITE), 1)
        self.assertEqual(len(err.strip().splitlines()), 1)
        return err

    def test_automatic_modes_require_this_runs_snapshot(self):
        for role in (_role(model=VEGA, effort="brisk", mode="routed"),
                     _role(effort="brisk", mode="native_effort")):
            with self.subTest(mode=role.selection.mode):
                self._rejects(
                    [role],
                    f"--roles-file entry 0 (id 'architect'): selection.mode "
                    f"'{role.selection.mode}' requires this run's discovery "
                    "snapshot (model-snapshot.json does not exist); run "
                    "--discover on this run directory first, or omit model, "
                    "effort, and selection to inherit.",
                    problem="model-snapshot.json does not exist")

    def test_snapshot_file_problems_become_the_same_authoring_error(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        run_dir = tmp.name
        path = os.path.join(run_dir, council_discovery.SNAPSHOT_FILENAME)
        role = _role(model=VEGA, effort="brisk", mode="routed")

        def write(text, mode=0o600):
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(path, mode)

        valid = json.dumps(_snapshot())
        cases = (
            (lambda: write("{not json"), "is not valid JSON"),
            (lambda: write('{"schema": "x"}'), "does not match"),
            (lambda: write(valid, 0o644), "not the private 0600 file"),
            (lambda: os.symlink(os.devnull, path), "is a symlink"),
        )
        for setup, fragment in cases:
            with contextlib.suppress(FileNotFoundError):
                os.remove(path)
            setup()
            planning, problem = council_discovery._read_snapshot(run_dir)
            with self.subTest(fragment=fragment):
                self.assertIsNone(planning)
                err = self._rejects([role], "requires this run's discovery "
                                            "snapshot (", problem=problem)
                self.assertIn(fragment, err)

    def test_snapshot_id_must_identify_this_runs_snapshot(self):
        self._rejects(
            [_role(model=VEGA, effort="brisk", mode="routed",
                   snapshot_id="ffffffffffffffff")],
            "snapshot_id 'ffffffffffffffff' does not identify this run's "
            f"discovery snapshot '{SNAPSHOT_ID}'",
            planning=_snapshot())

    def test_routed_requires_eligible_routing(self):
        self._rejects(
            [_role(model=VEGA, effort="brisk", mode="routed")],
            "discovery reported routing unavailable (not signed in: catalog "
            "is not account-grounded); omit model, effort, and selection to "
            "inherit",
            planning=_snapshot(account={"type": None,
                                        "requires_openai_auth": True}))
        self._rejects(
            [_role(model=VEGA, effort="brisk", mode="routed")],
            "discovery reported routing unavailable (discovery unavailable: "
            "codex_missing)",
            planning=_unavailable("codex_missing"))

    def test_routed_model_must_be_an_advertised_execution_id(self):
        self._rejects(
            [_role(model="future-invented-2099", effort="brisk",
                   mode="routed")],
            "model 'future-invented-2099' is not an advertised execution id "
            f"in snapshot {SNAPSHOT_ID}.",
            planning=_snapshot())
        # A picker id is not what -m receives: point at the dispatch id.
        self._rejects(
            [_role(model="picker-orion", effort="brisk", mode="routed")],
            f"not an advertised execution id in snapshot {SNAPSHOT_ID}; use "
            f"the execution id '{NATIVE}'",
            planning=_snapshot())

    def test_routed_rejects_hidden_and_retired_models(self):
        self._rejects(
            [_role(model=HIDDEN, effort="brisk", mode="routed")],
            f"cannot route to hidden model '{HIDDEN}'", planning=_snapshot())
        for now in (AFTER_LYRA_RETIRES, LYRA_RETIRES):
            with self.subTest(now=now):
                self._rejects(
                    [_role(model=LYRA, effort="brisk", mode="routed")],
                    f"model '{LYRA}' advertised retirement passed "
                    "(2031-01-01T00:00:00Z)",
                    planning=_snapshot(), now=now)
        # Before the date the same pair is valid.
        for now in (NOW, BEFORE_LYRA_RETIRES):
            self._validate([_role(model=LYRA, effort="brisk", mode="routed")],
                           planning=_snapshot(), now=now)

    def test_effort_must_be_advertised_for_that_model_exactly(self):
        for effort in ("adaptive-v2", "Brisk", "BRISK"):
            with self.subTest(effort=effort):
                self._rejects(
                    [_role(model=VEGA, effort=effort, mode="routed")],
                    f"effort '{effort}' is not advertised for model '{VEGA}'",
                    planning=_snapshot())

    def test_native_effort_requires_a_proven_native_model(self):
        """Managed new-thread defaults make effort-only unavailable."""
        self._rejects(
            [_role(effort="brisk", mode="native_effort")],
            "cannot adjust effort on the native model: managed new-thread "
            "defaults present; omit effort and selection to inherit",
            planning=_snapshot(managed=_managed_present()))
        self._rejects(
            [_role(effort="brisk", mode="native_effort")],
            "cannot adjust effort on the native model: no model is "
            "configured",
            planning=_snapshot(configured={
                "model": None, "effort": None, "provider": None,
                "model_origin": None, "effort_origin": None,
                "endpoint_overrides": [], "catalog_override": False}))
        self._rejects(
            [_role(effort="max-plus", mode="native_effort")],
            f"effort 'max-plus' is not advertised for model '{NATIVE}'",
            planning=_snapshot())

    def test_valid_automatic_selections_pass(self):
        self._validate([
            _role("a", model=VEGA, effort="deliberate", mode="routed"),
            _role("b", effort="adaptive-v2", mode="native_effort"),
        ], planning=_snapshot())

    def test_explicit_pins_and_inheritance_are_never_checked(self):
        self._validate([_role("a"), _role("b", model=CUSTOM, mode="user"),
                        _role("c", model="future-invented-2099")])

    def test_routing_off_is_not_an_authoring_error(self):
        self._validate([_role(model=VEGA, effort="brisk", mode="routed"),
                        _role(effort="brisk", mode="native_effort")],
                       routing_mode="off",
                       problem="model-snapshot.json does not exist")

    def test_entry_index_names_the_defective_role(self):
        self._rejects(
            [_role("a"), _role("b", model="nope-1", effort="brisk",
                               mode="routed")],
            "--roles-file entry 1 (id 'b'): model 'nope-1'",
            planning=_snapshot())


# ---------- preflight: the selection plan ----------

class PreflightPlanTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.run_dir = os.path.join(tmp.name, "run")
        os.mkdir(self.run_dir, 0o700)
        codex_home = os.path.join(tmp.name, "codex-home")
        os.mkdir(codex_home)
        for patcher in (
            patch("shutil.which", return_value="/fake/bin/codex"),
            patch.dict(os.environ, _clean_env(CODEX_HOME=codex_home),
                       clear=True),
            patch.object(council_selection, "_discover",
                         side_effect=AssertionError("preflight discovered")),
            patch.object(council_discovery, "_discover",
                         side_effect=AssertionError("preflight discovered")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        with open(os.path.join(self.run_dir, "context.md"), "w",
                  encoding="utf-8") as f:
            f.write("please review\n")

    def _stage(self, entries, snapshot=None):
        with open(os.path.join(self.run_dir, "roles.json"), "w",
                  encoding="utf-8") as f:
            json.dump(entries, f)
        if snapshot is not None:
            council_discovery._write_snapshot(self.run_dir, snapshot)

    def _preflight(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            codex_council._check_staging_dir(self.run_dir)
        return out.getvalue().splitlines()

    def test_plan_lines_for_every_selection(self):
        self._stage([
            _entry("plain"),
            _entry("pin", model=CUSTOM, effort="brisk",
                   selection={"mode": "user"}),
            _routed("route", model=VEGA, effort="deliberate"),
            _native_effort("tune", effort="adaptive-v2"),
        ], _snapshot())
        lines = self._preflight()
        self.assertRegex(lines[0], r"^\[codex-council\] staging OK: .+ "
                                   r"\(4 roles; max parallel 6\) version=\S+$")
        self.assertEqual(lines[1:], [
            "[codex-council] selection plan: plain: native inheritance",
            "[codex-council] selection plan: pin: explicit override (model "
            f"{CUSTOM}, effort brisk); unverified: not in the discovered "
            "catalog; forwarded unchanged",
            "[codex-council] selection plan: route: routed (model "
            f"{VEGA}, effort deliberate); revalidated at launch",
            "[codex-council] selection plan: tune: native-model effort "
            f"(effort adaptive-v2 on native model {NATIVE}); revalidated at "
            "launch",
        ])

    def test_routing_off_plans_native_inheritance(self):
        self._stage([_routed("route"), _native_effort("tune")])
        with patch.dict(os.environ, {ROUTING_ENV: "off"}):
            lines = self._preflight()
        self.assertEqual(lines[1:], [
            "[codex-council] selection plan: route: native inheritance "
            "(routing off)",
            "[codex-council] selection plan: tune: native inheritance "
            "(routing off)",
        ])

    def test_authoring_defect_exits_2_before_staging_ok(self):
        self._stage([_routed(model="future-invented-2099")], _snapshot())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _assert_usage_exit(
                self, self._preflight,
                expect_in_stderr="is not an advertised execution id")
        self.assertEqual(out.getvalue(), "")

    def test_untagged_pins_are_refused(self):
        self._stage([_entry(model=CUSTOM)])
        _assert_usage_exit(self, self._preflight,
                           expect_in_stderr="declare selection.mode")

    def test_invalid_routing_env_is_a_usage_error(self):
        self._stage([_entry()])
        with patch.dict(os.environ, {ROUTING_ENV: "sometimes"}):
            _assert_usage_exit(
                self, self._preflight,
                expect_in_stderr="CODEX_COUNCIL_MODEL_ROUTING must be 'auto' "
                                 "or 'off'; got 'sometimes'")


# ---------- launch-time resolution (in-process, discovery patched) ----------

class LaunchSelectionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.run_dir = tmp.name

    def _resolve_launch(self, roles, routing_mode="auto", launch=None):
        calls = []

        def fake_discover(mode):
            calls.append(mode)
            return launch if launch is not None else _snapshot()

        with patch.object(council_selection, "_discover",
                          side_effect=fake_discover):
            resolved, snapshot = council_selection._resolve_run_selections(
                roles, self.run_dir, routing_mode, at_launch=True)
        return resolved, snapshot, calls

    def test_no_automatic_role_means_no_launch_discovery(self):
        roles = [_role("a"), _role("b", model=CUSTOM, mode="user")]
        resolved, launch, calls = self._resolve_launch(roles)
        self.assertEqual(calls, [])
        self.assertIsNone(launch)
        self.assertEqual([r.decision.provenance for r in resolved],
                         ["native", "user"])

    def test_one_frozen_discovery_serves_every_automatic_role(self):
        council_discovery._write_snapshot(self.run_dir, _snapshot())
        roles = [_role("a", model=VEGA, effort="brisk", mode="routed"),
                 _role("b", effort="adaptive-v2", mode="native_effort"),
                 _role("c", model=LYRA, effort="deliberate", mode="routed")]
        resolved, launch, calls = self._resolve_launch(roles)
        self.assertEqual(calls, ["auto"])
        self.assertIsNotNone(launch)
        self.assertEqual(
            [(r.decision.provenance, r.decision.dispatch_model)
             for r in resolved],
            [("routed", VEGA), ("native_effort", NATIVE), ("routed", LYRA)])
        # The launch snapshot is never written over the planning one.
        planning, _ = council_discovery._read_snapshot(self.run_dir)
        self.assertEqual(planning["snapshot_id"], SNAPSHOT_ID)

    def test_routing_off_skips_launch_discovery(self):
        roles = [_role("a", model=VEGA, effort="brisk", mode="routed")]
        resolved, launch, calls = self._resolve_launch(roles, "off")
        self.assertEqual(calls, [])
        self.assertIsNone(launch)
        self.assertEqual(resolved[0].decision.note,
                         "CODEX_COUNCIL_MODEL_ROUTING=off")

    def test_authoring_defects_exit_before_discovery(self):
        roles = [_role("a", model=VEGA, effort="brisk", mode="routed")]
        _assert_usage_exit(self, lambda: self._resolve_launch(roles),
                           expect_in_stderr="requires this run's discovery "
                                            "snapshot")

    def _lyra_retiring_at(self, retirement):
        """A catalog whose LYRA entry retires at `retirement` (ISO UTC)."""
        epoch = calendar.timegm(time.strptime(retirement,
                                              "%Y-%m-%dT%H:%M:%SZ"))
        return [fake_codex.model_entry(LYRA, upgradeInfo={
            "model": VEGA, "retirementAt": epoch})] + [
            e for e in fake_codex.default_catalog() if e["model"] != LYRA]

    def test_a_retirement_after_discovery_falls_back_instead_of_exiting(self):
        """Authoring is judged as of discovery; a retirement that passes
        between discovery (created_at 2026-09-27T12:00:00Z) and launch is
        changed evidence, never an exit 2."""
        launch_clock = calendar.timegm((2026, 9, 28, 0, 0, 0))
        roles = [_role("a", model=LYRA, effort="brisk", mode="routed")]
        for retirement, outcome in (
            ("2026-09-27T12:00:01Z", "fallback"),   # after discovery
            ("2026-09-27T12:00:00Z", "exit"),       # at discovery: retired
            ("2026-09-27T11:59:59Z", "exit"),       # before discovery
        ):
            entries = self._lyra_retiring_at(retirement)
            council_discovery._write_snapshot(
                self.run_dir, _snapshot(entries=entries))
            with self.subTest(retirement=retirement), patch.object(
                    council_selection.time, "time",
                    return_value=launch_clock):
                if outcome == "exit":
                    _assert_usage_exit(
                        self, lambda e=entries: self._resolve_launch(
                            roles, launch=_snapshot(entries=e)),
                        expect_in_stderr=f"model '{LYRA}' advertised "
                                         f"retirement passed ({retirement})")
                    continue
                resolved, _, calls = self._resolve_launch(
                    roles, launch=_snapshot(entries=entries))
                self.assertEqual(calls, ["auto"])
                decision = resolved[0].decision
                self.assertEqual(decision.provenance, "fallback")
                self.assertEqual(
                    decision.note,
                    "selection evidence changed since discovery: model "
                    f"'{LYRA}' advertised retirement passed ({retirement})")


    def test_a_retirement_passing_during_launch_discovery_falls_back(self):
        """The resolver's clock is read after launch discovery: a model
        whose retirement passes while discovery runs is already retired
        when its evidence is judged."""
        entries = self._lyra_retiring_at(LYRA_RETIRES)
        planning = dict(_snapshot(entries=entries),
                        created_at="2030-12-31T23:59:00Z")
        council_discovery._write_snapshot(self.run_dir, planning)
        roles = [_role("a", model=LYRA, effort="brisk", mode="routed")]
        clock = [calendar.timegm((2030, 12, 31, 23, 59, 59))]

        def discover_across_the_retirement(mode):
            clock[0] = calendar.timegm((2031, 1, 1, 0, 0, 1))
            return _snapshot(entries=entries)

        with patch.object(council_selection, "_discover",
                          side_effect=discover_across_the_retirement), \
             patch.object(council_selection.time, "time",
                          side_effect=lambda: clock[0]):
            resolved, _ = council_selection._resolve_run_selections(
                roles, self.run_dir, "auto", at_launch=True)
        decision = resolved[0].decision
        self.assertEqual(decision.provenance, "fallback")
        self.assertIsNone(decision.dispatch_model)
        self.assertEqual(
            decision.note,
            "selection evidence changed since discovery: model "
            f"'{LYRA}' advertised retirement passed ({LYRA_RETIRES})")


# ---------- structured failure records ----------

def _api_failure(status, error, *, event="turn.failed"):
    """One codex failure event whose message is the JSON-in-message form."""
    message = json.dumps({"type": "error", "status": status, "error": error})
    if event == "error":
        return json.dumps({"type": "error", "message": message})
    return json.dumps({"type": "turn.failed", "error": {"message": message}})


def _text_failure(message):
    """Codex's `error` plus `turn.failed` events carrying plain text."""
    return "\n".join([
        json.dumps({"type": "error", "message": message}),
        json.dumps({"type": "turn.failed", "error": {"message": message}}),
    ])


NOT_FOUND = {"type": "invalid_request_error", "code": "model_not_found",
             "param": "model",
             "message": f"The model '{VEGA}' does not exist or you do not "
                        "have access to it."}
# The API quota error codes the [quota] tag must recognize (terminal, never
# retried as a 429). API error codes, not model names.
SPEC_QUOTA_CODES = (
    "insufficient_quota", "usage_limit_reached", "usage_limit_exceeded",
    "credit_balance_exhausted", "organization_spend_limit_exceeded",
    "project_spend_limit_exceeded", "organization_usage_limit_exceeded",
)


class FailureRecordTests(unittest.TestCase):
    def test_nested_json_message_becomes_one_structured_record(self):
        stdout = "\n".join([_api_failure(404, NOT_FOUND, event="error"),
                            _api_failure(404, NOT_FOUND)])
        self.assertEqual(council_failures._failure_records(stdout), [
            council_failures.FailureRecord(
                404, "invalid_request_error", "model_not_found", "model",
                NOT_FOUND["message"])])

    def test_direct_error_objects_and_plain_messages(self):
        stdout = "\n".join([
            json.dumps({"type": "error", "error": {
                "code": "insufficient_quota", "message": "No credit."}}),
            json.dumps({"type": "turn.failed", "error": "plain failure"}),
        ])
        self.assertEqual(council_failures._failure_records(stdout), [
            council_failures.FailureRecord(None, None, "insufficient_quota",
                                           None, "No credit."),
            council_failures.FailureRecord(message="plain failure"),
        ])

    def test_a_boolean_status_is_not_an_http_status(self):
        """JSON true is a bool, never HTTP status 1: the record carries no
        status, so a model_not_found code still counts as a rejection."""
        stdout = _api_failure(True, NOT_FOUND)
        records = council_failures._failure_records(stdout)
        self.assertEqual(records, [council_failures.FailureRecord(
            None, "invalid_request_error", "model_not_found", "model",
            NOT_FOUND["message"])])
        self.assertEqual(council_failures._failure_verdict(
            council_failures._failure_text(stdout, ""), records, VEGA).kind,
            "model-rejected")

    def test_json_in_message_decoding_is_bounded(self):
        inner = {"code": "model_not_found", "message": "deepest"}
        message = json.dumps(inner)
        for _ in range(3):
            message = json.dumps({"error": {"message": message}})
        records = council_failures._failure_records(
            json.dumps({"type": "error", "message": message}))
        self.assertFalse(any(r.code == "model_not_found" for r in records))
        self.assertEqual(records[-1].message, json.dumps(inner))
        # Three levels are decoded.
        message = json.dumps(inner)
        for _ in range(2):
            message = json.dumps({"error": {"message": message}})
        records = council_failures._failure_records(
            json.dumps({"type": "error", "message": message}))
        self.assertEqual(records[-1].code, "model_not_found")

    def test_only_error_and_turn_failed_events_are_read(self):
        stdout = "\n".join(
            json.dumps({"type": "item.completed", "item": {
                "type": item_type, "text": _api_failure(404, NOT_FOUND),
                "message": _api_failure(404, NOT_FOUND)}})
            for item_type in ("agent_message", "reasoning", "error",
                              "command_execution"))
        self.assertEqual(council_failures._failure_records(stdout), [])


# ---------- failure classification (both paths) ----------

class FailureClassificationTests(unittest.TestCase):
    def _classify(self, stdout, stderr="", model=None, resume=False):
        text = council_failures._failure_text(stdout, stderr)
        records = council_failures._failure_records(stdout)
        return council_failures._failure_verdict(
            text, records, model, resume).kind

    def test_quota_codes_and_prose_are_terminal_even_with_429(self):
        # The specified codes, spelled out rather than read back from the
        # implementation's set, so dropping one fails here. The set may
        # grow; a newly recognized code needs no test edit.
        self.assertLessEqual(set(SPEC_QUOTA_CODES),
                             council_failures.QUOTA_ERROR_CODES)
        for code in SPEC_QUOTA_CODES:
            for field in ("code", "type"):
                stdout = _api_failure(429, {field: code, "message": "limit"})
                with self.subTest(code=code, field=field):
                    self.assertEqual(self._classify(stdout), "quota")
                    self.assertEqual(self._classify(stdout, resume=True),
                                     "quota")
        self.assertEqual(
            self._classify("", "You've hit your usage limit. Upgrade to "
                               "continue."), "quota")
        # Unrecognized quota prose stays untagged (and never retriable).
        self.assertIsNone(self._classify("", "quota exceeded"))

    def test_model_ids_shaped_like_statuses_never_name_a_status(self):
        # A valid model id may contain status-looking text; a rejection of
        # it must stay [model-rejected], not [auth] or a retriable class.
        for model in ("future-status401", "custom/http401",
                      "future-status:429", "acme/http:503"):
            stdout = _api_failure(400, dict(
                NOT_FOUND, message=f"The model '{model}' does not exist or "
                                   "you do not have access to it."))
            for resume in (False, True):
                with self.subTest(model=model, resume=resume):
                    self.assertEqual(
                        self._classify(stdout, model=model, resume=resume),
                        "model-rejected")
        # Controls: genuine statuses still classify.
        self.assertEqual(self._classify("", "HTTP 401 Unauthorized",
                                        model="future-status401"), "auth")
        self.assertEqual(self._classify("", "status: 429 Too Many Requests",
                                        model="future-status:429"),
                         "rate-limit")
        self.assertEqual(self._classify("", "HTTP 503 Service Unavailable",
                                        model="acme/http:503"), "5xx")

    def test_structured_model_not_found_is_a_rejection(self):
        for status in (400, 404, None):
            error = dict(NOT_FOUND, message="gone")
            stdout = (_api_failure(status, error) if status else json.dumps(
                {"type": "error", "error": error}))
            with self.subTest(status=status):
                self.assertEqual(self._classify(stdout, model=VEGA),
                                 "model-rejected")
        self.assertIsNone(self._classify(_api_failure(
            410, dict(NOT_FOUND, message="gone")), model=VEGA))

    def test_complete_rejection_sentences_for_the_requested_model(self):
        chatgpt = (f"The '{CUSTOM}' model is not supported when using Codex "
                   "with a ChatGPT account.")
        api = (f"The model '{CUSTOM}' does not exist or you do not have "
               "access to it.")
        for sentence in (chatgpt, api):
            stdout = _api_failure(400, {"type": "invalid_request_error",
                                        "message": sentence})
            with self.subTest(sentence=sentence[:24]):
                self.assertEqual(self._classify(stdout, model=CUSTOM),
                                 "model-rejected")
                # Naming a different model is no evidence about this one.
                self.assertIsNone(self._classify(stdout, model=VEGA))
                # With no model sent, the native model was rejected.
                self.assertEqual(self._classify(stdout), "model-rejected")
        # The requested model is regex-escaped: "." is not a wildcard.
        stdout = _api_failure(400, {"message": chatgpt.replace(
            CUSTOM, "acmeX/future-review-2034:rev2")})
        self.assertIsNone(self._classify(stdout, model="acme./future-review"
                                                       "-2034:rev2"))

    def test_rejection_sentences_accept_a_backtick_quoted_model(self):
        """The OpenAI API's model_not_found sentence quotes the model in
        backticks. Codex passes it through as JSON-in-message, or as text
        after its "unexpected status NNN ...: " prefix (with the body's
        message or the raw JSON body), and every form is a rejection of
        the model it names, on both paths."""
        api = (f"The model `{VEGA}` does not exist or you do not have "
               "access to it.")
        chatgpt = (f"The `{VEGA}` model is not supported when using Codex "
                   "with a ChatGPT account.")
        suffix = ", url: https://api.example.invalid/v1/responses, cf-ray: x"
        for sentence in (api, chatgpt):
            body = json.dumps({"error": {"message": sentence,
                                         "type": "invalid_request_error"}})
            forms = (
                _api_failure(404, {"type": "invalid_request_error",
                                   "message": sentence}),
                _text_failure(f"unexpected status 404 Not Found: {sentence}"
                              f"{suffix}"),
                _text_failure(f"unexpected status 404 Not Found: {body}"
                              f"{suffix}"),
            )
            for stdout in forms:
                for resume in (False, True):
                    with self.subTest(sentence=sentence[:24],
                                      stdout=stdout[:48], resume=resume):
                        self.assertEqual(
                            self._classify(stdout, model=VEGA, resume=resume),
                            "model-rejected")
                        self.assertEqual(
                            self._classify(stdout, resume=resume),
                            "model-rejected")
                        self.assertIsNone(
                            self._classify(stdout, model=CUSTOM,
                                           resume=resume))
        # Codex's own text is what the tag quotes.
        stdout = _text_failure(f"unexpected status 404 Not Found: {api}")
        routed = council_selection.SelectionDecision(
            "routed", dispatch_model=VEGA)
        self.assertIn(
            f"this invocation: unexpected status 404 Not Found: "
            f"{api.rstrip('.')}. No substitute",
            _tag(
                council_failures._failure_text(stdout, ""), 1, "exec",
                council_failures._failure_records(stdout), routed))

    def test_a_usage_limit_for_one_model_ends_with_that_models_action(self):
        """Codex names a usage cap that applies to one model. It stays a
        terminal [quota] on both paths, and the tag ends with the action for
        the model that was sent, as for a rejection of it. The limit's label
        is the server's, never compared with the model sent. A plan-wide
        usage limit keeps Codex's text alone."""
        per_model = _text_failure(
            "You’ve hit your usage limit for Future-Vega-Tier. Switch to "
            "another model now, or try again at 3:05 PM.")
        plan = _text_failure("You've hit your usage limit. Upgrade to "
                             "continue, or try again at 3:05 PM.")
        native_action = ("Ask the user to update the Codex configuration "
                         "(model) or to name a model to pin.")
        for stdout in (per_model, plan):
            for resume in (False, True):
                with self.subTest(stdout=stdout[:60], resume=resume):
                    self.assertEqual(
                        self._classify(stdout, model=VEGA, resume=resume),
                        "quota")
        text = council_failures._failure_text(per_model, "")
        records = council_failures._failure_records(per_model)
        for provenance, model, action in (
            ("routed", VEGA, "Re-run this role with model, effort, and "
                             "selection omitted to inherit native "
                             "configuration."),
            ("user", VEGA, "Change or remove the explicit pin."),
            ("user", None, native_action),
            ("native_effort", NATIVE, native_action),
            ("native", None, native_action),
        ):
            decision = council_selection.SelectionDecision(
                provenance, dispatch_model=model)
            for phase in ("exec", "resume"):
                with self.subTest(provenance=provenance, model=model,
                                  phase=phase):
                    self.assertEqual(
                        _tag(
                            text, 1, phase, records, decision),
                        f"[quota] {text} {action}")
        routed = council_selection.SelectionDecision(
            "routed", dispatch_model=VEGA)
        text = council_failures._failure_text(plan, "")
        self.assertEqual(
            _tag(
                text, 1, "exec", council_failures._failure_records(plan),
                routed),
            f"[quota] {text}")

    def test_a_refused_model_that_is_the_native_one_asks_the_user(self):
        """A routed or pinned model that discovery proved is the native
        model is what an inheriting re-run would send again: its rejection
        and its per-model usage limit both give the native model's action,
        and the rejection says why. A different proven native model, or
        none, keeps the provenance's own action."""
        inherit = ("Re-run this role with model, effort, and selection "
                   "omitted to inherit native configuration.")
        change_pin = "Change or remove the explicit pin."
        native_action = ("Ask the user to update the Codex configuration "
                         "(model) or to name a model to pin.")
        rejection = _api_failure(404, dict(
            NOT_FOUND, message=f"The model '{NATIVE}' does not exist or you "
                               "do not have access to it."))
        usage = _text_failure(
            "You’ve hit your usage limit for Future-Orion-Tier. Switch to "
            "another model now, or try again at 3:05 PM.")
        for provenance, native, action in (
            ("routed", NATIVE, native_action),
            ("user", NATIVE, native_action),
            ("routed", VEGA, inherit),
            ("routed", None, inherit),
            ("user", VEGA, change_pin),
            ("user", None, change_pin),
        ):
            decision = council_selection.SelectionDecision(
                provenance, NATIVE, "brisk", NATIVE, "brisk",
                native_model=native)
            for phase in ("exec", "resume"):
                with self.subTest(provenance=provenance, native=native,
                                  phase=phase):
                    tagged = _tag(
                        council_failures._failure_text(rejection, ""), 1,
                        phase, council_failures._failure_records(rejection),
                        decision)
                    self.assertTrue(tagged.startswith("[model-rejected] "),
                                    tagged)
                    self.assertTrue(tagged.endswith(action), tagged)
                    self.assertEqual(
                        "which is also the natively configured model" in tagged,
                        native == NATIVE, tagged)
                    text = council_failures._failure_text(usage, "")
                    self.assertEqual(
                        _tag(
                            text, 1, phase,
                            council_failures._failure_records(usage),
                            decision),
                        f"[quota] {text} {action}")

    def test_near_misses_are_not_rejections(self):
        cases = (
            _api_failure(400, {"message": "Resource not found"}),
            _api_failure(400, {"message": f"model {VEGA} not supported"}),
            json.dumps({"type": "error", "message": (
                f"Model metadata for `{VEGA}` not found. Defaulting to "
                "fallback metadata; this can degrade performance and cause "
                "issues.")}),
            _api_failure(400, {
                "type": "invalid_request_error", "code": "unsupported_value",
                "param": "reasoning.effort",
                "message": f"Unsupported value: 'ultra' is not supported "
                           f"with the '{VEGA}' model."}),
            _api_failure(400, dict(NOT_FOUND, param="reasoning.effort")),
            _api_failure(400, dict(NOT_FOUND, param="service_tier")),
            _api_failure(400, {"message": (
                f"The model '{VEGA}' does not exist or you do not have "
                "access to it (model_reasoning_effort).")}),
        )
        for stdout in cases:
            with self.subTest(stdout=stdout[:60]):
                self.assertNotEqual(self._classify(stdout, model=VEGA),
                                    "model-rejected")
        # With no structured record to veto it, a stderr line that names
        # reasoning effort or service tier is still about that setting,
        # even when it also carries a complete rejection sentence.
        for stderr in (
            f"The model '{VEGA}' does not exist or you do not have access to "
            "it (model_reasoning_effort).",
            f"The model '{VEGA}' does not exist or you do not have access to "
            "it: reasoning.effort",
            f"The '{VEGA}' model is not supported when using Codex with a "
            "ChatGPT account (service_tier).",
        ):
            for resume in (False, True):
                with self.subTest(stderr=stderr[-32:], resume=resume):
                    self.assertNotEqual(
                        self._classify("", stderr, VEGA, resume),
                        "model-rejected")
        self.assertEqual(
            self._classify("", "Selected model is at capacity. Please try a "
                               "different model.", model=VEGA), "5xx")

    def test_unrelated_text_naming_a_setting_does_not_mask_a_rejection(self):
        self.assertEqual(
            self._classify(_api_failure(404, NOT_FOUND),
                           "warning: unknown config key service_tier", VEGA),
            "model-rejected")
        # A record's structured param decides what it is about: `model`
        # here, whatever else its message mentions.
        about_model = _api_failure(400, {
            "type": "invalid_request_error", "param": "model",
            "message": NOT_FOUND["message"] + " (see service_tier)"})
        for resume in (False, True):
            self.assertEqual(
                self._classify(about_model, model=VEGA, resume=resume),
                "model-rejected")

    def test_a_setting_record_never_hides_a_rejection_record(self):
        """Records are judged one by one: an unsupported-effort record says
        nothing about another record's model_not_found, in either order and
        on both paths, even when the rejection also looks stale."""
        effort = _api_failure(400, {
            "type": "invalid_request_error", "code": "unsupported_value",
            "param": "reasoning.effort",
            "message": "Unsupported value: 'ultra' for reasoning.effort."},
            event="error")
        for param in ("model", None):
            rejection = dict(NOT_FOUND, message=NOT_FOUND["message"]
                             + " Thread not found; no rollout found.")
            if param is None:
                del rejection["param"]
            rejection = _api_failure(400, rejection)
            for stdout in ("\n".join([effort, rejection]),
                           "\n".join([rejection, effort])):
                for resume in (False, True):
                    with self.subTest(param=param, first=stdout[:30],
                                      resume=resume):
                        self.assertEqual(
                            self._classify(stdout, model=VEGA,
                                           resume=resume),
                            "model-rejected")

    def test_model_ids_may_contain_the_setting_names(self):
        """A model id is free to contain reasoning.effort, service_tier, or
        model_reasoning_effort: the words inside the id never turn a
        rejection of it into a setting failure (or a stale thread)."""
        for model in ("future-service_tier-2035",
                      "future-model_reasoning_effort-2035",
                      "future-reasoning.effort-2035", "service_tier"):
            api = (f"The model '{model}' does not exist or you do not have "
                   "access to it.")
            chatgpt = (f"The `{model}` model is not supported when using "
                       "Codex with a ChatGPT account.")
            forms = (
                _api_failure(400, {"type": "invalid_request_error",
                                   "code": "model_not_found", "param": "model",
                                   "message": api + " Thread not found."}),
                _api_failure(400, {"type": "invalid_request_error",
                                   "message": chatgpt + " Thread not found."}),
                _text_failure(f"unexpected status 404 Not Found: {api}"),
            )
            for stdout in forms:
                for resume in (False, True):
                    with self.subTest(model=model, stdout=stdout[:40],
                                      resume=resume):
                        self.assertEqual(
                            self._classify(stdout, model=model,
                                           resume=resume),
                            "model-rejected")
            # A stderr-only sentence counts the same way.
            for resume in (False, True):
                self.assertEqual(
                    self._classify("", api + " Thread not found.", model,
                                   resume), "model-rejected")
        # Outside the quoted id, a setting name still marks the text as
        # about that setting.
        model = "future-service_tier-2035"
        self.assertNotEqual(self._classify("", (
            f"The model '{model}' does not exist or you do not have access "
            "to it (service_tier)."), model), "model-rejected")

    def test_structured_authentication_failures_are_auth_first(self):
        """HTTP 401, or an authentication error type or code, is [auth]
        before stale matching: it never clears state or restarts fresh."""
        for error, status in (
            ({"type": "authentication_error", "code": "invalid_api_key",
              "message": "Thread not found."}, 401),
            ({"type": "invalid_request_error", "code": "invalid_api_key",
              "message": "Thread not found; no rollout found."}, None),
            ({"type": "authentication_error",
              "message": "Session expired."}, None),
            ({"message": "Thread not found."}, 401),
        ):
            stdout = (_api_failure(status, error) if status else json.dumps(
                {"type": "error", "error": error}))
            for resume in (False, True):
                with self.subTest(error=error, status=status, resume=resume):
                    self.assertEqual(
                        self._classify(stdout, model=VEGA, resume=resume),
                        "auth")
        for resume in (False, True):
            self.assertEqual(self._classify(
                "", "HTTP 401 while resuming; thread not found",
                resume=resume), "auth")

    def test_precedence_on_both_paths(self):
        rejection_with_stale = _api_failure(400, dict(
            NOT_FOUND, message=NOT_FOUND["message"] + " Thread not found."))
        cases = (
            # (stdout, stderr, fresh verdict, resume verdict)
            (_api_failure(429, {"code": "insufficient_quota",
                                "message": "401 unauthorized"}), "",
             "auth", "auth"),
            (_api_failure(429, {"code": "insufficient_quota",
                                "message": "thread not found"}), "",
             "quota", "quota"),
            (_api_failure(503, dict(NOT_FOUND, message="thread not found")),
             "", "5xx", "5xx"),
            (rejection_with_stale, "", "model-rejected", "model-rejected"),
            ("", "Error: no rollout found for thread id stale-429-sid",
             None, "stale"),
            ("", "HTTP 429 Too Many Requests; thread not found",
             "rate-limit", "rate-limit"),
            ("", "thread not found; too many requests", "rate-limit",
             "stale"),
            ('', '{"error": {"message": "service unavailable for this '
                 'account tier", "type": "invalid_request_error"}}',
             None, None),
        )
        for stdout, stderr, fresh, resumed in cases:
            with self.subTest(stdout=stdout[:40], stderr=stderr[:40]):
                self.assertEqual(self._classify(stdout, stderr, VEGA), fresh)
                self.assertEqual(
                    self._classify(stdout, stderr, VEGA, resume=True), resumed)
        # An anchored 429/5xx outranks even a complete rejection sentence
        # naming the requested model. At HTTP 400 the same text is a
        # rejection; a transient failure stays retriable, never terminal.
        for sentence in (NOT_FOUND["message"],
                         f"The '{VEGA}' model is not supported when using "
                         "Codex with a ChatGPT account."):
            for status, verdict in ((400, "model-rejected"), (503, "5xx"),
                                    (429, "rate-limit")):
                stdout = _api_failure(status, {"message": sentence})
                for resume in (False, True):
                    with self.subTest(sentence=sentence[:24], status=status,
                                      resume=resume):
                        self.assertEqual(
                            self._classify(stdout, "", VEGA, resume), verdict)

    def test_rejection_message_names_what_was_sent_and_one_action(self):
        stdout = _api_failure(404, NOT_FOUND)
        text = council_failures._failure_text(stdout, "")
        records = council_failures._failure_records(stdout)
        provider = NOT_FOUND["message"].rstrip(".")
        # The action follows WHICH model was refused. The native model (a
        # native_effort role's pin, an effort-only user pin, or plain
        # inheritance) is what an inheriting re-run would send again.
        native_action = ("Ask the user to update the Codex configuration "
                         "(model) or to name a model to pin.")
        for provenance, model, action in (
            ("user", VEGA, "Change or remove the explicit pin."),
            ("user", None, native_action),
            ("routed", VEGA, "Re-run this role with model, effort, and "
                             "selection omitted to inherit native "
                             "configuration."),
            ("native_effort", NATIVE, native_action),
            ("native", None, native_action),
            ("fallback", None, native_action),
        ):
            decision = council_selection.SelectionDecision(
                provenance, dispatch_model=model)
            subject = (f"requested model '{model}'" if model
                       else "natively configured model")
            for phase, kept in (("resume", " and the saved thread was kept"),
                                ("exec", "")):
                with self.subTest(provenance=provenance, model=model,
                                  phase=phase):
                    self.assertEqual(
                        _tag(
                            text, 1, phase, records, decision),
                        f"[model-rejected] Codex rejected the {subject} for "
                        f"this invocation: {provider}. No substitute model "
                        f"was tried{kept}. {action}")

    def test_quota_and_untagged_failures_keep_the_failure_text(self):
        stdout = _api_failure(429, {"code": "insufficient_quota",
                                    "message": "No credit."})
        text = council_failures._failure_text(stdout, "")
        records = council_failures._failure_records(stdout)
        self.assertEqual(
            _tag(text, 1, "exec", records),
            f"[quota] {text}")
        self.assertEqual(
            _tag("502 bad gateway", 1, "exec"),
            "[retriable:5xx] 502 bad gateway")
        self.assertEqual(_tag("", 7, "resume"),
                         "codex resume exited 7")


# ---------- the role runner: dispatch and state on failures ----------

class RunRoleSelectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patcher in (
            patch.object(codex_council, "_project_root",
                         return_value="/fixed/project/root"),
            patch.object(codex_council, "STATE_DIR", tmp.name),
            patch.dict(os.environ, _clean_env(), clear=True),
            patch.object(codex_council.asyncio, "sleep",
                         side_effect=self._no_sleep),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.calls = []

    @staticmethod
    async def _no_sleep(_delay):
        return None

    def _fake(self, *runs):
        """Patch the subprocess with these runs, in order; the last repeats."""
        runs = list(runs)

        async def fake_subproc(cmd, prompt, role_id=""):
            self.calls.append(cmd)
            return runs.pop(0) if len(runs) > 1 else runs[0]

        return patch.object(codex_council, "_run_codex_subprocess",
                            side_effect=fake_subproc)

    @staticmethod
    def _failed(stdout, rc=1):
        return codex_council.CodexRun(returncode=rc, stdout=stdout, stderr="")

    @staticmethod
    def _decided(role, provenance, model=None, effort=None):
        """The role with a launch decision sending (model, effort)."""
        return dataclasses.replace(role, decision=council_selection.SelectionDecision(
            provenance, role.model, role.effort, model, effort))

    async def _attempts(self, role):
        with contextlib.redirect_stderr(io.StringIO()):
            return await codex_council._run_role_attempts(role, "prompt")

    async def test_rejection_with_stale_words_on_resume_keeps_state(self):
        codex_council.save_session("architect", "live-sid")
        stdout = _api_failure(400, dict(
            NOT_FOUND, message=NOT_FOUND["message"] + " Thread not found; no "
                                                     "rollout found."))
        role = self._decided(_role(model=VEGA, mode="user"), "user", VEGA)
        with self._fake(self._failed(stdout)):
            result = await self._attempts(role)
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith(
            f"[model-rejected] Codex rejected the requested model '{VEGA}'"))
        self.assertIn("the saved thread was kept", result.error)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("resume", self.calls[0])
        self.assertEqual(codex_council.load_session("architect")[0],
                         "live-sid")

    async def test_a_rejection_beside_a_setting_record_keeps_state(self):
        """An unsupported-effort record next to a model_not_found record
        (either order) is still a rejection: one subprocess, the saved
        thread unchanged, no stale restart."""
        effort = _api_failure(400, {
            "type": "invalid_request_error", "code": "unsupported_value",
            "param": "reasoning.effort",
            "message": "Unsupported value: 'ultra' for reasoning.effort."},
            event="error")
        rejection = _api_failure(400, dict(
            NOT_FOUND, message=NOT_FOUND["message"] + " Thread not found."))
        role = self._decided(_role(model=VEGA, effort="ultra", mode="user"),
                             "user", VEGA, "ultra")
        for stdout in ("\n".join([effort, rejection]),
                       "\n".join([rejection, effort])):
            self.calls.clear()
            codex_council.save_session("architect", "live-sid")
            with self.subTest(first=stdout[:30]), \
                    self._fake(self._failed(stdout)):
                result = await self._attempts(role)
                self.assertTrue(result.error.startswith(
                    "[model-rejected] "), result.error)
                self.assertIn("the saved thread was kept", result.error)
                self.assertEqual(len(self.calls), 1)
                self.assertIn("resume", self.calls[0])
                self.assertEqual(codex_council.load_session("architect")[0],
                                 "live-sid")

    async def test_a_rejected_model_id_naming_a_setting_keeps_state(self):
        for model in ("future-service_tier-2035",
                      "future-model_reasoning_effort-2035",
                      "future-reasoning.effort-2035"):
            stdout = _api_failure(400, {
                "type": "invalid_request_error", "code": "model_not_found",
                "param": "model",
                "message": f"The model '{model}' does not exist or you do "
                           "not have access to it. Thread not found."})
            role = self._decided(_role(model=model, mode="user"), "user",
                                 model)
            self.calls.clear()
            codex_council.save_session("architect", "live-sid")
            with self.subTest(model=model), self._fake(self._failed(stdout)):
                result = await self._attempts(role)
                self.assertTrue(result.error.startswith(
                    f"[model-rejected] Codex rejected the requested model "
                    f"'{model}'"), result.error)
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(codex_council.load_session("architect")[0],
                                 "live-sid")

    async def test_structured_auth_with_stale_words_keeps_state(self):
        stdout = _api_failure(401, {
            "type": "authentication_error", "code": "invalid_api_key",
            "message": "Thread not found."})
        codex_council.save_session("architect", "live-sid")
        with self._fake(self._failed(stdout)):
            result = await self._attempts(_role())
        self.assertTrue(result.error.startswith("[auth] "), result.error)
        self.assertFalse(result.retriable)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(codex_council.load_session("architect")[0],
                         "live-sid")

    async def test_provider_text_shaped_like_a_tag_is_never_retried(self):
        """An untagged failure keeps Codex's text, which may begin with
        "[retriable:"; the retry decision is the verdict's, so it runs
        once on either path."""
        text = "[retriable:provider-tag] This request is permanently invalid."
        for saved in (None, "live-sid"):
            self.calls.clear()
            if saved:
                codex_council.save_session("architect", saved)
            with self.subTest(saved=saved), \
                    self._fake(self._failed(_text_failure(text))):
                result = await self._attempts(_role())
                self.assertTrue(result.error.startswith(text), result.error)
                self.assertFalse(result.retriable)
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(result.attempts, 1)
        # A genuine transient failure is retried from the same data.
        self.calls.clear()
        with self._fake(self._failed(_api_failure(
                503, {"message": "upstream overloaded"}))):
            result = await self._attempts(_role())
        self.assertTrue(result.error.startswith("[retriable:5xx] "))
        self.assertTrue(result.retriable)
        self.assertEqual(len(self.calls), codex_council.MAX_RETRY_ATTEMPTS)

    async def test_rejection_on_the_fresh_path_is_terminal(self):
        role = self._decided(_role(model=VEGA, effort="brisk",
                                   mode="routed"), "routed", VEGA, "brisk")
        with self._fake(self._failed(_api_failure(404, NOT_FOUND))):
            result = await self._attempts(role)
        self.assertTrue(result.error.startswith("[model-rejected]"))
        self.assertTrue(result.error.endswith(
            "Re-run this role with model, effort, and selection omitted to "
            "inherit native configuration."))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result.attempts, 1)
        # No substitute: the only command is the one the decision allowed.
        self.assertEqual(self.calls[0][4:8], [
            "-m", VEGA, "-c", 'model_reasoning_effort="brisk"'])

    @staticmethod
    def _sentence_failure(model, extra=""):
        """Codex's text-only ChatGPT rejection sentence naming `model`: no
        structured code, so only the model it names ties it to an
        invocation."""
        return _api_failure(400, {
            "type": "invalid_request_error",
            "message": f"The '{model}' model is not supported when using "
                       f"Codex with a ChatGPT account.{extra}"})

    async def test_a_fallback_role_matches_the_model_it_sent_not_requested(self):
        """A routed role that fell back requested VEGA but sent no model, so
        a sentence naming the native model rejects THIS invocation. On
        resume that outranks the stale-looking words: one subprocess, the
        saved thread kept; the fresh path tags it the same way."""
        stdout = self._sentence_failure(NATIVE, " Thread not found.")
        fell_back = self._decided(
            _role(model=VEGA, effort="brisk", mode="routed"), "fallback")
        for saved in (None, "live-sid"):
            self.calls.clear()
            if saved:
                codex_council.save_session("architect", saved)
            with self.subTest(saved=saved), self._fake(self._failed(stdout)):
                result = await self._attempts(fell_back)
                self.assertTrue(result.error.startswith(
                    "[model-rejected] Codex rejected the natively configured "
                    "model for this invocation"), result.error)
                self.assertTrue(result.error.endswith(
                    "Ask the user to update the Codex configuration (model) "
                    "or to name a model to pin."))
                self.assertEqual(len(self.calls), 1)
                self.assertEqual("resume" in self.calls[0], bool(saved))
                self.assertNotIn("-m", self.calls[0])
                self.assertEqual(codex_council.load_session("architect")[0],
                                 saved)

    async def test_a_sentence_naming_another_model_is_not_this_rejection(self):
        """A native_effort role requested no model but sent the native one:
        a sentence naming a different model says nothing about this
        invocation, on either path, and stays an untagged failure."""
        stdout = self._sentence_failure(VEGA)
        pinned_native = self._decided(
            _role(effort="adaptive-v2", mode="native_effort"),
            "native_effort", NATIVE, "adaptive-v2")
        for saved in (None, "live-sid"):
            self.calls.clear()
            if saved:
                codex_council.save_session("architect", saved)
            with self.subTest(saved=saved), self._fake(self._failed(stdout)):
                result = await self._attempts(pinned_native)
                self.assertFalse(result.error.startswith("["), result.error)
                self.assertIn(f"The '{VEGA}' model is not supported",
                              result.error)
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(self.calls[0][4:6], ["-m", NATIVE])
                self.assertEqual(codex_council.load_session("architect")[0],
                                 saved)

    async def test_a_failed_attempt_is_classified_once_on_both_paths(self):
        """The resume path formats the verdict it branched on: the failure
        is classified, and Codex's rejection parsed, once per attempt."""
        role = self._decided(_role(model=VEGA, mode="user"), "user", VEGA)
        real_verdict = council_failures._failure_verdict
        real_rejection = council_failures._model_rejection
        for saved in (None, "live-sid"):
            self.calls.clear()
            if saved:
                codex_council.save_session("architect", saved)
            verdicts, rejections = [], []

            def verdict_spy(*args, **kwargs):
                verdicts.append(kwargs.get("resume", False))
                return real_verdict(*args, **kwargs)

            def rejection_spy(*args):
                rejections.append(args)
                return real_rejection(*args)

            with self.subTest(saved=saved), \
                 self._fake(self._failed(_api_failure(404, NOT_FOUND))), \
                 patch.object(codex_council, "_failure_verdict",
                              side_effect=verdict_spy), \
                 patch.object(council_failures, "_failure_verdict",
                              side_effect=verdict_spy), \
                 patch.object(council_failures, "_model_rejection",
                              side_effect=rejection_spy):
                result = await self._attempts(role)
                self.assertTrue(result.error.startswith("[model-rejected]"))
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(verdicts, [bool(saved)])
                self.assertEqual(len(rejections), 1)

    async def test_quota_429_is_terminal_and_keeps_state_on_both_paths(self):
        stdout = _api_failure(429, {"type": "insufficient_quota",
                                    "code": "insufficient_quota",
                                    "message": "No credit left."})
        for saved in (None, "live-sid"):
            self.calls.clear()
            if saved:
                codex_council.save_session("architect", saved)
            with self.subTest(saved=saved), \
                 self._fake(self._failed(stdout)):
                result = await self._attempts(_role())
                self.assertTrue(result.error.startswith("[quota] "))
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(codex_council.load_session("architect")[0],
                                 saved)

    async def test_a_routed_models_usage_limit_names_the_inherit_rerun(self):
        """A routed role that hit its model's usage limit fails [quota]
        once, keeps its thread, and is told to re-run inheriting."""
        stdout = _text_failure(
            "You’ve hit your usage limit for Future-Vega-Tier. Switch to "
            "another model now, or try again at 3:05 PM.")
        role = self._decided(_role(model=VEGA, effort="brisk",
                                   mode="routed"), "routed", VEGA, "brisk")
        for saved in (None, "live-sid"):
            self.calls.clear()
            if saved:
                codex_council.save_session("architect", saved)
            with self.subTest(saved=saved), self._fake(self._failed(stdout)):
                result = await self._attempts(role)
                self.assertTrue(result.error.startswith("[quota] "))
                self.assertTrue(result.error.endswith(
                    "Re-run this role with model, effort, and selection "
                    "omitted to inherit native configuration."), result.error)
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(codex_council.load_session("architect")[0],
                                 saved)

    async def test_stale_thread_ids_with_digits_still_restart_fresh(self):
        codex_council.save_session("architect", "stale-429-sid")
        ok = codex_council.CodexRun(returncode=0, stdout="\n".join([
            json.dumps({"type": "thread.started", "thread_id": "new-sid"}),
            json.dumps({"type": "item.completed", "item": {
                "type": "agent_message", "text": "fresh"}}),
        ]), stderr="")
        stale = codex_council.CodexRun(
            returncode=1, stdout="",
            stderr="Error: no rollout found for thread id stale-429-sid")
        with self._fake(stale, ok):
            result = await self._attempts(_role())
        self.assertTrue(result.ok)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(codex_council.load_session("architect")[0], "new-sid")

    async def test_a_fallback_role_resumes_its_saved_thread_bare(self):
        """A routed role that fell back resumes the thread an earlier council
        saved: same UUID, no -m/-c, and the thread is never reset."""
        codex_council.save_session("architect", "live-sid")
        ok = codex_council.CodexRun(returncode=0, stdout="\n".join([
            json.dumps({"type": "thread.started", "thread_id": "live-sid"}),
            json.dumps({"type": "item.completed", "item": {
                "type": "agent_message", "text": "ok"}}),
        ]), stderr="")
        fell_back = self._decided(
            _role(model=VEGA, effort="brisk", mode="routed"), "fallback")
        with self._fake(ok):
            result = await self._attempts(fell_back)
        self.assertTrue(result.ok, result.error)
        (cmd,) = self.calls
        self.assertEqual(cmd[cmd.index("resume") + 1], "live-sid")
        self.assertNotIn("-m", cmd)
        self.assertNotIn("-c", cmd)
        self.assertEqual(codex_council.load_session("architect")[0],
                         "live-sid")

    async def test_commands_carry_only_the_decisions_dispatch_values(self):
        ok = codex_council.CodexRun(returncode=0, stdout="\n".join([
            json.dumps({"type": "thread.started", "thread_id": "sid"}),
            json.dumps({"type": "item.completed", "item": {
                "type": "agent_message", "text": "ok"}}),
        ]), stderr="")
        fell_back = self._decided(
            _role(model=VEGA, effort="brisk", mode="routed"), "fallback")
        pinned_native = self._decided(
            _role(effort="adaptive-v2", mode="native_effort"),
            "native_effort", NATIVE, "adaptive-v2")
        with self._fake(ok):
            await self._attempts(fell_back)
            codex_council.clear_session("architect")
            await self._attempts(pinned_native)
        self.assertNotIn("-m", self.calls[0])
        self.assertNotIn("-c", self.calls[0])
        self.assertEqual(self.calls[1][4:8], [
            "-m", NATIVE, "-c", 'model_reasoning_effort="adaptive-v2"'])


# ---------- reporting ----------

def _decided_role(rid, provenance, *, model=None, effort=None, mode=None,
                  dispatch=(None, None), reason=None, note=None):
    selection = None
    if mode is not None:
        selection = council_selection.Selection(
            mode, SNAPSHOT_ID if mode != "user" else None, reason)
    decision = council_selection.SelectionDecision(
        provenance, model, effort, dispatch[0], dispatch[1], reason, note)
    return council_selection.Role(rid, rid.title(), "i", model, effort,
                                  selection, decision)


def _provenance_roles():
    return [
        _decided_role("plain", "native"),
        _decided_role("pin", "user", model=CUSTOM, effort="brisk",
                      mode="user", dispatch=(CUSTOM, "brisk"),
                      note=council_selection.UNVERIFIED_MODEL_ADVISORY),
        _decided_role("route", "routed", model=VEGA, effort="deliberate",
                      mode="routed", dispatch=(VEGA, "deliberate"),
                      reason="narrow checks"),
        _decided_role("tune", "native_effort", effort="adaptive-v2",
                      mode="native_effort", dispatch=(NATIVE, "adaptive-v2"),
                      reason="hardest judgment"),
        _decided_role("fell", "fallback", model=VEGA, effort="brisk",
                      mode="routed", reason="narrow checks",
                      note="launch discovery unavailable: codex_missing"),
    ]


def _results(roles):
    return [codex_council.RoleResult(role=role, ok=True, text="reply",
                                     elapsed_seconds=1.5) for role in roles]


class ReportingTests(unittest.TestCase):
    def test_summary_notes_for_every_provenance(self):
        report = codex_council._format_report(
            _results(_provenance_roles()), 2.0)
        summary = report.split("## Summary\n\n", 1)[1].split("\n\n", 1)[0]
        self.assertEqual(summary.splitlines(), [
            "- **Plain** [plain]: ok — 1.5s",
            f"- **Pin** [pin]: ok (explicit: model {CUSTOM}, effort brisk) "
            "— 1.5s",
            f"- **Route** [route]: ok (routed: model {VEGA}, effort "
            "deliberate) — 1.5s",
            "- **Tune** [tune]: ok (routed effort: adaptive-v2 on native "
            f"model {NATIVE}) — 1.5s",
            "- **Fell** [fell]: ok (native inheritance; routing fell back) "
            "— 1.5s",
        ])

    def test_model_selection_paragraph_follows_the_summary(self):
        caveat = ("codex exec does not report the model or effort that "
                  "served a turn; values above are what the council sent, "
                  "and \"native inheritance\" means no override was sent.")
        for sentence, expected in (
            (None, "launch discovery not run (no runtime-grounded selections)"),
            ("launch discovery ok (codex-cli 9.9.9)",
             "launch discovery ok (codex-cli 9.9.9)"),
        ):
            report = codex_council._format_report(
                _results([_decided_role("plain", "native")]), 1.0, sentence)
            with self.subTest(sentence=sentence):
                self.assertIn(
                    "- **Plain** [plain]: ok — 1.5s\n\nModel selection: "
                    f"{expected}. {caveat}\n\n## Plain (plain)\n", report)

    def test_discovery_state_and_sentence(self):
        ok = _snapshot()
        cases = (
            (("auto", False, None), ("not-run", "no runtime-grounded "
                                                "selections"),
             "launch discovery not run (no runtime-grounded selections)"),
            (("off", True, None), ("not-run", "CODEX_COUNCIL_MODEL_ROUTING=off"),
             "launch discovery not run (CODEX_COUNCIL_MODEL_ROUTING=off)"),
            (("auto", True, ok), ("ok", None),
             "launch discovery ok (codex-cli 9.9.9)"),
            (("auto", True, _unavailable("codex_missing")),
             ("unavailable", "codex_missing"),
             "launch discovery unavailable: codex_missing"),
            (("auto", True, _snapshot(account={
                "type": None, "requires_openai_auth": True})),
             ("ok", "not signed in: catalog is not account-grounded"),
             "launch discovery ok (codex-cli 9.9.9)"),
        )
        for args, state, sentence in cases:
            with self.subTest(args=args[:2]):
                got = council_selection._launch_discovery_state(*args)
                self.assertEqual(got, state)
                self.assertEqual(
                    council_selection._discovery_sentence(*got, args[2]),
                    sentence)

    def test_role_section_selection_line_for_every_provenance(self):
        lines = [
            codex_council._format_role_section(r)[2]
            for r in _results(_provenance_roles())
        ]
        self.assertEqual(lines, [
            "_Model selection: native inheritance (no model or effort "
            "override sent)_",
            f"_Model selection: explicit override — sent model {CUSTOM}, "
            "effort brisk; unverified: not in the discovered catalog; "
            "forwarded unchanged_",
            f"_Model selection: routed — sent model {VEGA}, effort "
            "deliberate; reason: narrow checks_",
            "_Model selection: routed effort on the native model — sent "
            f"model {NATIVE} (pinned native model), effort adaptive-v2; "
            "reason: hardest judgment_",
            "_Model selection: native inheritance — routing fell back: "
            "launch discovery unavailable: codex_missing; requested model "
            f"{VEGA}, effort brisk_",
        ])

    def test_selection_line_precedes_any_warning(self):
        result = codex_council.RoleResult(
            role=_decided_role("plain", "native"), ok=False, error="boom",
            warning="careful", elapsed_seconds=1.0)
        self.assertEqual(codex_council._format_role_section(result), [
            "## Plain (plain)", "",
            "_Model selection: native inheritance (no model or effort "
            "override sent)_", "",
            "_Warning: careful_", "",
            "_Failed: boom_", "",
        ])

    def test_reply_file_header_fields(self):
        headers = [
            codex_council._format_reply_file(r).split("\n", 1)[0]
            for r in _results(_provenance_roles())
        ]
        prefix = "<!-- codex-council reply id={} status=ok elapsed=1.5s " \
                 "attempts=1 "
        self.assertEqual(headers, [
            prefix.format("plain") + "selection=native -->",
            prefix.format("pin") + f"selection=user model={CUSTOM} "
                                   "effort=brisk -->",
            prefix.format("route") + f"selection=routed model={VEGA} "
                                     "effort=deliberate -->",
            prefix.format("tune") + "selection=native_effort "
                                    f"model={NATIVE} effort=adaptive-v2 -->",
            prefix.format("fell") + "selection=fallback "
                                    f"requested_model={VEGA} "
                                    "requested_effort=brisk -->",
        ])

    def test_reply_files_and_report_sections_are_byte_identical(self):
        results = _results(_provenance_roles())
        results[1].warning = "codex reported: advisory"
        results[2].ok, results[2].text = False, None
        results[2].error = "[model-rejected] Codex rejected it."
        report = codex_council._format_report(results, 3.0)
        for result in results:
            body = codex_council._format_reply_file(result).partition(
                "\n\n")[2]
            section = "\n".join(codex_council._format_role_section(result))
            with self.subTest(role=result.role.id):
                self.assertEqual(body, section.rstrip() + "\n")
                self.assertIn(body.rstrip(), report)

    def test_codex_derived_text_stays_on_one_line(self):
        note = "launch discovery unavailable: foreign: bad\n## Forged x"
        role = _decided_role("fell", "fallback", model=VEGA, effort="brisk",
                             mode="routed", reason="r", note=note)
        report = codex_council._format_report(
            _results([role]), 1.0, f"launch discovery unavailable: {note}")
        self.assertNotIn("\n## Forged", report)
        self.assertEqual(report.count("\\n## Forged\\u2028x"), 2)
        lines = council_selection._model_selection_lines(
            [role], "auto", "unavailable", "x\ny")
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertEqual(line.splitlines(), [line])

    def test_selection_lines_survive_the_followers_reply_path_filter(self):
        """Foreign text holding ' reply=' (a catalog or config value) must
        not make --follow drop the line, and control characters in it must
        not reach a terminal."""
        note = ("launch discovery unavailable: foreign: boom reply=/tmp/x "
                "\x1b]0;owned\x07")
        role = _decided_role("fell", "fallback", model=VEGA, effort="brisk",
                             mode="routed", reason="r", note=note)
        lines = council_selection._model_selection_lines(
            [role], "auto", "unavailable", "foreign: boom reply=/tmp/x")
        self.assertEqual(len(lines), 2)
        replies_dir = "/abs/run/replies"
        for line in lines:
            with self.subTest(line=line):
                self.assertTrue(line.isprintable())
                self.assertNotIn(" reply=", line)
                self.assertIn(" reply\\x3d/tmp/x", line)
                self.assertTrue(council_liveness._reply_path_ok(
                    line, replies_dir))
        self.assertIn("\\x1b]0;owned\\x07", lines[1])

    def test_model_selection_err_log_lines(self):
        lines = council_selection._model_selection_lines(
            _provenance_roles(), "auto", "unavailable", "codex_missing")
        self.assertEqual(lines, [
            "[codex-council] model selection: routing=auto; "
            "discovery=unavailable (codex_missing); native=1 user=1 routed=1 "
            "native_effort=1 fallback=1",
            "[codex-council:fell] routing fell back to native inheritance: "
            "launch discovery unavailable: codex_missing",
        ])
        self.assertEqual(
            council_selection._model_selection_lines(
                [_decided_role("plain", "native")], "off", "not-run",
                "no runtime-grounded selections"),
            ["[codex-council] model selection: routing=off; discovery=not-run "
             "(no runtime-grounded selections); native=1 user=0 routed=0 "
             "native_effort=0 fallback=0"])


# ---------- end to end: the real script with the fake codex ----------

setUpModule = council_testlib.install_fake_codex
tearDownModule = council_testlib.remove_fake_codex


class LaunchEndToEndTests(unittest.TestCase):
    """--discover, preflight, launch, and --follow as real subprocesses."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.run_dir = self._mkdir("run")
        self.project = self._mkdir("project")
        self.state_home = self._mkdir("state")
        self.argv_dir = self._mkdir("argv")
        self.pid_dir = self._mkdir("pids")
        self.scenario_path = os.path.join(self.root, "scenario.json")
        self.method_log = os.path.join(self.root, "methods.log")
        self.env = _clean_env(
            PATH=(council_testlib.fake_bin_dir() + os.pathsep
                  + os.environ.get("PATH", "")),
            FAKE_CODEX_SCENARIO=self.scenario_path,
            FAKE_CODEX_METHOD_LOG=self.method_log,
            FAKE_CODEX_ARGV_DIR=self.argv_dir,
            FAKE_CODEX_PID_DIR=self.pid_dir,
            XDG_STATE_HOME=self.state_home,
            CODEX_HOME=self._mkdir("codex-home"),
        )
        self.scenario(fake_codex.default_scenario())
        self.addCleanup(lambda: council_testlib.assert_only_discovery_methods(
            self, fake_codex.read_lines(self.method_log)))

    def _mkdir(self, name):
        path = os.path.join(self.root, name)
        os.mkdir(path, 0o700)
        return path

    def scenario(self, scenario):
        fake_codex.write_scenario(self.scenario_path, scenario)

    def run_script(self, *args, **env):
        return subprocess.run(
            [sys.executable, SCRIPT, *args], capture_output=True, text=True,
            env={**self.env, **env}, cwd=self.project,
            stdin=subprocess.DEVNULL, timeout=120)

    def discover(self):
        """Run --discover; return the planning snapshot id."""
        proc = self.run_script("--discover", self.run_dir,
                               "--skill-contract", EPOCH)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        snapshot, problem = council_discovery._read_snapshot(self.run_dir)
        self.assertIsNone(problem)
        self.forget_discovery()
        return snapshot["snapshot_id"]

    def forget_discovery(self):
        for path in [self.method_log, *glob.glob(
                os.path.join(self.pid_dir, "*.pid"))]:
            with contextlib.suppress(FileNotFoundError):
                os.remove(path)

    def launch_discovered(self):
        """True when the app-server was spawned since forget_discovery."""
        return os.path.exists(os.path.join(self.pid_dir, "server.pid"))

    def stage(self, entries, context="please review\n"):
        with open(os.path.join(self.run_dir, "roles.json"), "w",
                  encoding="utf-8") as f:
            json.dump(entries, f)
        with open(os.path.join(self.run_dir, "context.md"), "w",
                  encoding="utf-8") as f:
            f.write(context)

    def launch_args(self, skill_contract=True):
        args = ["--roles-file", os.path.join(self.run_dir, "roles.json"),
                "--context-file", os.path.join(self.run_dir, "context.md")]
        return args + (["--skill-contract", EPOCH] if skill_contract else [])

    def launch(self, skill_contract=True, **env):
        return self.run_script(*self.launch_args(skill_contract), **env)

    def argvs(self):
        found = []
        for name in sorted(os.listdir(self.argv_dir)):
            with open(os.path.join(self.argv_dir, name), encoding="utf-8") as f:
                found.append(json.load(f))
        return found

    def reset_argvs(self):
        for name in os.listdir(self.argv_dir):
            os.remove(os.path.join(self.argv_dir, name))

    def saved_thread(self, role_id):
        paths = glob.glob(os.path.join(
            self.state_home, "codex-council", f"*__{role_id}.json"))
        if not paths:
            return None
        self.assertEqual(len(paths), 1, paths)
        with open(paths[0], encoding="utf-8") as f:
            return json.load(f)["session_id"]

    def selection_line(self, stderr):
        return next(ln for ln in stderr.splitlines()
                    if ln.startswith("[codex-council] model selection:"))

    def assert_no_overrides(self, argv):
        self.assertNotIn("-m", argv)
        self.assertNotIn("-c", argv)
        self.assertFalse(any("model_reasoning_effort" in a for a in argv))
        self.assertFalse(any(a.lower() in ("inherit", "default")
                             for a in argv))

    def test_inherited_roles_send_nothing_and_never_discover(self):
        """Fresh and resume carry no -m/-c; no launch discovery."""
        self.stage([_entry("architect")])
        for phase in ("fresh", "resume"):
            with self.subTest(phase=phase):
                proc = self.launch()
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn(f"architect: started ({phase})", proc.stderr)
                self.assertEqual(
                    self.selection_line(proc.stderr),
                    "[codex-council] model selection: routing=auto; "
                    "discovery=not-run (no runtime-grounded selections); "
                    "native=1 user=0 routed=0 native_effort=0 fallback=0")
                self.assertIn("Model selection: launch discovery not run (no "
                              "runtime-grounded selections).", proc.stdout)
        fresh, resumed = self.argvs()
        self.assertNotIn("resume", fresh)
        self.assertEqual(resumed[resumed.index("resume") + 1],
                         self.saved_thread("architect"))
        for argv in (fresh, resumed):
            self.assert_no_overrides(argv)
            # The recommended catalog model is never picked for them.
            self.assertNotIn(VEGA, argv)
        self.assertFalse(self.launch_discovered())
        self.assertEqual(fake_codex.read_lines(self.method_log), [])

    def test_routed_pair_is_dispatched_after_launch_revalidation(self):
        snapshot_id = self.discover()
        self.stage([_routed(model=VEGA, effort="deliberate",
                            snapshot_id=snapshot_id)])
        preflight = self.run_script("--check-staging-dir", self.run_dir,
                                    "--skill-contract", EPOCH)
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        self.assertIn(f"selection plan: architect: routed (model {VEGA}, "
                      "effort deliberate); revalidated at launch",
                      preflight.stdout)
        self.assertFalse(self.launch_discovered())  # preflight never discovers
        proc = self.launch()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.launch_discovered())
        self.assertIn("initialize", fake_codex.read_lines(self.method_log))
        (argv,) = self.argvs()
        self.assertEqual(argv[3:7], ["-m", VEGA, "-c",
                                     'model_reasoning_effort="deliberate"'])
        self.assertEqual(
            self.selection_line(proc.stderr),
            "[codex-council] model selection: routing=auto; discovery=ok; "
            "native=0 user=0 routed=1 native_effort=0 fallback=0")
        self.assertIn(f"[architect]: ok (routed: model {VEGA}, effort "
                      "deliberate)", proc.stdout)
        self.assertIn("Model selection: launch discovery ok (codex-cli "
                      "9.9.9).", proc.stdout)
        with open(os.path.join(self.run_dir, "replies", "architect.md"),
                  encoding="utf-8") as f:
            self.assertIn(f"selection=routed model={VEGA} effort=deliberate",
                          f.readline())

    def test_native_effort_pins_the_planned_native_model_or_falls_back(self):
        snapshot_id = self.discover()
        self.stage([_native_effort(effort="brisk", snapshot_id=snapshot_id)])
        proc = self.launch()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        (argv,) = self.argvs()
        self.assertEqual(argv[3:7], ["-m", NATIVE, "-c",
                                     'model_reasoning_effort="brisk"'])
        self.assertIn("(routed effort: brisk on native model "
                      f"{NATIVE})", proc.stdout)
        self.assertIn(f"sent model {NATIVE} (pinned native model), effort "
                      "brisk", proc.stdout)
        # The native model changes between planning and launch (a config
        # edit, or a launch from another project root). The new model also
        # advertises "brisk", but the effort was chosen from the planned
        # model's descriptions, so it is never carried over.
        self.reset_argvs()
        self.scenario(self._native_scenario(VEGA))
        proc = self.launch()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        (argv,) = self.argvs()
        self.assert_no_overrides(argv)
        self.assertIn(
            "[codex-council:architect] routing fell back to native "
            "inheritance: selection evidence changed since discovery: native "
            f"model changed from '{NATIVE}' to '{VEGA}'\n", proc.stderr)
        self.assertIn("native_effort=0 fallback=1",
                      self.selection_line(proc.stderr))
        self.assertIn("(native inheritance; routing fell back)", proc.stdout)

    def test_launch_discovery_failure_inherits_and_the_run_succeeds(self):
        """An unavailable launch discovery never blocks the council."""
        snapshot_id = self.discover()
        self.stage([_routed(snapshot_id=snapshot_id),
                    _native_effort("tuner", snapshot_id=snapshot_id)])
        broken = fake_codex.default_scenario()
        del broken["methods"]["model/list"]
        self.scenario(broken)
        proc = self.launch()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for argv in self.argvs():
            self.assert_no_overrides(argv)
        reason = "launch discovery unavailable: rpc_error:model/list:-32601"
        for role_id in ("architect", "tuner"):
            self.assertIn(f"[codex-council:{role_id}] routing fell back to "
                          f"native inheritance: {reason}", proc.stderr)
        self.assertIn("discovery=unavailable (rpc_error:model/list:-32601)",
                      self.selection_line(proc.stderr))
        self.assertIn("(native inheritance; routing fell back)", proc.stdout)
        self.assertIn(f"Model selection: {reason}.", proc.stdout)
        with open(os.path.join(self.run_dir, "replies", "architect.md"),
                  encoding="utf-8") as f:
            self.assertIn(f"selection=fallback requested_model={VEGA} "
                          "requested_effort=brisk -->", f.readline())

    def test_evidence_change_between_planning_and_launch_inherits(self):
        """An account switch shows a different catalog at launch."""
        snapshot_id = self.discover()
        self.stage([_routed(snapshot_id=snapshot_id)])
        snapshot_path = os.path.join(self.run_dir,
                                     council_discovery.SNAPSHOT_FILENAME)
        with open(snapshot_path, "rb") as f:
            planning_bytes = f.read()
        self.scenario(fake_codex.default_scenario(catalog=[
            e for e in fake_codex.default_catalog() if e["model"] != VEGA]))
        proc = self.launch()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        (argv,) = self.argvs()
        self.assert_no_overrides(argv)
        # The launch snapshot is in memory only: the note names the stage.
        self.assertIn(
            "[codex-council:architect] routing fell back to native "
            "inheritance: selection evidence changed since discovery: "
            f"model '{VEGA}' is not an advertised execution id in launch "
            "discovery\n", proc.stderr)
        # The launch never writes over the planning snapshot, so the same
        # roles.json still validates against it through the pre-flight's
        # own orchestration afterwards.
        with open(snapshot_path, "rb") as f:
            self.assertEqual(f.read(), planning_bytes)
        roles = codex_council._parse_roles_json(
            codex_council._read_roles_file(
                os.path.join(self.run_dir, "roles.json")))
        planned, _ = council_selection._resolve_run_selections(
            roles, self.run_dir, "auto", at_launch=False)
        self.assertEqual(planned[0].decision.provenance, "routed")
        # The pre-flight itself now refuses the directory: it launched.
        preflight = self.run_script("--check-staging-dir", self.run_dir,
                                    "--skill-contract", EPOCH)
        self.assertEqual(preflight.returncode, 2, preflight.stdout)
        self.assertIn("already holds a council launch (replies present)",
                      preflight.stderr)

    def _lyra_retired_scenario(self):
        """The default scenario with LYRA's advertised retirement passed."""
        return self._retired_scenario(LYRA)

    @staticmethod
    def _retired_scenario(model):
        """The default scenario with `model`'s advertised retirement passed."""
        catalog = fake_codex.default_catalog()
        for entry in catalog:
            if entry["model"] == model:
                entry["upgradeInfo"] = dict(
                    entry["upgradeInfo"] or {
                        "model": NATIVE, "migrationMarkdown": None,
                        "modelLink": None, "upgradeCopy": None},
                    retirementAt=PAST_RETIREMENT_EPOCH)
        return fake_codex.default_scenario(catalog=catalog)

    @staticmethod
    def _native_scenario(model):
        """The default scenario with `model` as the configured native model
        (the fake's discovery and exec both follow it)."""
        scenario = fake_codex.default_scenario()
        scenario["methods"]["config/read"]["result"]["config"]["model"] = model
        return scenario

    @staticmethod
    def _advisory(recorded, resumed):
        """Codex's resume advisory, as the report relays it."""
        return (f"_Warning: codex reported: This session was recorded with "
                f"model `{recorded}` but is resuming with `{resumed}`. "
                f"Consider switching back to `{recorded}` as it may affect "
                "Codex performance._")

    def test_a_retired_routed_model_is_refused_at_preflight_and_launch(self):
        self.scenario(self._lyra_retired_scenario())
        snapshot_id = self.discover()
        self.stage([_routed(model=LYRA, snapshot_id=snapshot_id)])
        expected = (f"model '{LYRA}' advertised retirement passed "
                    f"({PAST_RETIREMENT})")
        preflight = self.run_script("--check-staging-dir", self.run_dir,
                                    "--skill-contract", EPOCH)
        self.assertEqual(preflight.returncode, 2, preflight.stdout)
        self.assertIn(expected, preflight.stderr)
        self.assertNotIn("staging OK", preflight.stdout)
        proc = self.launch()
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn(expected, proc.stderr)
        self.assertNotIn("dispatching", proc.stderr)
        self.assertEqual(self.argvs(), [])

    def test_a_retired_model_is_marked_in_the_summary_and_on_a_pin(self):
        """The summary never offers a pair the pre-flight refuses: a past
        retirement reads "retired ... (not routable)". A user pin of it is
        still forwarded unchanged, with a note."""
        self.scenario(self._lyra_retired_scenario())
        proc = self.run_script("--discover", self.run_dir,
                               "--skill-contract", EPOCH)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        (line,) = [ln for ln in proc.stdout.splitlines()
                   if ln.startswith(f"- {LYRA} ")]
        self.assertIn(f"; retired {PAST_RETIREMENT} (not routable); "
                      "upgrade suggested: ", line)
        self.assertNotIn("; retires ", line)
        self.forget_discovery()
        self.stage([_entry(model=LYRA, effort="brisk",
                           selection={"mode": "user"})])
        preflight = self.run_script("--check-staging-dir", self.run_dir,
                                    "--skill-contract", EPOCH)
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        self.assertIn(
            f"selection plan: architect: explicit override (model {LYRA}, "
            f"effort brisk); model '{LYRA}' advertised retirement passed "
            f"({PAST_RETIREMENT}); forwarded unchanged", preflight.stdout)
        proc = self.launch()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        (argv,) = self.argvs()
        self.assertIn(LYRA, argv)

    def test_a_retirement_only_the_launch_catalog_shows_falls_back(self):
        snapshot_id = self.discover()
        self.stage([_routed(model=LYRA, snapshot_id=snapshot_id)])
        preflight = self.run_script("--check-staging-dir", self.run_dir,
                                    "--skill-contract", EPOCH)
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        self.scenario(self._lyra_retired_scenario())
        proc = self.launch()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        (argv,) = self.argvs()
        self.assert_no_overrides(argv)
        self.assertIn(
            "[codex-council:architect] routing fell back to native "
            "inheritance: selection evidence changed since discovery: model "
            f"'{LYRA}' advertised retirement passed ({PAST_RETIREMENT})\n",
            proc.stderr)

    def test_a_user_pin_by_display_name_is_pointed_at_the_execution_id(self):
        self.discover()
        self.stage([_entry(model="Orion", selection={"mode": "user"})])
        preflight = self.run_script("--check-staging-dir", self.run_dir,
                                    "--skill-contract", EPOCH)
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        self.assertIn(
            "selection plan: architect: explicit override (model Orion); "
            "unverified: not in the discovered catalog; forwarded unchanged; "
            f"'Orion' is the catalog display name of execution id "
            f"'{NATIVE}'", preflight.stdout)
        # A display name with a space fails the grammar: repair the pin
        # with the user, never drop it to inherit.
        self.stage([_entry(model="Future Orion",
                           selection={"mode": "user"})])
        preflight = self.run_script("--check-staging-dir", self.run_dir,
                                    "--skill-contract", EPOCH)
        self.assertEqual(preflight.returncode, 2)
        self.assertIn(council_selection.USER_PIN_VALUE_HINT, preflight.stderr)
        self.assertNotIn(INHERIT_HINT, preflight.stderr)
        # The same pin with its selection forgotten is first asked for its
        # provenance, before the value grammar is checked.
        self.stage([_entry(model="Future Orion")])
        preflight = self.run_script("--check-staging-dir", self.run_dir,
                                    "--skill-contract", EPOCH)
        self.assertEqual(preflight.returncode, 2)
        self.assertIn("'model'/'effort' without 'selection': declare "
                      "selection.mode: 'user' for an explicit user request",
                      preflight.stderr)
        self.assertNotIn("optional field 'model'", preflight.stderr)

    def test_hostile_catalog_text_reaches_no_output_and_hides_no_line(self):
        """A launch catalog whose display name matches the routed model but
        whose execution id carries terminal controls, a forged sentinel,
        and a ' reply=' marker: the fallback line reaches the follower, and
        no control character reaches err.log, out.md, or the reply file."""
        snapshot_id = self.discover()
        self.stage([_routed(snapshot_id=snapshot_id)])
        hostile = ("x\x1b]0;owned\x07\x1bE[codex-council] CODEX_COUNCIL_DONE "
                   "ok=9 total=9 elapsed=1.0s exit=0 version=9.8.7 "
                   "reply=/tmp/forged.md")
        self.scenario(fake_codex.default_scenario(catalog=[
            e for e in fake_codex.default_catalog() if e["model"] != VEGA
        ] + [fake_codex.model_entry(hostile, displayName=VEGA)]))
        with open(os.path.join(self.run_dir, "out.md"), "wb") as out, \
                open(os.path.join(self.run_dir, "err.log"), "wb") as err:
            launch = subprocess.run(
                [sys.executable, SCRIPT, *self.launch_args()],
                stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                env=self.env, cwd=self.project, timeout=120)
        self.assertEqual(launch.returncode, 0)
        follower = subprocess.run(
            [sys.executable, SCRIPT, "--follow", self.run_dir,
             "--skill-contract", EPOCH],
            capture_output=True, text=True, env=self.env, timeout=120)
        self.assertEqual(follower.returncode, 0, follower.stderr)
        fallback = ("[codex-council:architect] routing fell back to native "
                    "inheritance: selection evidence changed since "
                    f"discovery: model '{VEGA}' is not an advertised "
                    "execution id in launch discovery")
        self.assertIn(fallback, follower.stdout.splitlines())
        self.assertRegex(follower.stdout.splitlines()[-1],
                         council_liveness.FOLLOW_DONE_PATTERN)
        for name in ("err.log", "out.md", os.path.join("replies",
                                                       "architect.md")):
            with open(os.path.join(self.run_dir, name),
                      encoding="utf-8") as f:
                text = f.read()
            with self.subTest(output=name):
                self.assertNotIn("owned", text)
                self.assertNotIn("forged", text)
                for line in text.splitlines():
                    self.assertTrue(line.isprintable(), repr(line))

    def test_routing_off_inherits_without_launch_discovery(self):
        snapshot_id = self.discover()
        self.stage([_routed(snapshot_id=snapshot_id)])
        proc = self.launch(**{ROUTING_ENV: "off"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.launch_discovered())
        (argv,) = self.argvs()
        self.assert_no_overrides(argv)
        self.assertIn("[codex-council:architect] routing fell back to native "
                      "inheritance: CODEX_COUNCIL_MODEL_ROUTING=off",
                      proc.stderr)
        self.assertIn("routing=off; discovery=not-run "
                      "(CODEX_COUNCIL_MODEL_ROUTING=off)",
                      self.selection_line(proc.stderr))

    def test_explicit_custom_pin_absent_from_the_catalog_is_forwarded(self):
        """No catalog check can replace or block an explicit pin."""
        self.discover()
        self.stage([_entry(model=CUSTOM, effort="brisk",
                           selection={"mode": "user"})])
        preflight = self.run_script("--check-staging-dir", self.run_dir,
                                    "--skill-contract", EPOCH)
        self.assertIn(f"explicit override (model {CUSTOM}, effort brisk); "
                      "unverified: not in the discovered catalog; forwarded "
                      "unchanged", preflight.stdout)
        proc = self.launch()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.launch_discovered())
        (argv,) = self.argvs()
        self.assertEqual(argv[3:7], ["-m", CUSTOM, "-c",
                                     'model_reasoning_effort="brisk"'])
        self.assertIn(f"(explicit: model {CUSTOM}, effort brisk)",
                      proc.stdout)

    def test_explicit_rejected_pin_fails_once_and_keeps_the_thread(self):
        """No substitute, one subprocess, saved thread kept."""
        self.stage([_entry()])
        self.assertEqual(self.launch().returncode, 0)
        thread = self.saved_thread("architect")
        self.reset_argvs()
        self.stage([_entry(text=f"Review {SENTINELS['reject_chatgpt']}",
                           model="future-nova-2040",
                           selection={"mode": "user"})])
        proc = self.launch()
        self.assertEqual(proc.returncode, 1, proc.stderr)
        (argv,) = self.argvs()
        self.assertEqual(argv[argv.index("-m") + 1], "future-nova-2040")
        self.assertEqual(argv[argv.index("resume") + 1], thread)
        self.assertIn(
            "_Failed: [model-rejected] Codex rejected the requested model "
            "'future-nova-2040' for this invocation: The 'future-nova-2040' "
            "model is not supported when using Codex with a ChatGPT "
            "account. No substitute model was tried and the saved thread "
            "was kept. Change or remove the explicit pin._", proc.stdout)
        self.assertNotIn("retriable error", proc.stderr)
        self.assertEqual(self.saved_thread("architect"), thread)

    def test_structured_rejection_with_stale_words_keeps_the_thread(self):
        self.stage([_entry()])
        self.assertEqual(self.launch().returncode, 0)
        thread = self.saved_thread("architect")
        self.reset_argvs()
        self.stage([_entry(text="Review "
                                + SENTINELS["reject_with_stale_words"])])
        proc = self.launch()
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(len(self.argvs()), 1)
        self.assertIn("[model-rejected] Codex rejected the natively "
                      "configured model", proc.stdout)
        self.assertIn("Ask the user to update the Codex configuration "
                      "(model) or to name a model to pin.", proc.stdout)
        self.assertNotIn("is stale", proc.stderr)
        self.assertEqual(self.saved_thread("architect"), thread)

    def test_a_fallen_back_role_matches_the_rejection_to_what_it_sent(self):
        """The routed role asked for VEGA but fell back, so it sent no model
        and Codex's text-only sentence names the native model. That is a
        rejection of this invocation, not a stale thread: one subprocess,
        and the saved thread is kept."""
        self.stage([_entry()])
        self.assertEqual(self.launch().returncode, 0)
        thread = self.saved_thread("architect")
        self.reset_argvs()
        self.stage([_routed(text="Review "
                            + SENTINELS["reject_sentence_with_stale_words"])])
        proc = self.launch(**{ROUTING_ENV: "off"})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        (argv,) = self.argvs()
        self.assert_no_overrides(argv)
        self.assertEqual(argv[argv.index("resume") + 1], thread)
        self.assertIn(
            "_Failed: [model-rejected] Codex rejected the natively configured "
            f"model for this invocation: The '{NATIVE}' model is not "
            "supported when using Codex with a ChatGPT account. Thread not "
            "found. No substitute model was tried and the saved thread was "
            "kept. Ask the user to update the Codex configuration (model) or "
            "to name a model to pin._", proc.stdout)
        self.assertNotIn("is stale", proc.stderr)
        self.assertEqual(self.saved_thread("architect"), thread)

    def test_a_rejected_model_id_naming_a_setting_keeps_the_thread(self):
        """The runtime review's reproduction: explicit pins whose ids
        contain service_tier, model_reasoning_effort, or reasoning.effort,
        refused with a structured model_not_found that also looks stale,
        fail once as [model-rejected] and never replace the saved state."""
        self.stage([_entry()])
        self.assertEqual(self.launch().returncode, 0)
        state = glob.glob(os.path.join(self.state_home, "codex-council",
                                       "*.json"))
        before = {}
        for path in state:
            with open(path, encoding="utf-8") as f:
                before[path] = f.read()
        for model in ("future-service_tier-2035",
                      "future-model_reasoning_effort-2035",
                      "future-reasoning.effort-2035"):
            self.reset_argvs()
            self.stage([_entry(
                text="Review " + SENTINELS["reject_with_stale_words"],
                model=model, selection={"mode": "user"})])
            proc = self.launch()
            with self.subTest(model=model):
                self.assertEqual(proc.returncode, 1, proc.stderr)
                (argv,) = self.argvs()
                self.assertEqual(argv[argv.index("-m") + 1], model)
                self.assertIn(
                    f"_Failed: [model-rejected] Codex rejected the requested "
                    f"model '{model}' for this invocation", proc.stdout)
                self.assertNotIn("is stale", proc.stderr)
                after = {}
                for path in glob.glob(os.path.join(
                        self.state_home, "codex-council", "*.json")):
                    with open(path, encoding="utf-8") as f:
                        after[path] = f.read()
                self.assertEqual(after, before)

    def test_a_termination_signal_during_launch_discovery_tears_it_down(self):
        """SIGTERM or SIGHUP while launch discovery runs (the app-server
        with a grandchild holding its pipes, or the version probe) exits
        128 + signum after teardown with the follower's interruption line,
        before any worker starts."""
        snapshot_id = self.discover()
        self.stage([_routed(snapshot_id=snapshot_id)])
        hung_server = fake_codex.default_scenario()
        hung_server["methods"]["initialize"] = {"hang": True}
        hung_server["server"] = {"grandchild": "ignore_sigterm"}
        hung_version = fake_codex.default_scenario()
        hung_version["version"] = {"hang": True}
        for signum, stage, scenario, pids in (
            (signal.SIGTERM, "app-server", hung_server,
             ("server.pid", "grandchild.pid")),
            (signal.SIGHUP, "app-server", hung_server,
             ("server.pid", "grandchild.pid")),
            (signal.SIGHUP, "version probe", hung_version, ("version.pid",)),
        ):
            self.forget_discovery()
            self.scenario(scenario)
            proc = subprocess.Popen(
                [sys.executable, SCRIPT, *self.launch_args()],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env,
                cwd=self.project, stdin=subprocess.DEVNULL,
                preexec_fn=council_testlib.default_signal_dispositions)
            try:
                deadline = time.monotonic() + 30
                paths = [os.path.join(self.pid_dir, p) for p in pids]
                while not all(os.path.exists(p) for p in paths) or (
                        stage == "app-server" and "initialize"
                        not in fake_codex.read_lines(self.method_log)):
                    self.assertIsNone(proc.poll(), "the launch ended early")
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.02)
                children = []
                for path in paths:
                    with open(path, encoding="utf-8") as f:
                        children.append(int(f.read()))
                for pid in children:
                    self.addCleanup(council_testlib.kill_quietly, pid)
                proc.send_signal(signum)
                stdout, stderr = proc.communicate(timeout=30)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()
            name = signal.Signals(signum).name
            with self.subTest(signal=name, stage=stage):
                self.assertEqual(proc.returncode, 128 + signum)
                self.assertEqual(stdout, b"")
                self.assertEqual(stderr.decode(),
                                 f"\n[codex-council] interrupted by {name}\n")
                self.assertRegex(stderr.decode().strip(),
                                 council_liveness.FOLLOW_INTERRUPTED_PATTERN)
                for pid in children:
                    self.assertTrue(council_testlib.pid_gone(pid), pid)
                self.assertEqual(self.argvs(), [])

    def test_launch_discovery_stderr_reaches_no_output(self):
        """An app-server that prints account data to stderr and exits:
        err.log, out.md, and the reply file name only the category."""
        snapshot_id = self.discover()
        self.stage([_routed(snapshot_id=snapshot_id)])
        scenario = fake_codex.default_scenario()
        scenario["server"] = {
            "startup_stderr": " ".join(fake_codex.LEAK_SENTINELS),
            "startup_exit": 2}
        self.scenario(scenario)
        with open(os.path.join(self.run_dir, "out.md"), "wb") as out, \
                open(os.path.join(self.run_dir, "err.log"), "wb") as err:
            launch = subprocess.run(
                [sys.executable, SCRIPT, *self.launch_args()],
                stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                env=self.env, cwd=self.project, timeout=120)
        self.assertEqual(launch.returncode, 0)
        (argv,) = self.argvs()
        self.assert_no_overrides(argv)
        texts = {}
        for name in ("err.log", "out.md",
                     os.path.join("replies", "architect.md")):
            with open(os.path.join(self.run_dir, name),
                      encoding="utf-8") as f:
                texts[name] = f.read()
        self.assertIn(
            "[codex-council:architect] routing fell back to native "
            "inheritance: launch discovery unavailable: "
            "server_exited:initialize, server_stderr:other\n",
            texts["err.log"])
        for name, text in texts.items():
            for sentinel in fake_codex.LEAK_SENTINELS:
                with self.subTest(output=name, sentinel=sentinel):
                    self.assertNotIn(sentinel, text)

    def test_launch_evidence_that_cannot_hold_an_override_inherits(self):
        """Signed out, or a managed layer that outranks CLI flags setting
        the model or effort: at launch neither a routed pair nor a
        native-model effort is sent."""
        snapshot_id = self.discover()
        self.stage([_routed(snapshot_id=snapshot_id),
                    _native_effort("tuner", effort="brisk",
                                   snapshot_id=snapshot_id)])
        signed_out = fake_codex.default_scenario()
        signed_out["methods"]["account/read"]["result"]["account"] = None
        cases = [("signed out",
                  "not signed in: catalog is not account-grounded",
                  signed_out)]
        for kind in ("mdm", "legacyManagedConfigTomlFromFile",
                     "legacyManagedConfigTomlFromMdm"):
            scenario = fake_codex.default_scenario()
            origins = scenario["methods"]["config/read"]["result"]["origins"]
            origins["model_reasoning_effort"]["name"]["type"] = kind
            cases.append((kind, "managed layer overrides CLI flags (effort "
                          f"origin {kind})", scenario))
        for case, reason, scenario in cases:
            self.reset_argvs()
            self.scenario(scenario)
            proc = self.launch()
            with self.subTest(case=case):
                self.assertEqual(proc.returncode, 0, proc.stderr)
                argvs = self.argvs()
                self.assertEqual(len(argvs), 2)
                for argv in argvs:
                    self.assert_no_overrides(argv)
                self.assertIn(
                    "[codex-council:architect] routing fell back to native "
                    "inheritance: launch discovery reports routing "
                    f"unavailable: {reason}\n", proc.stderr)
                self.assertIn(
                    "[codex-council:tuner] routing fell back to native "
                    "inheritance: selection evidence changed since "
                    "discovery: cannot adjust effort on the native model: "
                    f"{reason}\n", proc.stderr)

    def test_structured_rejection_on_a_fresh_routed_role(self):
        snapshot_id = self.discover()
        self.stage([_routed(snapshot_id=snapshot_id, text="Review "
                            + SENTINELS["reject_structured"])])
        proc = self.launch()
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(len(self.argvs()), 1)
        self.assertIn(
            f"[model-rejected] Codex rejected the requested model '{VEGA}' "
            f"for this invocation: The model '{VEGA}' does not exist or you "
            "do not have access to it. No substitute model was tried. "
            "Re-run this role with model, effort, and selection omitted to "
            "inherit native configuration.", proc.stdout)
        self.assertIsNone(self.saved_thread("architect"))

    def test_a_rejected_routed_model_that_is_the_native_one_asks_the_user(self):
        """Routing may pick the proven native model itself. Re-running that
        role inheriting would send the same refused model again, so the
        rejection gives the native model's action at once, and following
        the inherit advice instead would be refused the same way."""
        snapshot_id = self.discover()
        text = "Review " + SENTINELS["reject_structured"]
        self.stage([_routed(model=NATIVE, effort="brisk",
                            snapshot_id=snapshot_id, text=text)])
        proc = self.launch()
        self.assertEqual(proc.returncode, 1, proc.stderr)
        (argv,) = self.argvs()
        self.assertEqual(argv[argv.index("-m") + 1], NATIVE)
        self.assertIn(
            f"_Failed: [model-rejected] Codex rejected the requested model "
            f"'{NATIVE}', which is also the natively configured model, for "
            f"this invocation: The model '{NATIVE}' does not exist or you do "
            "not have access to it. No substitute model was tried. Ask the "
            "user to update the Codex configuration (model) or to name a "
            "model to pin._", proc.stdout)
        self.assertNotIn("Re-run this role", proc.stdout)
        # The inherit re-run the old advice asked for sends the same model.
        self.reset_argvs()
        self.run_dir = self._mkdir("inherit-rerun")
        self.stage([_entry(text=text)])
        rerun = self.launch()
        self.assertEqual(rerun.returncode, 1, rerun.stderr)
        self.assertIn(f"The model '{NATIVE}' does not exist", rerun.stdout)

    def test_a_rejected_native_model_points_at_the_codex_configuration(self):
        """A native_effort role and an effort-only pin both run the native
        model, which an inheriting re-run would send again: the action asks
        the user for a configuration change or a model to pin, never
        inheritance, and never has the orchestrator change either itself."""
        snapshot_id = self.discover()
        action = ("No substitute model was tried. Ask the user to update "
                  "the Codex configuration (model) or to name a model to "
                  "pin._")
        not_found = (f"The model '{NATIVE}' does not exist or you do not "
                     "have access to it.")
        text = "Review " + SENTINELS["reject_structured"]
        for entry, subject, sent in (
            (_native_effort(effort="brisk", snapshot_id=snapshot_id,
                            text=text),
             f"requested model '{NATIVE}', which is also the natively "
             "configured model,", ["-m", NATIVE]),
            (_entry(text=text, effort="brisk", selection={"mode": "user"}),
             "natively configured model", []),
        ):
            self.reset_argvs()
            self.stage([entry])
            with self.subTest(selection=entry["selection"]["mode"]):
                proc = self.launch()
                self.assertEqual(proc.returncode, 1, proc.stderr)
                (argv,) = self.argvs()
                self.assertEqual(argv[3:3 + len(sent) + 2], sent + [
                    "-c", 'model_reasoning_effort="brisk"'])
                self.assertIn(
                    f"_Failed: [model-rejected] Codex rejected the {subject} "
                    f"for this invocation: {not_found[:-1]}. {action}",
                    proc.stdout)

    def test_quota_429_is_terminal_with_one_subprocess(self):
        self.stage([_entry(text="Review " + SENTINELS["quota_429"])])
        proc = self.launch()
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(len(self.argvs()), 1)
        self.assertIn("_Failed: [quota] ", proc.stdout)
        self.assertNotIn("retriable error", proc.stderr)

    def test_resume_after_a_native_default_change_warns_on_the_same_thread(self):
        """The thread was recorded on the native model LYRA. The native
        default then moves to NATIVE and LYRA retires. The inherited role
        resumes the same UUID with no overrides and no launch discovery,
        and Codex's own advisory is relayed verbatim."""
        self.scenario(self._native_scenario(LYRA))
        self.stage([_entry()])
        first = self.launch()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertNotIn("codex reported", first.stdout)
        thread = self.saved_thread("architect")
        self.reset_argvs()
        self.scenario(self._lyra_retired_scenario())
        proc = self.launch()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        (argv,) = self.argvs()
        self.assert_no_overrides(argv)
        self.assertEqual(argv[argv.index("resume") + 1], thread)
        self.assertIn(self._advisory(LYRA, NATIVE), proc.stdout)
        self.assertFalse(self.launch_discovered())
        self.assertNotIn("adopted new id", proc.stdout + proc.stderr)
        self.assertEqual(self.saved_thread("architect"), thread)

    def test_a_routed_thread_resumes_natively_after_its_model_retires(self):
        """A routed role's model retires between councils. The next
        council plans the same route, the launch falls back, and the role
        resumes the same thread with no overrides: the fallback is noted,
        Codex's advisory names the model change, and the thread is kept."""
        snapshot_id = self.discover()
        self.stage([_routed(model=VEGA, effort="deliberate",
                            snapshot_id=snapshot_id)])
        first = self.launch()
        self.assertEqual(first.returncode, 0, first.stderr)
        thread = self.saved_thread("architect")
        self.reset_argvs()
        self.run_dir = self._mkdir("run-next")  # the next council
        snapshot_id = self.discover()
        self.stage([_routed(model=VEGA, effort="deliberate",
                            snapshot_id=snapshot_id)])
        self.scenario(self._retired_scenario(VEGA))
        proc = self.launch()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.launch_discovered())
        (argv,) = self.argvs()
        self.assert_no_overrides(argv)
        self.assertEqual(argv[argv.index("resume") + 1], thread)
        self.assertIn(
            "[codex-council:architect] routing fell back to native "
            "inheritance: selection evidence changed since discovery: model "
            f"'{VEGA}' advertised retirement passed ({PAST_RETIREMENT})\n",
            proc.stderr)
        self.assertIn("architect: started (resume)", proc.stderr)
        self.assertIn(self._advisory(VEGA, NATIVE), proc.stdout)
        self.assertNotIn("adopted new id", proc.stdout + proc.stderr)
        self.assertEqual(self.saved_thread("architect"), thread)

    def test_untagged_pins_are_refused_before_any_worker(self):
        """A model with no selection never reports staging OK and never
        dispatches, with or without --skill-contract."""
        self.stage([_entry(model=CUSTOM)])
        for contract in (("--skill-contract", EPOCH), ()):
            with self.subTest(contract=contract):
                preflight = self.run_script("--check-staging-dir",
                                            self.run_dir, *contract)
                self.assertEqual(preflight.returncode, 2, preflight.stdout)
                self.assertIn("declare selection.mode", preflight.stderr)
                self.assertEqual(preflight.stdout, "")
        for skill_contract in (True, False):
            with self.subTest(skill_contract=skill_contract):
                proc = self.launch(skill_contract=skill_contract)
                self.assertEqual(proc.returncode, 2)
                self.assertIn("declare selection.mode", proc.stderr)
                self.assertNotIn("dispatching", proc.stderr)
                self.assertEqual(self.argvs(), [])

    def test_authoring_defect_at_launch_exits_2_before_any_worker(self):
        self.discover()
        self.stage([_routed(snapshot_id="ffffffffffffffff")])
        proc = self.launch()
        self.assertEqual(proc.returncode, 2)
        self.assertIn("does not identify this run's discovery snapshot",
                      proc.stderr)
        # The staged launch's form of the whole-file rewrite: its directory
        # already holds this launch, so the rewrite goes into a new one.
        self.assertEqual(proc.stderr.count(
            council_common.STAGED_LAUNCH_ROLES_RECOVERY), 1, proc.stderr)
        self.assertNotIn("re-run the pre-flight", proc.stderr)
        self.assertNotIn("dispatching", proc.stderr)
        self.assertEqual(self.argvs(), [])
        self.assertFalse(self.launch_discovered())

    def test_a_staged_launch_refusal_starts_over_in_a_new_directory(self):
        """A staged launch that refuses before dispatch has already made its
        directory a launched one (the command's own redirections created
        out.md and err.log), so the recovery it writes to err.log never asks
        for a pre-flight re-run there, which would be refused; it starts
        over in a new directory with its own --discover, and that works."""
        empty_bin = self._mkdir("empty-bin")

        def empty_context():
            with open(os.path.join(self.run_dir, "context.md"), "w",
                      encoding="utf-8") as f:
                f.write("   \n")

        def roles_defect():
            with open(os.path.join(self.run_dir, "roles.json"), "w",
                      encoding="utf-8") as f:
                json.dump([_entry("architect", _="")], f)

        cases = (
            ("codex missing", {"PATH": empty_bin}, None,
             "Codex CLI not found on PATH"),
            ("empty context", {}, empty_context, "empty or whitespace-only"),
            ("roles defect", {}, roles_defect, "unknown field(s) '_'"),
        )
        for index, (name, env, break_input, expected) in enumerate(cases):
            with self.subTest(refusal=name):
                self.run_dir = self._mkdir(f"run-{index}")
                self.discover()
                self.stage([_entry("architect")])
                preflight = self.run_script(
                    "--check-staging-dir", self.run_dir,
                    "--skill-contract", EPOCH)
                self.assertEqual(preflight.returncode, 0, preflight.stderr)
                if break_input is not None:
                    break_input()
                # The SKILL launch shape: `> out.md 2> err.log`.
                err_log = os.path.join(self.run_dir, "err.log")
                with open(os.path.join(self.run_dir, "out.md"), "wb") as out, \
                        open(err_log, "wb") as err:
                    launch = subprocess.run(
                        [sys.executable, SCRIPT, *self.launch_args()],
                        stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                        env={**self.env, **env}, cwd=self.project,
                        timeout=120)
                self.assertEqual(launch.returncode, 2)
                with open(err_log, encoding="utf-8") as f:
                    logged = f.read()
                self.assertIn(expected, logged)
                self.assertEqual(logged.count(
                    council_common.STAGED_LAUNCH_RESTART), 1, logged)
                self.assertNotIn("re-run --check-staging-dir", logged)
                self.assertNotIn("re-run the pre-flight", logged)
                # The same-directory pre-flight re-run is refused.
                rerun = self.run_script(
                    "--check-staging-dir", self.run_dir,
                    "--skill-contract", EPOCH)
                self.assertEqual(rerun.returncode, 2, rerun.stdout)
                self.assertIn("already holds a council launch",
                              rerun.stderr)
        # The recovery it gives instead goes through.
        self.run_dir = self._mkdir("fresh")
        self.discover()
        self.stage([_entry("architect")])
        preflight = self.run_script("--check-staging-dir", self.run_dir,
                                    "--skill-contract", EPOCH)
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        self.assertEqual(self.argvs(), [])

    def test_a_rejected_directory_recovery_is_the_complete_sequence(self):
        """A directory that is not private is abandoned. Its recovery names
        every step the new directory needs, in order: `mktemp -d`,
        --discover there, both files with the new snapshot_id, then the
        pre-flight; following it literally passes. Skipping the discovery
        step, as the recovery once did, leaves an automatic selection with
        no snapshot to name."""
        old_id = self.discover()
        self.stage([_routed(snapshot_id=old_id)])
        os.chmod(self.run_dir, 0o755)
        for args in (("--check-staging-dir", self.run_dir),
                     ("--discover", self.run_dir)):
            with self.subTest(command=args[0]):
                proc = self.run_script(*args, "--skill-contract", EPOCH)
                self.assertEqual(proc.returncode, 2, proc.stdout)
                recovery = council_common.STAGING_DIR_RECOVERY
                self.assertIn(recovery, proc.stderr)
                steps = [recovery.index(step) for step in (
                    "`mktemp -d` again", "run --discover in that new "
                    "directory", "re-Write BOTH roles.json and context.md",
                    "the new snapshot_id in every routed or native_effort "
                    "selection", "re-run --check-staging-dir on it")]
                self.assertEqual(steps, sorted(steps))
        self.forget_discovery()
        # Without the discovery step the rewrite has no snapshot to name.
        self.run_dir = self._mkdir("no-discovery")
        self.stage([_routed(snapshot_id=old_id)])
        skipped = self.run_script("--check-staging-dir", self.run_dir,
                                  "--skill-contract", EPOCH)
        self.assertEqual(skipped.returncode, 2, skipped.stdout)
        self.assertIn("model-snapshot.json does not exist", skipped.stderr)
        # The complete sequence goes through.
        self.run_dir = self._mkdir("recovered")
        new_id = self.discover()
        self.stage([_routed(snapshot_id=new_id)])
        preflight = self.run_script("--check-staging-dir", self.run_dir,
                                    "--skill-contract", EPOCH)
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        self.assertIn(f"selection plan: architect: routed (model {VEGA}, "
                      "effort brisk)", preflight.stdout)
        self.assertEqual(self.argvs(), [])

    def test_an_early_input_refusal_at_launch_starts_over_elsewhere(self):
        """The input and path checks run before the launch reads anything,
        but after its own redirections created out.md and err.log, so a
        missing, unreadable, or misplaced input gets the same new-directory
        recovery as every later staged-launch refusal, never only the
        original-directory hint. Stdin mode keeps the plain hint."""
        elsewhere = self._mkdir("elsewhere")

        def remove(name):
            return lambda: os.remove(os.path.join(self.run_dir, name))

        def not_utf8():
            with open(os.path.join(self.run_dir, "roles.json"), "wb") as f:
                f.write(b"\xff\xfe[]")

        def context_dir():
            path = os.path.join(self.run_dir, "context.md")
            os.remove(path)
            os.mkdir(path)

        def roles_elsewhere():
            os.replace(os.path.join(self.run_dir, "roles.json"),
                       os.path.join(elsewhere, "roles.json"))
            return os.path.join(elsewhere, "roles.json")

        cases = (
            ("missing context", remove("context.md"), "file does not exist"),
            ("missing roles", remove("roles.json"), "file does not exist"),
            ("context is a directory", context_dir, "path is a directory"),
            ("roles not UTF-8", not_utf8, "is not valid UTF-8"),
            ("roles in another directory", roles_elsewhere,
             "must be in the same mktemp directory"),
        )
        for index, (name, break_input, expected) in enumerate(cases):
            with self.subTest(refusal=name):
                self.run_dir = self._mkdir(f"early-{index}")
                self.discover()
                self.stage([_entry("architect")])
                args = self.launch_args()
                moved = break_input()
                if moved is not None:
                    args[args.index("--roles-file") + 1] = moved
                # The SKILL launch shape: `> out.md 2> err.log`.
                err_log = os.path.join(self.run_dir, "err.log")
                with open(os.path.join(self.run_dir, "out.md"), "wb") as out, \
                        open(err_log, "wb") as err:
                    launch = subprocess.run(
                        [sys.executable, SCRIPT, *args],
                        stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                        env=self.env, cwd=self.project, timeout=120)
                self.assertEqual(launch.returncode, 2)
                with open(err_log, encoding="utf-8") as f:
                    logged = f.read()
                self.assertIn(expected, logged)
                self.assertEqual(logged.count(
                    council_common.STAGED_LAUNCH_RESTART), 1, logged)
                self.assertNotIn("re-run --check-staging-dir", logged)
                self.assertNotIn("re-run the pre-flight", logged)
                # What the old hint alone led to: a pre-flight re-run in
                # the same directory, which is refused.
                rerun = self.run_script(
                    "--check-staging-dir", self.run_dir,
                    "--skill-contract", EPOCH)
                self.assertEqual(rerun.returncode, 2, rerun.stdout)
                self.assertIn("already holds a council launch",
                              rerun.stderr)
        # Stdin mode has no staged directory to abandon.
        missing = os.path.join(elsewhere, "absent.json")
        proc = self.run_script("--roles-file", missing)
        self.assertEqual(proc.returncode, 2)
        self.assertIn(codex_council.STAGING_PATH_HINT, proc.stderr)
        self.assertNotIn(council_common.STAGED_LAUNCH_RESTART, proc.stderr)
        self.assertEqual(self.argvs(), [])

    def test_a_launch_directory_is_never_reused_for_another_launch(self):
        """The skill's launch command opens out.md and err.log before the
        runner starts, so from that moment the directory may hold a running
        council: the pre-flight and --discover refuse it, and after the run
        the report, log, and planning snapshot are left untouched."""
        snapshot_id = self.discover()
        self.stage([_routed(snapshot_id=snapshot_id)])
        preflight = self.run_script("--check-staging-dir", self.run_dir,
                                    "--skill-contract", EPOCH)
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        # What `> out.md 2> err.log` does before the runner even starts.
        out = open(os.path.join(self.run_dir, "out.md"), "wb")
        err = open(os.path.join(self.run_dir, "err.log"), "wb")
        self.addCleanup(out.close)
        self.addCleanup(err.close)
        running = self.run_script("--check-staging-dir", self.run_dir,
                                  "--skill-contract", EPOCH)
        self.assertEqual(running.returncode, 2, running.stdout)
        self.assertIn("already holds a council launch (out.md, err.log "
                      "present)", running.stderr)
        launch = subprocess.run(
            [sys.executable, SCRIPT, *self.launch_args()],
            stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=self.env,
            cwd=self.project, timeout=120)
        self.assertEqual(launch.returncode, 0)
        out.close()
        err.close()
        outputs = {}
        for name in ("out.md", "err.log",
                     council_discovery.SNAPSHOT_FILENAME):
            with open(os.path.join(self.run_dir, name), "rb") as f:
                outputs[name] = f.read()
        self.assertIn(b"CODEX_COUNCIL_DONE ok=1 total=1", outputs["err.log"])
        self.forget_discovery()
        for args in (("--check-staging-dir", self.run_dir),
                     ("--discover", self.run_dir)):
            with self.subTest(command=args[0]):
                proc = self.run_script(*args, "--skill-contract", EPOCH)
                self.assertEqual(proc.returncode, 2, proc.stdout)
                self.assertIn("already holds a council launch (out.md, "
                              "err.log, replies present)", proc.stderr)
                self.assertIn(council_common.LAUNCHED_DIR_RECOVERY,
                              proc.stderr)
        self.assertFalse(self.launch_discovered())
        for name, content in outputs.items():
            with open(os.path.join(self.run_dir, name), "rb") as f:
                self.assertEqual(f.read(), content, name)

    def test_a_subdirectory_launch_discovers_and_runs_at_the_git_root(self):
        """Execution-context parity from a Git subdirectory: discovery's
        config/read cwd and every worker's -C are the same Git top level,
        not the launch directory, so a .codex/config.toml below the root
        is outside the discovered baseline (whether codex exec also ignores
        it is Codex's side and is not verified live here)."""
        repo = self._mkdir("repo")
        env = {**self.env, "GIT_CONFIG_GLOBAL": os.devnull,
               "GIT_CONFIG_NOSYSTEM": "1"}
        subprocess.run(["git", "init", "-q", repo], env=env, check=True,
                       capture_output=True)
        subdir = os.path.join(repo, "pkg", "sub")
        os.makedirs(subdir)
        toplevel = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=subdir, env=env,
            check=True, capture_output=True, text=True).stdout.strip()
        request_log = os.path.join(self.root, "requests.log")
        env["FAKE_CODEX_REQUEST_LOG"] = request_log

        def run_from_subdir(*args):
            return subprocess.run(
                [sys.executable, SCRIPT, *args], capture_output=True,
                text=True, env=env, cwd=subdir, stdin=subprocess.DEVNULL,
                timeout=120)

        proc = run_from_subdir("--discover", self.run_dir,
                               "--skill-contract", EPOCH)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        snapshot, problem = council_discovery._read_snapshot(self.run_dir)
        self.assertIsNone(problem)
        self.stage([_routed(snapshot_id=snapshot["snapshot_id"])])
        launch = run_from_subdir(*self.launch_args())
        self.assertEqual(launch.returncode, 0, launch.stderr)
        config_reads = [
            json.loads(line)["params"]["cwd"]
            for line in fake_codex.read_lines(request_log)
            if json.loads(line)["method"] == "config/read"
        ]
        # Planning discovery, then the launch's own revalidation.
        self.assertEqual(config_reads, [toplevel, toplevel])
        self.assertEqual(snapshot["context"]["project_root"], toplevel)
        (argv,) = self.argvs()
        self.assertEqual(argv[argv.index("-C") + 1], toplevel)
        with open(os.path.join(self.pid_dir, "server.env"),
                  encoding="utf-8") as f:
            spawned_in = json.load(f)["cwd"]
        # The processes themselves ran in the subdirectory.
        self.assertEqual(os.path.realpath(spawned_in),
                         os.path.realpath(subdir))
        self.assertNotEqual(os.path.realpath(subdir),
                            os.path.realpath(toplevel))

    def test_reply_files_match_the_report_and_the_follower_reaches_done(self):
        snapshot_id = self.discover()
        self.stage([
            _entry("plain"),
            _entry("pin", model=CUSTOM, selection={"mode": "user"}),
            _routed("route", snapshot_id=snapshot_id),
            _native_effort("tune", snapshot_id=snapshot_id),
        ])
        out = open(os.path.join(self.run_dir, "out.md"), "wb")
        err = open(os.path.join(self.run_dir, "err.log"), "wb")
        self.addCleanup(out.close)
        self.addCleanup(err.close)
        launch = subprocess.Popen(
            [sys.executable, SCRIPT, *self.launch_args()],
            stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=self.env,
            cwd=self.project)
        self.addCleanup(lambda: launch.poll() is None and launch.kill())
        follower = subprocess.run(
            [sys.executable, SCRIPT, "--follow", self.run_dir,
             "--skill-contract", EPOCH],
            capture_output=True, text=True, env=self.env, timeout=120)
        self.assertEqual(follower.returncode, 0, follower.stderr)
        self.assertEqual(launch.wait(timeout=120), 0)
        lines = follower.stdout.splitlines()
        self.assertTrue(lines[0].startswith("[codex-council] dispatching 4 "))
        self.assertEqual(
            lines[1], "[codex-council] model selection: routing=auto; "
                      "discovery=ok; native=1 user=1 routed=1 native_effort=1 "
                      "fallback=0")
        self.assertRegex(lines[-1], council_liveness.FOLLOW_DONE_PATTERN)
        with open(os.path.join(self.run_dir, "out.md"), encoding="utf-8") as f:
            report = f.read()
        for role_id in ("plain", "pin", "route", "tune"):
            path = os.path.join(self.run_dir, "replies", f"{role_id}.md")
            self.assertTrue(any(ln.endswith(f" reply={path}") for ln in lines))
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            with open(path, encoding="utf-8") as f:
                body = f.read().partition("\n\n")[2]
            with self.subTest(role=role_id):
                self.assertIn(f"## Architect ({role_id})\n\n"
                              "_Model selection: ", body)
                self.assertIn(body.rstrip(), report)


if __name__ == "__main__":
    unittest.main()
