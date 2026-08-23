"""Regression tests for claim-integrity-check.py ( outcome 3).

Pins two things, and the SECOND is the one that matters.

1. DETECTION: a goal whose ``claimed_by`` key is PRESENT with value None is a
   finding; a goal where the key is ABSENT is not. That distinction is the
   entire check -- every application clear site pops the key, so key-present-None
   is a shape no code in this tree can produce (rb-3636 sub-mechanism B).

2. THE FALSE-ZERO CONTROL CAN ACTUALLY FIRE. A check that reports "0 damaged"
   looks identical whether the store is healthy or the check was pointed at a
   source carrying no claim fields at all. That is not hypothetical: a version
   built on aspirations-compact-summary.json would have read clean forever
   (16 keys / 194 goals / claimed_by on ZERO of them, measured cc-02
   2026-08-22). So the BLIND branch is asserted to FIRE on a zero-live-claim
   store, not merely observed staying quiet on a healthy one -- rb-5828: a green
   check nobody has proven can go red is not evidence.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest  # noqa: F401 -- harness parity with sibling suites

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "claim_integrity_check", _SCRIPTS / "claim-integrity-check.py")
cic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cic)


def _write_store(tmp_path, goals):
    """One aspiration wrapping the given goal dicts, as JSONL."""
    p = tmp_path / "aspirations.jsonl"
    p.write_text(json.dumps({"id": "asp-999", "goals": goals}) + "\n",
                 encoding="utf-8")
    return p


def _scan(tmp_path, goals):
    import datetime as dt
    presence, findings = {}, []
    scanned = cic._scan_store(_write_store(tmp_path, goals), "world",
                              dt.datetime.now(), presence, findings)
    return scanned, presence, findings


# --- the control, asserted to FIRE -----------------------------------------

def test_blind_fires_when_no_live_claim_carries_a_value():
    """The compact-projection case. Zero findings AND zero live claims is the
    byte-identical twin of a healthy store; it must NOT read clean."""
    assert cic.verdict_for(present_value=0, findings=[]) == "BLIND"


def test_blind_outranks_findings():
    """A source that drops claim fields cannot be trusted to have produced a
    meaningful finding set either -- BLIND wins over 'damaged'."""
    assert cic.verdict_for(present_value=0, findings=[{"goal_id": "g-1"}]) == "BLIND"


def test_clean_and_damaged_require_live_claims_present():
    assert cic.verdict_for(present_value=3, findings=[]) == "clean"
    assert cic.verdict_for(present_value=3, findings=[{"goal_id": "g-1"}]) == "damaged"


def test_projection_shaped_store_is_blind_not_clean(tmp_path):
    """End-to-end shape of the trap: goals carrying no claim key at all."""
    scanned, presence, findings = _scan(tmp_path, [
        {"id": "g-1", "status": "pending", "title": "a"},
        {"id": "g-2", "status": "pending", "title": "b"},
    ])
    assert scanned == 2
    assert findings == []
    assert cic.verdict_for(presence.get("value", 0), findings) == "BLIND"


# --- detection --------------------------------------------------------------

def test_key_present_none_is_a_finding_key_absent_is_not(tmp_path):
    scanned, presence, findings = _scan(tmp_path, [
        {"id": "g-absent", "status": "pending"},
        {"id": "g-null", "status": "pending", "claimed_by": None},
        {"id": "g-live", "status": "pending", "claimed_by": "alpha"},
    ])
    assert scanned == 3
    assert presence == {"absent": 1, "null": 1, "value": 1}
    assert [f["goal_id"] for f in findings] == ["g-null"]
    # A live claim present means the control correctly stays silent.
    assert cic.verdict_for(presence["value"], findings) == "damaged"


def test_partial_field_survival_marks_reconcile_damage(tmp_path):
    """claimed_at surviving beside a null claimed_by cannot come from a pop --
    every clear site moves the pair as a unit."""
    _, _, findings = _scan(tmp_path, [
        {"id": "g-live", "status": "pending", "claimed_by": "alpha"},
        {"id": "g-damaged", "status": "pending", "claimed_by": None,
         "claimed_at": "2026-08-19T18:19:40", "started": "2026-08-19T18:19:40",
         "executed_by": "echo"},
    ])
    f = next(x for x in findings if x["goal_id"] == "g-damaged")
    assert f["reconcile_damage"] is True
    assert set(f["surviving_siblings"]) == {"claimed_at", "started", "executed_by"}
    assert f["attributed_to"] == "echo"


def test_null_without_survivors_is_reported_but_not_damage(tmp_path):
    """Weaker evidence ('s real shape) -- still reported, not
    silently downgraded, but distinguishable from the unforgeable case."""
    _, _, findings = _scan(tmp_path, [
        {"id": "g-live", "status": "pending", "claimed_by": "alpha"},
        {"id": "g-bare", "status": "pending", "claimed_by": None},
    ])
    f = next(x for x in findings if x["goal_id"] == "g-bare")
    assert f["reconcile_damage"] is False
    assert f["surviving_siblings"] == {}


def test_terminal_goals_are_excluded(tmp_path):
    """A damaged claim on a completed goal cannot cause duplicate execution."""
    scanned, _, findings = _scan(tmp_path, [
        {"id": "g-done", "status": "completed", "claimed_by": None,
         "claimed_at": "2026-08-19T18:19:40"},
        {"id": "g-live", "status": "pending", "claimed_by": "alpha"},
    ])
    assert scanned == 1
    assert findings == []


def test_blocked_status_is_in_scope(tmp_path):
    """Non-terminal is wider than 'pending' -- a hand-census restricted to
    pending missed g-350-207 (blocked) on the live store. guard-1802: a
    detector's predicate must not be narrower than the population."""
    _, _, findings = _scan(tmp_path, [
        {"id": "g-live", "status": "pending", "claimed_by": "alpha"},
        {"id": "g-blocked", "status": "blocked", "claimed_by": None,
         "claimed_at": "2026-08-19T18:19:40"},
    ])
    assert [f["goal_id"] for f in findings] == ["g-blocked"]


def test_key_state_discriminates_all_three(tmp_path):
    assert cic._key_state({}, "claimed_by") == "absent"
    assert cic._key_state({"claimed_by": None}, "claimed_by") == "null"
    assert cic._key_state({"claimed_by": "alpha"}, "claimed_by") == "value"


# --- the WIRING, not the function ------------------------------------------

def test_claim_integrity_is_registered_in_subcmds_not_only_dispatch():
    """precheck-eval.py's own comment: cmd_run_all iterates SUBCMDS, NOT
    DISPATCH, so a DISPATCH-only entry gives the check a CLI name while it
    never fires in the precheck. A detector that never runs is
    indistinguishable from one that always returns clean, and no test of the
    check's LOGIC can see the difference (rb-5828: coverage certifies the
    function, never the wiring). Pin the call site.
    """
    spec = importlib.util.spec_from_file_location(
        "precheck_eval", _SCRIPTS / "precheck-eval.py")
    pe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pe)

    subcmd_names = [name for name, _ in pe.SUBCMDS]
    assert "claim-integrity" in subcmd_names, (
        "claim-integrity fell out of SUBCMDS — it would still answer on the "
        "CLI via DISPATCH while silently never running in run-all")
    assert "claim-integrity" in pe.DISPATCH
