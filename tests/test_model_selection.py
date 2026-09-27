"""v1.0.0 model selection: the roles.json contract, the resolver, launch
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

import contextlib
import copy
import dataclasses
import glob
import io
import json
import os
import random
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.abspath(os.path.join(
    TESTS_DIR, "..", "plugins", "codex-council", "skills", "codex-council",
    "scripts",
))
sys.path.insert(0, SCRIPTS_DIR)
sys.path.insert(0, TESTS_DIR)

import codex_council  # noqa: E402
import fake_codex  # noqa: E402

SCRIPT = os.path.join(SCRIPTS_DIR, "codex_council.py")
EPOCH = str(codex_council.SKILL_CONTRACT_EPOCH)
ROUTING_ENV = codex_council.MODEL_ROUTING_ENV
NATIVE = fake_codex.NATIVE_MODEL          # future-orion-2032, the native model
VEGA = "future-vega-2033"                 # visible, recommended
LYRA = "future-lyra-2030"                 # visible, retires 2031-01-01
HIDDEN = "future-hidden-2031"             # hidden
CUSTOM = "acme/future-review-2034:rev2"   # a custom-provider id, never listed
SNAPSHOT_ID = "0123456789abcdef"
NOW = "2026-09-27T12:00:00Z"
AFTER_LYRA_RETIRES = "2031-06-01T00:00:00Z"
SENTINELS = fake_codex.EXEC_SENTINELS
REWRITE = "rewrite the entire file passed to --roles-file"
INHERIT_HINT = ("omit model, effort, and selection to inherit native "
                "configuration")
FORBIDDEN_METHODS = ("thread/start", "thread/resume", "turn/start")


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


def _parse(entries, require_selection=False):
    raw = entries if isinstance(entries, str) else json.dumps(entries)
    return codex_council._parse_roles_json(raw, require_selection)


def _role(rid="architect", model=None, effort=None, mode=None,
          snapshot_id=SNAPSHOT_ID, reason="grounded in the snapshot"):
    """A parsed Role (selection attached) without going through JSON."""
    selection = None
    if mode == "user":
        selection = codex_council.Selection("user")
    elif mode is not None:
        selection = codex_council.Selection(mode, snapshot_id, reason)
    return codex_council.Role(
        rid, rid.title(), " ".join(_instruction()), model, effort, selection)


def _assert_usage_exit(test, callable_, *, expect_in_stderr):
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        with test.assertRaises(SystemExit) as ctx:
            callable_()
    test.assertEqual(ctx.exception.code, 2)
    test.assertIn(expect_in_stderr, buf.getvalue())
    return buf.getvalue()


def _clean_env(**extra):
    """os.environ minus anything that changes council or discovery verdicts."""
    dropped = {
        "CODEX_API_KEY", ROUTING_ENV, codex_council.SESSION_KEY_ENV,
        codex_council.MAX_PARALLEL_ENV, codex_council.STALL_SECS_ENV,
    }
    env = {
        key: value for key, value in os.environ.items()
        if key not in dropped and not key.startswith("FAKE_CODEX_")
    }
    env.update(extra)
    return env


# ---------- synthetic snapshots (built by the real snapshot builder) ----------

def _catalog(entries):
    catalog = codex_council._new_catalog()
    page, problem = codex_council._normalize_model_page({"data": entries})
    assert problem is None, problem
    codex_council._merge_model_page(catalog, page, [])
    return catalog


def _snapshot(snapshot_id=SNAPSHOT_ID, routing_mode="auto", entries=None,
              **observed):
    """A discovery snapshot; keyword args override the observations."""
    base = {
        "context": dict(
            codex_council._EMPTY_DISCOVERY_CONTEXT, project_root="/proj",
            launch_cwd="/proj", codex_executable="/bin/codex",
            codex_cli_version="9.9.9", codex_home="/home/.codex",
        ),
        "problems": [],
        "conclusive": True,
        "account": {"type": "chatgpt", "requires_openai_auth": True},
        "configured": {"model": NATIVE, "effort": "deliberate",
                       "provider": None, "model_origin": "user",
                       "effort_origin": "user"},
        "managed": {"status": "absent", "model": None, "effort": None,
                    "provider_keys": []},
        "catalog": _catalog(
            fake_codex.default_catalog() if entries is None else entries),
    }
    base.update(observed)
    return codex_council._build_snapshot(
        snapshot_id=snapshot_id, created_at="2026-09-27T12:00:00Z",
        plugin_version="9.8.7", routing_mode=routing_mode, **base,
    )


def _unavailable(problem="rpc_error:model/list:-32601"):
    return _snapshot(conclusive=False, problems=[problem], account=None,
                     configured=None, managed=None, catalog=None)


def _managed_present():
    return {"status": "present", "model": "future-managed-2035",
            "effort": "deliberate", "provider_keys": []}


def _resolve(role, planning=None, launch=None, routing_mode="auto", now=NOW):
    return codex_council._resolve_selection(
        role, planning, launch, routing_mode, now)


# ---------- the value grammar (model and effort) ----------

class SelectionValueGrammarTests(unittest.TestCase):
    def test_future_values_and_case_are_preserved(self):
        for model in (NATIVE, CUSTOM, "Future.Model@2+exp", "x"):
            with self.subTest(model=model):
                self.assertEqual(_parse([_entry(model=model)])[0].model, model)
        for effort in ("adaptive-v2", "deliberate", "High", "x-high",
                       "X.High:2", "ultra"):
            with self.subTest(effort=effort):
                self.assertEqual(
                    _parse([_entry(effort=effort)])[0].effort, effort)

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
                        self, lambda f=field, v=value: _parse([_entry(**{f: v})]),
                        expect_in_stderr=f"optional field '{field}'",
                    )
                    self.assertIn(REWRITE, err)
                    self.assertIn(INHERIT_HINT, err)

    def test_reserved_inheritance_words_are_not_model_ids(self):
        for value in ("inherit", "default", "INHERIT", "Default", "InHeRiT"):
            for selection in (None, {"mode": "user"}):
                with self.subTest(value=value, selection=selection):
                    extra = {"model": value}
                    if selection:
                        extra["selection"] = selection
                    err = _assert_usage_exit(
                        self, lambda extra=extra: _parse([_entry(**extra)]),
                        expect_in_stderr=(
                            f"model '{value}' is not an inheritance value; "
                            f"{INHERIT_HINT}"),
                    )
                    self.assertIn(REWRITE, err)

    def test_accepted_values_stay_single_argv_items_and_toml_strings(self):
        for effort in ("adaptive-v2", "X.High:2", "a/b@c+d"):
            with self.subTest(effort=effort):
                cmd = codex_council._fresh_cmd("/r", CUSTOM, effort)
                self.assertEqual(cmd[cmd.index("-m") + 1], CUSTOM)
                setting = cmd[cmd.index("-c") + 1]
                self.assertEqual(setting, f'model_reasoning_effort="{effort}"')
                if tomllib is not None:
                    self.assertEqual(
                        tomllib.loads(setting)["model_reasoning_effort"],
                        effort)


# ---------- the selection object ----------

class SelectionObjectParsingTests(unittest.TestCase):
    def test_inheritance_is_omission(self):
        role = _parse([_entry()])[0]
        self.assertIsNone(role.model)
        self.assertIsNone(role.effort)
        self.assertIsNone(role.selection)
        decision = codex_council._role_decision(role)
        self.assertEqual(decision.provenance, "native")
        self.assertIsNone(decision.dispatch_model)
        self.assertIsNone(decision.dispatch_effort)

    def test_user_mode_takes_either_or_both_values_and_an_optional_reason(self):
        for extra in ({"model": CUSTOM}, {"effort": "brisk"},
                      {"model": CUSTOM, "effort": "brisk"}):
            with self.subTest(extra=extra):
                role = _parse([_entry(selection={"mode": "user"}, **extra)])[0]
                self.assertEqual(role.selection,
                                 codex_council.Selection("user"))
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
        self.assertEqual(role.selection, codex_council.Selection(
            "routed", SNAPSHOT_ID, "narrow checks fit the fast model"))

    def test_routed_needs_both_values_a_snapshot_id_and_a_reason(self):
        cases = (
            (_routed(model=None), "needs both 'model' and 'effort'"),
            (_routed(effort=None), "needs both 'model' and 'effort'"),
            (_routed(snapshot_id=None), "needs 'snapshot_id'"),
            (_routed(snapshot_id="0123456789ABCDEF"), "needs 'snapshot_id'"),
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
        for ch in codex_council.LINEBREAK_CHARS:
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

    def test_untagged_pins_are_user_pins_only_for_direct_cli_use(self):
        role = _parse([_entry(model=CUSTOM, effort="brisk")])[0]
        self.assertEqual(role.selection, codex_council.Selection("user"))
        for extra in ({"model": CUSTOM}, {"effort": "brisk"}):
            with self.subTest(extra=extra):
                err = _assert_usage_exit(
                    self, lambda e=extra: _parse([_entry(**e)],
                                                 require_selection=True),
                    expect_in_stderr="declare selection.mode: 'user' for an "
                                     "explicit user request, 'routed' or "
                                     "'native_effort' for a runtime-grounded "
                                     "choice")
                self.assertIn(REWRITE, err)
        # Inheritance and tagged selections are fine on the skill path.
        roles = _parse([_entry("a"), _routed("b"),
                        _entry("c", model=CUSTOM, selection={"mode": "user"})],
                       require_selection=True)
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
            "bad-model": [_entry(model="a b")],
            "reserved-model": [_entry(model="inherit")],
            "untagged-on-skill-path": [_entry(model=CUSTOM)],
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
                        _parse(entries, require_selection=True)
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
                    codex_council.SelectionDecision("inherit", "native"))

    def test_explicit_pin_is_forwarded_unchanged_even_off_catalog(self):
        """AC9: catalog absence never rejects or replaces an explicit pin."""
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
        self.assertEqual(decision.note, codex_council.UNVERIFIED_MODEL_ADVISORY)

    def test_untagged_pin_resolves_as_a_user_pin(self):
        decision = _resolve(_role(model=VEGA, effort="brisk"), _snapshot())
        self.assertEqual(decision.mode, "user")
        self.assertEqual(decision.provenance, "user")
        self.assertIsNone(decision.note)

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

    def test_partial_pin_advisory_follows_managed_defaults(self):
        """AC7: a partial pin while managed defaults exist is flagged."""
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
                    codex_council.PARTIAL_PIN_ADVISORY in note, flagged)
        both = _role(model=VEGA, effort="brisk", mode="user")
        self.assertIsNone(_resolve(both, _snapshot(
            managed=_managed_present())).note)
        self.assertIsNone(_resolve(partial, None).note)

    def test_routed_pair_is_dispatched_exactly_as_authored(self):
        role = _role(model=VEGA, effort="deliberate", mode="routed")
        decision = _resolve(role, _snapshot())
        self.assertEqual(decision, codex_council.SelectionDecision(
            "routed", "routed", VEGA, "deliberate", VEGA, "deliberate",
            "grounded in the snapshot", None))

    def test_native_effort_pins_the_native_model_the_evidence_proves(self):
        role = _role(effort="brisk", mode="native_effort")
        planned = _resolve(role, _snapshot())
        self.assertEqual((planned.provenance, planned.dispatch_model,
                          planned.dispatch_effort),
                         ("native_effort", NATIVE, "brisk"))
        self.assertIsNone(planned.requested_model)
        # The native model changed before launch: the LAUNCH one is pinned.
        launch = _snapshot(configured={
            "model": VEGA, "effort": None, "provider": None,
            "model_origin": "project", "effort_origin": None})
        launched = _resolve(role, _snapshot(), launch)
        self.assertEqual((launched.dispatch_model, launched.dispatch_effort),
                         (VEGA, "brisk"))

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
        """AC4: a failed launch discovery never blocks; it inherits."""
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
        signed_out = _snapshot(account={"type": None,
                                        "requires_openai_auth": True})
        routed = _resolve(_role(model=VEGA, effort="brisk", mode="routed"),
                          _snapshot(), signed_out)
        self.assertEqual(
            routed.note, "launch discovery reports routing unavailable: not "
                         "signed in: catalog is not account-grounded")
        native = _resolve(_role(effort="brisk", mode="native_effort"),
                          _snapshot(), signed_out)
        self.assertEqual(native.provenance, "native_effort")

    def test_evidence_change_since_planning_falls_back_with_the_detail(self):
        """AC8: launch evidence that no longer supports the pair inherits."""
        prefix = "selection evidence changed since discovery: "
        without_vega = [e for e in fake_codex.default_catalog()
                        if e["model"] != VEGA]
        hidden_vega = [fake_codex.model_entry(VEGA, hidden=True)] + without_vega
        narrowed_vega = [fake_codex.model_entry(VEGA, supportedReasoningEfforts=[
            {"reasoningEffort": "deliberate", "description": "d"}],
            defaultReasoningEffort="deliberate")] + without_vega
        cases = (
            (without_vega, VEGA, NOW,
             f"model '{VEGA}' is not an advertised execution id in snapshot "
             "fedcba9876543210"),
            (hidden_vega, VEGA, NOW,
             f"cannot route to hidden model '{VEGA}' (hidden models are for "
             "explicit user pins)"),
            (narrowed_vega, VEGA, NOW,
             f"effort 'brisk' is not advertised for model '{VEGA}'"),
            (None, LYRA, AFTER_LYRA_RETIRES,
             f"model '{LYRA}' advertised retirement passed "
             "(2031-01-01T00:00:00Z)"),
        )
        for entries, model, now, detail in cases:
            launch = _snapshot(snapshot_id="fedcba9876543210", entries=entries)
            decision = _resolve(
                _role(model=model, effort="brisk", mode="routed"),
                _snapshot(), launch, now=now)
            with self.subTest(detail=detail):
                self.assertEqual(decision.provenance, "fallback")
                self.assertEqual(decision.note, prefix + detail)
                self.assertIsNone(decision.dispatch_model)

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
            "model_origin": "user", "effort_origin": None}))
        self.assertEqual(
            moved.note,
            prefix + f"effort 'adaptive-v2' is not advertised for model '{VEGA}'")

    def test_a_native_model_id_outside_the_grammar_is_never_pinned(self):
        odd = "odd model id"
        snapshot = _snapshot(
            entries=[fake_codex.model_entry(odd)],
            configured={"model": odd, "effort": None, "provider": None,
                        "model_origin": "user", "effort_origin": None})
        self.assertEqual(snapshot["native"]["resolution"], "proven")
        decision = _resolve(_role(effort="brisk", mode="native_effort"),
                            None, snapshot)
        self.assertEqual(decision.provenance, "fallback")
        self.assertIn(f"native model id {odd!r} is not a dispatchable "
                      "selection value", decision.note)

    def test_catalog_order_and_recommendation_never_change_decisions(self):
        """AC1/AC5: no ranking by position, id spelling, or isDefault."""
        roles = [
            _role("a"),
            _role("b", model=VEGA, effort="brisk", mode="routed"),
            _role("c", model=LYRA, effort="deliberate", mode="routed"),
            _role("d", effort="adaptive-v2", mode="native_effort"),
            _role("e", model=CUSTOM, mode="user"),
            _role("f", model=HIDDEN, effort="brisk", mode="routed"),
        ]
        automatic = [r for r in roles if codex_council._is_automatic(r)]

        def verdicts(snapshot):
            return (
                [_resolve(r, snapshot) for r in roles],
                [codex_council._authoring_problem(r, snapshot, None, NOW)
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
        """AC5: native orion vs recommended vega; nothing auto-picks vega."""
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
        """AC3: an unseen model and effort route with no code change."""
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
            codex_council._role_decision(_role(model=VEGA)).provenance, "user")
        routed = codex_council._role_decision(
            _role(model=VEGA, effort="brisk", mode="routed"))
        self.assertEqual(routed.provenance, "fallback")
        attached = codex_council.Role(
            "x", "X", "i", decision=codex_council.SelectionDecision(
                "routed", "routed", VEGA, "brisk", VEGA, "brisk", "r"))
        self.assertIs(codex_council._role_decision(attached),
                      attached.decision)


# ---------- authoring validation against the planning snapshot ----------

class AuthoringValidationTests(unittest.TestCase):
    def _validate(self, roles, planning=None, problem=None,
                  routing_mode="auto", now=NOW):
        codex_council._validate_selection_authoring(
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
        path = os.path.join(run_dir, codex_council.SNAPSHOT_FILENAME)
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
            planning, problem = codex_council._read_snapshot(run_dir)
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
        self._rejects(
            [_role(model=LYRA, effort="brisk", mode="routed")],
            f"model '{LYRA}' advertised retirement passed "
            "(2031-01-01T00:00:00Z)",
            planning=_snapshot(), now=AFTER_LYRA_RETIRES)
        # Before the date the same pair is valid.
        self._validate([_role(model=LYRA, effort="brisk", mode="routed")],
                       planning=_snapshot())

    def test_effort_must_be_advertised_for_that_model_exactly(self):
        for effort in ("adaptive-v2", "Brisk", "BRISK"):
            with self.subTest(effort=effort):
                self._rejects(
                    [_role(model=VEGA, effort=effort, mode="routed")],
                    f"effort '{effort}' is not advertised for model '{VEGA}'",
                    planning=_snapshot())

    def test_native_effort_requires_a_proven_native_model(self):
        """AC7: managed new-thread defaults make effort-only unavailable."""
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
                "model_origin": None, "effort_origin": None}))
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
            patch.object(codex_council, "_discover",
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
            codex_council._write_snapshot(self.run_dir, snapshot)

    def _preflight(self, require_selection=True):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            codex_council._check_staging_dir(self.run_dir, require_selection)
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

    def test_skill_path_rejects_untagged_pins_direct_use_accepts_them(self):
        self._stage([_entry(model=CUSTOM)])
        _assert_usage_exit(self, self._preflight,
                           expect_in_stderr="declare selection.mode")
        lines = self._preflight(require_selection=False)
        self.assertEqual(lines[1], "[codex-council] selection plan: "
                                   f"architect: explicit override (model "
                                   f"{CUSTOM})")

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

        with patch.object(codex_council, "_discover",
                          side_effect=fake_discover):
            resolved, snapshot = codex_council._resolve_launch_selections(
                roles, self.run_dir, routing_mode)
        return resolved, snapshot, calls

    def test_no_automatic_role_means_no_launch_discovery(self):
        roles = [_role("a"), _role("b", model=CUSTOM, mode="user")]
        resolved, launch, calls = self._resolve_launch(roles)
        self.assertEqual(calls, [])
        self.assertIsNone(launch)
        self.assertEqual([r.decision.provenance for r in resolved],
                         ["native", "user"])

    def test_one_frozen_discovery_serves_every_automatic_role(self):
        codex_council._write_snapshot(self.run_dir, _snapshot())
        roles = [_role("a", model=VEGA, effort="brisk", mode="routed"),
                 _role("b", effort="adaptive-v2", mode="native_effort"),
                 _role("c", model=LYRA, effort="deliberate", mode="routed")]
        resolved, launch, calls = self._resolve_launch(roles)
        self.assertEqual(calls, ["auto"])
        self.assertEqual(
            [(r.decision.provenance, r.decision.dispatch_model)
             for r in resolved],
            [("routed", VEGA), ("native_effort", NATIVE), ("routed", LYRA)])
        # The launch snapshot is never written over the planning one.
        planning, _ = codex_council._read_snapshot(self.run_dir)
        self.assertEqual(planning["snapshot_id"], SNAPSHOT_ID)

    def test_routing_off_skips_launch_discovery(self):
        roles = [_role("a", model=VEGA, effort="brisk", mode="routed")]
        resolved, launch, calls = self._resolve_launch(roles, "off")
        self.assertEqual(calls, [])
        self.assertEqual(resolved[0].decision.note,
                         "CODEX_COUNCIL_MODEL_ROUTING=off")

    def test_authoring_defects_exit_before_discovery(self):
        roles = [_role("a", model=VEGA, effort="brisk", mode="routed")]
        _assert_usage_exit(self, lambda: self._resolve_launch(roles),
                           expect_in_stderr="requires this run's discovery "
                                            "snapshot")


# ---------- structured failure records ----------

def _api_failure(status, error, *, event="turn.failed"):
    """One codex failure event whose message is the JSON-in-message form."""
    message = json.dumps({"type": "error", "status": status, "error": error})
    if event == "error":
        return json.dumps({"type": "error", "message": message})
    return json.dumps({"type": "turn.failed", "error": {"message": message}})


NOT_FOUND = {"type": "invalid_request_error", "code": "model_not_found",
             "param": "model",
             "message": f"The model '{VEGA}' does not exist or you do not "
                        "have access to it."}


class FailureRecordTests(unittest.TestCase):
    def test_nested_json_message_becomes_one_structured_record(self):
        stdout = "\n".join([_api_failure(404, NOT_FOUND, event="error"),
                            _api_failure(404, NOT_FOUND)])
        self.assertEqual(codex_council._failure_records(stdout), [
            codex_council.FailureRecord(
                404, "invalid_request_error", "model_not_found", "model",
                NOT_FOUND["message"])])

    def test_direct_error_objects_and_plain_messages(self):
        stdout = "\n".join([
            json.dumps({"type": "error", "error": {
                "code": "insufficient_quota", "message": "No credit."}}),
            json.dumps({"type": "turn.failed", "error": "plain failure"}),
        ])
        self.assertEqual(codex_council._failure_records(stdout), [
            codex_council.FailureRecord(None, None, "insufficient_quota",
                                        None, "No credit."),
            codex_council.FailureRecord(message="plain failure"),
        ])

    def test_json_in_message_decoding_is_bounded(self):
        inner = {"code": "model_not_found", "message": "deepest"}
        message = json.dumps(inner)
        for _ in range(3):
            message = json.dumps({"error": {"message": message}})
        records = codex_council._failure_records(
            json.dumps({"type": "error", "message": message}))
        self.assertFalse(any(r.code == "model_not_found" for r in records))
        self.assertEqual(records[-1].message, json.dumps(inner))
        # Three levels are decoded.
        message = json.dumps(inner)
        for _ in range(2):
            message = json.dumps({"error": {"message": message}})
        records = codex_council._failure_records(
            json.dumps({"type": "error", "message": message}))
        self.assertEqual(records[-1].code, "model_not_found")

    def test_only_error_and_turn_failed_events_are_read(self):
        stdout = "\n".join(
            json.dumps({"type": "item.completed", "item": {
                "type": item_type, "text": _api_failure(404, NOT_FOUND),
                "message": _api_failure(404, NOT_FOUND)}})
            for item_type in ("agent_message", "reasoning", "error",
                              "command_execution"))
        self.assertEqual(codex_council._failure_records(stdout), [])


# ---------- failure classification (both paths) ----------

class FailureClassificationTests(unittest.TestCase):
    def _classify(self, stdout, stderr="", model=None, resume=False):
        text = codex_council._failure_text(stdout, stderr)
        records = codex_council._failure_records(stdout)
        return codex_council._failure_verdict(text, records, model, resume)

    def test_quota_codes_and_prose_are_terminal_even_with_429(self):
        for code in sorted(codex_council.QUOTA_ERROR_CODES):
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
        self.assertEqual(
            self._classify("", "Selected model is at capacity. Please try a "
                               "different model.", model=VEGA), "5xx")

    def test_unrelated_text_naming_a_setting_does_not_mask_a_rejection(self):
        self.assertEqual(
            self._classify(_api_failure(404, NOT_FOUND),
                           "warning: unknown config key service_tier", VEGA),
            "model-rejected")

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

    def test_rejection_message_names_what_was_sent_and_one_action(self):
        stdout = _api_failure(404, NOT_FOUND)
        text = codex_council._failure_text(stdout, "")
        records = codex_council._failure_records(stdout)
        provider = NOT_FOUND["message"].rstrip(".")
        for provenance, model, action in (
            ("user", VEGA, "Change or remove the explicit pin."),
            ("routed", VEGA, "Re-run this role with model, effort, and "
                             "selection omitted to inherit native "
                             "configuration."),
            ("native_effort", NATIVE, "Re-run this role with model, effort, "
                                      "and selection omitted to inherit "
                                      "native configuration."),
            ("native", None, "Update the Codex configuration (model) or pin "
                             "an available model."),
            ("fallback", None, "Update the Codex configuration (model) or "
                               "pin an available model."),
        ):
            decision = codex_council.SelectionDecision(
                "user", provenance, dispatch_model=model)
            subject = (f"requested model '{model}'" if model
                       else "natively configured model")
            for phase, kept in (("resume", " and the saved thread was kept"),
                                ("exec", "")):
                with self.subTest(provenance=provenance, phase=phase):
                    self.assertEqual(
                        codex_council._classify_failure(
                            text, 1, phase, records, decision),
                        f"[model-rejected] Codex rejected the {subject} for "
                        f"this invocation: {provider}. No substitute model "
                        f"was tried{kept}. {action}")

    def test_quota_and_legacy_tags_keep_the_failure_text(self):
        stdout = _api_failure(429, {"code": "insufficient_quota",
                                    "message": "No credit."})
        text = codex_council._failure_text(stdout, "")
        records = codex_council._failure_records(stdout)
        self.assertEqual(
            codex_council._classify_failure(text, 1, "exec", records),
            f"[quota] {text}")
        self.assertEqual(
            codex_council._classify_failure("502 bad gateway", 1, "exec"),
            "[retriable:5xx] 502 bad gateway")
        self.assertEqual(codex_council._classify_failure("", 7, "resume"),
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
        return dataclasses.replace(role, decision=codex_council.SelectionDecision(
            role.selection.mode if role.selection else "inherit",
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
        selection = codex_council.Selection(
            mode, SNAPSHOT_ID if mode != "user" else None, reason)
    decision = codex_council.SelectionDecision(
        mode or "inherit", provenance, model, effort, dispatch[0],
        dispatch[1], reason, note)
    return codex_council.Role(rid, rid.title(), "i", model, effort, selection,
                              decision)


def _provenance_roles():
    return [
        _decided_role("plain", "native"),
        _decided_role("pin", "user", model=CUSTOM, effort="brisk",
                      mode="user", dispatch=(CUSTOM, "brisk"),
                      note=codex_council.UNVERIFIED_MODEL_ADVISORY),
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
            (None, "discovery not run (no runtime-grounded selections)"),
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
             "discovery not run (no runtime-grounded selections)"),
            (("off", True, None), ("not-run", "CODEX_COUNCIL_MODEL_ROUTING=off"),
             "discovery not run (CODEX_COUNCIL_MODEL_ROUTING=off)"),
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
                got = codex_council._launch_discovery_state(*args)
                self.assertEqual(got, state)
                self.assertEqual(
                    codex_council._discovery_sentence(*got, args[2]), sentence)

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
        note = "launch discovery unavailable: stderr: bad\n## Forged x"
        role = _decided_role("fell", "fallback", model=VEGA, effort="brisk",
                             mode="routed", reason="r", note=note)
        report = codex_council._format_report(
            _results([role]), 1.0, f"launch discovery unavailable: {note}")
        self.assertNotIn("\n## Forged", report)
        self.assertEqual(report.count("\\n## Forged\\u2028x"), 2)
        lines = codex_council._model_selection_lines(
            [role], "auto", "unavailable", "x\ny")
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertEqual(line.splitlines(), [line])

    def test_model_selection_err_log_lines(self):
        lines = codex_council._model_selection_lines(
            _provenance_roles(), "auto", "unavailable", "codex_missing")
        self.assertEqual(lines, [
            "[codex-council] model selection: routing=auto; "
            "discovery=unavailable (codex_missing); native=1 user=1 routed=1 "
            "native_effort=1 fallback=1",
            "[codex-council:fell] routing fell back to native inheritance: "
            "launch discovery unavailable: codex_missing",
        ])
        self.assertEqual(
            codex_council._model_selection_lines(
                [_decided_role("plain", "native")], "off", "not-run",
                "no runtime-grounded selections"),
            ["[codex-council] model selection: routing=off; discovery=not-run "
             "(no runtime-grounded selections); native=1 user=0 routed=0 "
             "native_effort=0 fallback=0"])


# ---------- end to end: the real script with the fake codex ----------

_FAKE_BIN = {}


def setUpModule():
    _FAKE_BIN["tmp"] = tempfile.TemporaryDirectory()
    _FAKE_BIN["dir"] = _FAKE_BIN["tmp"].name
    fake_codex.install(_FAKE_BIN["dir"])


def tearDownModule():
    _FAKE_BIN.pop("tmp").cleanup()


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
            PATH=_FAKE_BIN["dir"] + os.pathsep + os.environ.get("PATH", ""),
            FAKE_CODEX_SCENARIO=self.scenario_path,
            FAKE_CODEX_METHOD_LOG=self.method_log,
            FAKE_CODEX_ARGV_DIR=self.argv_dir,
            FAKE_CODEX_PID_DIR=self.pid_dir,
            XDG_STATE_HOME=self.state_home,
            CODEX_HOME=self._mkdir("codex-home"),
        )
        self.scenario(fake_codex.default_scenario())
        self.addCleanup(self._assert_only_discovery_methods)

    def _mkdir(self, name):
        path = os.path.join(self.root, name)
        os.mkdir(path, 0o700)
        return path

    def _assert_only_discovery_methods(self):
        for method in fake_codex.read_lines(self.method_log):
            if not method.startswith("response:"):
                self.assertIn(method, fake_codex.DISCOVERY_METHODS)
            self.assertNotIn(method, FORBIDDEN_METHODS)

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
        snapshot, problem = codex_council._read_snapshot(self.run_dir)
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
        """AC2: fresh and resume carry no -m/-c; no launch discovery."""
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
                self.assertIn("Model selection: discovery not run (no "
                              "runtime-grounded selections).", proc.stdout)
        fresh, resumed = self.argvs()
        self.assertNotIn("resume", fresh)
        self.assertEqual(resumed[resumed.index("resume") + 1],
                         self.saved_thread("architect"))
        for argv in (fresh, resumed):
            self.assert_no_overrides(argv)
            # AC5: the recommended catalog model is never picked for them.
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

    def test_native_effort_pins_the_launch_native_model(self):
        snapshot_id = self.discover()
        self.stage([_native_effort(effort="brisk", snapshot_id=snapshot_id)])
        # The native model changes between planning and launch.
        scenario = fake_codex.default_scenario()
        scenario["methods"]["config/read"]["result"]["config"]["model"] = VEGA
        self.scenario(scenario)
        proc = self.launch()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        (argv,) = self.argvs()
        self.assertEqual(argv[3:7], ["-m", VEGA, "-c",
                                     'model_reasoning_effort="brisk"'])
        self.assertIn("(routed effort: brisk on native model "
                      f"{VEGA})", proc.stdout)
        self.assertIn(f"sent model {VEGA} (pinned native model), effort "
                      "brisk", proc.stdout)

    def test_launch_discovery_failure_inherits_and_the_run_succeeds(self):
        """AC4: an unavailable launch discovery never blocks the council."""
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
        """AC8: an account switch shows a different catalog at launch."""
        snapshot_id = self.discover()
        self.stage([_routed(snapshot_id=snapshot_id)])
        self.scenario(fake_codex.default_scenario(catalog=[
            e for e in fake_codex.default_catalog() if e["model"] != VEGA]))
        proc = self.launch()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        (argv,) = self.argvs()
        self.assert_no_overrides(argv)
        self.assertRegex(
            proc.stderr,
            r"\[codex-council:architect\] routing fell back to native "
            r"inheritance: selection evidence changed since discovery: "
            rf"model '{VEGA}' is not an advertised execution id in snapshot "
            r"[0-9a-f]{16}\n")

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
        """AC9: no catalog check can replace or block an explicit pin."""
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
        """AC9/AC11: no substitute, one subprocess, saved thread kept."""
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
        self.assertIn("Update the Codex configuration (model) or pin an "
                      "available model.", proc.stdout)
        self.assertNotIn("is stale", proc.stderr)
        self.assertEqual(self.saved_thread("architect"), thread)

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

    def test_quota_429_is_terminal_with_one_subprocess(self):
        self.stage([_entry(text="Review " + SENTINELS["quota_429"])])
        proc = self.launch()
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(len(self.argvs()), 1)
        self.assertIn("_Failed: [quota] ", proc.stdout)
        self.assertNotIn("retriable error", proc.stderr)

    def test_resume_after_a_native_default_change_warns_on_the_same_thread(self):
        """AC11: Codex's own advisory is kept verbatim; the UUID is kept."""
        self.stage([_entry()])
        self.assertEqual(self.launch().returncode, 0)
        thread = self.saved_thread("architect")
        self.reset_argvs()
        self.stage([_entry(text="Review " + SENTINELS["resume_advisory"])])
        proc = self.launch()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        (argv,) = self.argvs()
        self.assert_no_overrides(argv)
        self.assertEqual(argv[argv.index("resume") + 1], thread)
        self.assertIn(
            "_Warning: codex reported: This session was recorded with model "
            f"`{fake_codex.RECORDED_MODEL}` but is resuming with `{NATIVE}`. "
            f"Consider switching back to `{fake_codex.RECORDED_MODEL}` as it "
            "may affect Codex performance._", proc.stdout)
        self.assertNotIn("adopted new id", proc.stdout + proc.stderr)
        self.assertEqual(self.saved_thread("architect"), thread)

    def test_skill_path_refuses_untagged_pins_before_any_worker(self):
        self.stage([_entry(model=CUSTOM)])
        proc = self.launch()
        self.assertEqual(proc.returncode, 2)
        self.assertIn("declare selection.mode", proc.stderr)
        self.assertNotIn("dispatching", proc.stderr)
        self.assertEqual(self.argvs(), [])
        # Direct CLI use (no --skill-contract) keeps the explicit-pin meaning.
        proc = self.launch(skill_contract=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        (argv,) = self.argvs()
        self.assertEqual(argv[argv.index("-m") + 1], CUSTOM)

    def test_authoring_defect_at_launch_exits_2_before_any_worker(self):
        self.discover()
        self.stage([_routed(snapshot_id="ffffffffffffffff")])
        proc = self.launch()
        self.assertEqual(proc.returncode, 2)
        self.assertIn("does not identify this run's discovery snapshot",
                      proc.stderr)
        self.assertIn(REWRITE, proc.stderr)
        self.assertNotIn("dispatching", proc.stderr)
        self.assertEqual(self.argvs(), [])
        self.assertFalse(self.launch_discovered())

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
        self.assertRegex(lines[-1], codex_council.FOLLOW_DONE_PATTERN)
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
