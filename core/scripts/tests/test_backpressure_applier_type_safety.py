"""Pins for the backpressure rollback APPLIER's type safety ().

cmd_backpressure (state-update-audit.py, Step 8.85) executes the rollback_actions
that meta-backpressure emits. It had three defects, all measured on live data:

  (A) ZEROING. A null prior (rollback_to None) was written as "0.0" whatever the
      field's type. The g-115-2677 floor exists for NUMERIC weights; over a dict
      it replaced live encoding-strategy records with 0.0.
  (B) SILENT NON-APPLY. A dict rollback_to went through str(), a Python repr, so
      meta-set stored a STRING and type_destruction refused it. The rc was never
      read, so rollback_history said "reverted" while nothing changed.
  (C) LOST AUDIT EVENT. The evolution event carried date "", which the endpoint
      refuses (YYYY-MM-DD), and nothing checked that rc either.

The meta-set transport is pinned against the PRODUCTION parser
(mind_api meta_yaml._coerce_set_value + _assert_type_preserved), not just
json.loads, so a change on either side of the contract fails here.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]        # core/scripts
_ROOT = _SCRIPTS.parents[1]                           # PROJECT_ROOT
for _p in (str(_SCRIPTS), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from mind_api.src.meta import meta_yaml as MY  # noqa: E402


def _import():
    spec = importlib.util.spec_from_file_location(
        "state_update_audit_ts", _SCRIPTS / "state-update-audit.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["state_update_audit_ts"] = mod
    spec.loader.exec_module(mod)
    return mod


MOD = _import()

FIELD = "reflection_effectiveness_by_type"
PRIOR = {"execution": {"total": 58, "effective": 58, "rate": 1.0},
         "spark": {"total": 3, "effective": 3, "rate": 1.0}}
FAILED = {"execution": {"total": 107, "effective": 107, "rate": 1.0},
          "spark": {"total": 3, "effective": 3, "rate": 1.0}}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _action(field=FIELD, rollback_to=PRIOR, failed_value=FAILED, mc="mc-test"):
    return {"strategy_file": "reflection-strategy.yaml", "field": field,
            "rollback_to": rollback_to, "failed_value": failed_value,
            "meta_change_id": mc, "reason": "test regression"}


class _Fake:
    """Records every _run argv. The rc and output of each script can be set;
    meta-read echoes back whatever meta-set last wrote, as the real store does."""

    def __init__(self, actions, meta_set_rc=0, meta_set_err="",
                 readback=None, readback_rc=0, evo_rc=0):
        self.payload = json.dumps({"rollback_actions": actions,
                                   "dead_end_candidates": [], "graduated": []})
        self.meta_set_rc, self.meta_set_err = meta_set_rc, meta_set_err
        self.readback, self.readback_rc, self.evo_rc = readback, readback_rc, evo_rc
        self.calls, self.stdin, self.store = [], {}, {}

    def __call__(self, argv, input_text=None, timeout=None):
        self.calls.append(list(argv))
        name = argv[0]
        if input_text is not None:
            self.stdin.setdefault(name, []).append(input_text)
        if name == "meta-backpressure.sh":
            return self.payload, "", 0
        if name == "meta-set.sh":
            if self.meta_set_rc == 0:
                self.store[argv[2]] = MY._coerce_set_value(argv[3], False)
            return "", self.meta_set_err, self.meta_set_rc
        if name == "meta-read.sh":
            if self.readback is not None:
                return self.readback, "", self.readback_rc
            return json.dumps(self.store.get(argv[3])), "", self.readback_rc
        if name == "evolution-log-append.sh":
            return "", "", self.evo_rc
        return "", "", 0

    def named(self, name):
        return [c for c in self.calls if c[0] == name]


def _run(monkeypatch, fake):
    monkeypatch.setattr(MOD, "_run", fake)
    return MOD.cmd_backpressure(argparse.Namespace(learning_value=0.5))


# ── (B) structured values round-trip through meta-set's own parser ───────────

def test_dict_rollback_arg_round_trips_to_the_same_dict(monkeypatch):
    fake = _Fake([_action()])
    result = _run(monkeypatch, fake)
    arg = fake.named("meta-set.sh")[0][3]
    assert json.loads(arg) == PRIOR
    # The production coerce + type guard ACCEPT it and yield the same dict ...
    value = MY._coerce_set_value(arg, False)
    MY._assert_type_preserved(FAILED, value, False, FIELD)
    assert value == PRIOR
    assert result["rollbacks_applied"] == [FIELD]
    assert result["rollbacks_failed"] == []
    assert "rollback_failed" not in result["flags"]


def test_old_repr_encoding_is_what_type_destruction_refused():
    # Positive control: the pre-fix str() arg is a repr, which the same
    # production pair refuses. Proves the test above can see defect (B).
    value = MY._coerce_set_value(str(PRIOR), False)
    assert isinstance(value, str)
    try:
        MY._assert_type_preserved(FAILED, value, False, FIELD)
    except MY._MetaYamlError as e:
        assert e.code == "type_destruction"
    else:
        raise AssertionError("str(dict) was accepted; the control no longer bites")


def test_list_and_bool_rollbacks_are_json_encoded(monkeypatch):
    fake = _Fake([_action(field="modes", rollback_to=["a", "b"], failed_value=["a"]),
                  _action(field="enabled", rollback_to=True, failed_value=False)])
    result = _run(monkeypatch, fake)
    args = {c[2]: c[3] for c in fake.named("meta-set.sh")}
    assert MY._coerce_set_value(args["modes"], False) == ["a", "b"]
    # str(True) is "True", which the parser keeps as a STRING.
    assert MY._coerce_set_value(args["enabled"], False) is True
    assert result["rollbacks_applied"] == ["modes", "enabled"]


def test_scalar_rollbacks_keep_their_encoding_and_skip_the_readback(monkeypatch):
    fake = _Fake([_action(field="weights.priority", rollback_to=0.3, failed_value=0.5),
                  _action(field="note", rollback_to="2026-07-28", failed_value="x")])
    result = _run(monkeypatch, fake)
    args = {c[2]: c[3] for c in fake.named("meta-set.sh")}
    assert args == {"weights.priority": "0.3", "note": "2026-07-28"}
    assert fake.named("meta-read.sh") == []
    assert result["rollbacks_applied"] == ["weights.priority", "note"]


# ── (A) a null prior is zeroed only when the field is numeric ────────────────

def test_null_prior_dict_is_skipped_visibly_and_numeric_keeps_the_floor(monkeypatch):
    fake = _Fake([_action(field="data_authoring", rollback_to=None,
                          failed_value={"cross_references": "rb-1"}),
                  _action(field="weights.opportunity_boost", rollback_to=None,
                          failed_value=0.5)])
    result = _run(monkeypatch, fake)
    sets = fake.named("meta-set.sh")
    # Exactly one write, and it is the numeric one ( floor kept).
    assert [(c[2], c[3]) for c in sets] == [("weights.opportunity_boost", "0.0")]
    assert not any(c[2] == "data_authoring" for c in sets)
    assert [s["field"] for s in result["rollbacks_skipped"]] == ["data_authoring"]
    assert "rollback_skipped" in result["flags"]
    assert result["rollbacks_applied"] == ["weights.opportunity_boost"]


def test_scalar_prior_over_a_dict_is_skipped_not_zeroed(monkeypatch):
    # mc-374/375: roi_history[81]/[82] rolled back to 0 over a dict. meta-set's
    # type guard allows dict -> number, so only the applier can refuse it. The
    # reverse direction (a dict prior over a zeroed 0.0) must still land.
    fake = _Fake([_action(field="roi_history[81]", rollback_to=0, failed_value={"roi": 1.5}),
                  _action(field="data_authoring", rollback_to={"a": 1}, failed_value=0.0)])
    result = _run(monkeypatch, fake)
    sets = fake.named("meta-set.sh")
    assert [c[2] for c in sets] == ["data_authoring"]
    assert MY._coerce_set_value(sets[0][3], False) == {"a": 1}
    assert [s["field"] for s in result["rollbacks_skipped"]] == ["roi_history[81]"]
    assert result["rollbacks_applied"] == ["data_authoring"]


def test_null_prior_on_string_and_bool_fields_is_skipped(monkeypatch):
    # bool is an int subclass; 0.0 over True would still be a type change.
    fake = _Fake([_action(field="roi_history_note", rollback_to=None, failed_value="note"),
                  _action(field="enabled", rollback_to="None", failed_value=True)])
    result = _run(monkeypatch, fake)
    assert fake.named("meta-set.sh") == []
    assert [s["field"] for s in result["rollbacks_skipped"]] == ["roi_history_note", "enabled"]
    assert result["rollbacks_applied"] == []


# ── (B) a write that did not land is recorded, never counted ─────────────────

def test_meta_set_refusal_is_recorded_as_rollback_failed(monkeypatch):
    fake = _Fake([_action()], meta_set_rc=1,
                 meta_set_err="type_destruction: refusing to replace a dict")
    result = _run(monkeypatch, fake)
    assert "rollback_failed" in result["flags"]
    assert FIELD not in result["rollbacks_applied"]
    assert result["rollbacks_failed"][0]["field"] == FIELD
    assert "type_destruction" in result["rollbacks_failed"][0]["error"]
    # No success event for a revert that did not happen.
    assert fake.named("evolution-log-append.sh") == []
    # A remedy that did not land is a hard failure: iteration-close WARNs.
    assert MOD._has_hard_failure(result["flags"])
    assert MOD._has_hard_failure(["backpressure:rollback_failed"])


def test_readback_mismatch_is_a_failure_not_an_apply(monkeypatch):
    # rc=0 from meta-set, but the stored value is a string (what a mangled
    # positional arg produces, guard-5633).
    fake = _Fake([_action()], readback=json.dumps(str(PRIOR)))
    result = _run(monkeypatch, fake)
    assert result["rollbacks_applied"] == []
    assert "readback" in result["rollbacks_failed"][0]["error"]
    assert fake.named("evolution-log-append.sh") == []


def test_unreadable_readback_is_a_failure(monkeypatch):
    fake = _Fake([_action()], readback='{"error": "field_not_found"}', readback_rc=1)
    result = _run(monkeypatch, fake)
    assert result["rollbacks_applied"] == []
    assert "rc=1" in result["rollbacks_failed"][0]["error"]


# ── (C) the audit event carries a date the endpoint accepts ──────────────────

def test_evolution_event_has_a_valid_date_and_required_fields(monkeypatch):
    fake = _Fake([_action()])
    _run(monkeypatch, fake)
    events = [json.loads(s) for s in fake.stdin["evolution-log-append.sh"]]
    assert len(events) == 1
    assert DATE_RE.match(events[0]["date"])
    assert {"date", "event", "details"} <= set(events[0])
    assert events[0]["event"] == "backpressure_rollback"


def test_evolution_event_lands_through_the_production_endpoint(monkeypatch, tmp_path):
    # The regex above transcribes the endpoint's check. Drive the handler itself
    # and read the row back, because a 2xx is not delivery (guard-7324). This
    # proves the HANDLER only; evolution-log-append.sh posts its stdin to this
    # route verbatim, and that wiring is a separate claim (guard-2751).
    from types import SimpleNamespace
    from mind_api.src.endpoints import aspirations_write as AW

    def post(body):
        return AW.evolution_append(SimpleNamespace(
            paths=SimpleNamespace(meta=tmp_path), body=body.encode("utf-8"),
            headers={"x-mind-agent": "alpha"}))

    fake = _Fake([_action()])
    _run(monkeypatch, fake)
    body = fake.stdin["evolution-log-append.sh"][0]
    resp = post(body)
    assert resp.status == 200, resp.body
    log = (tmp_path / "evolution-log.jsonl").read_text(encoding="utf-8")
    assert [json.loads(line) for line in log.splitlines()] == [json.loads(body)]
    # Positive control: the same handler refuses the pre-fix event (date "").
    assert post(json.dumps(dict(json.loads(body), date=""))).status == 400


def test_evolution_log_failure_is_informational(monkeypatch):
    fake = _Fake([_action()], evo_rc=1)
    result = _run(monkeypatch, fake)
    assert result["rollbacks_applied"] == [FIELD]      # the revert DID land
    assert result["evolution_log_failed"] == [FIELD]
    assert "evolution_log_failed" in result["flags"]
    assert not MOD._has_hard_failure(result["flags"])
    assert not MOD._has_hard_failure(["backpressure:rollback_skipped"])
