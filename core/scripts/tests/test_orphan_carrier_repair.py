"""Tests for orphan_carrier_repair ().

The selector is where every safety property of this writer lives, so it is
tested as a pure function against the exact populations the 2026-09-13
fleet-wide enumeration measured. Two of these are NEGATIVE CONTROLS in the sense
guard-6558 step 3 requires: they prove the opposite divergence is NOT written.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import orphan_carrier_repair as ocr  # noqa: E402

DAY = 1440.0
THRESH = 3 * DAY


def row(agent="alpha", sid="s1", host="cc-04", age=10 * DAY,
        body_state="active", held_goal=None):
    return {"agent": agent, "sid": sid, "host": host, "age_minutes": age,
            "body_state": body_state, "held_goal": held_goal, "doc": {}}


def only_reasons(excluded, sid):
    return next(f["exclusion_reasons"] for f in excluded if f["sid"] == sid)


def test_the_canonical_orphan_is_selected():
    sel, _ = ocr.select_rows([row()], THRESH, "alpha")
    assert [f["sid"] for f in sel] == ["s1"]


def test_parked_body_is_never_written():
    """NEGATIVE CONTROL. `parked` is a LIVE but dormant Body with an hourly
    re-poll; closing it would un-finish a resumable Body -- the one direction
    that is destructive."""
    sel, exc = ocr.select_rows([row(body_state="parked")], THRESH, "alpha")
    assert sel == []
    assert any("not-a-LIVE-body_state" in r for r in only_reasons(exc, "s1"))


@pytest.mark.parametrize("closed", sorted(ocr.ws.CLOSED_BODY_STATES))
def test_an_already_closed_carrier_is_never_rewritten(closed):
    """NEGATIVE CONTROL. Rewriting a closed row is at best a no-op and at worst
    lets a later state resurrect a closed reading."""
    sel, exc = ocr.select_rows([row(body_state=closed)], THRESH, "alpha")
    assert sel == []
    assert any("not-a-LIVE-body_state" in r for r in only_reasons(exc, "s1"))


def test_the_blind_population_is_excluded_and_this_is_the_whole_added_clause():
    """The predicate as originally proposed omitted the LIVE-state clause and
    would have swept 23 of the 24 blind rows -- the conflation the prior unit
    explicitly forbade. An absent or empty body_state is not evidence of a
    stall; it is evidence of nothing."""
    for blind in (None, "", "   "):
        sel, exc = ocr.select_rows([row(body_state=blind)], THRESH, "alpha")
        assert sel == [], f"blind body_state {blind!r} must not be selected"
        assert any("not-a-LIVE-body_state" in r for r in only_reasons(exc, "s1"))


def test_a_row_holding_a_live_claim_is_excluded():
    """The measured case: bravo/be7df8fb on cc-05 was ALERTING
    (stalled_with_claim) while holding live goal g-369-310. The note that
    scoped this unit warned the predicate must not be `is_alerting` for exactly
    this row."""
    sel, exc = ocr.select_rows([row(held_goal="g-369-310")], THRESH, "alpha")
    assert sel == []
    assert "holds-live-claim" in only_reasons(exc, "s1")


def test_a_fresh_carrier_is_excluded_so_a_live_body_is_never_closed():
    sel, exc = ocr.select_rows([row(age=5.0)], THRESH, "alpha")
    assert sel == []
    assert "fresher-than-threshold" in only_reasons(exc, "s1")


def test_an_unparseable_timestamp_is_excluded_rather_than_assumed_old():
    sel, exc = ocr.select_rows([row(age=None)], THRESH, "alpha")
    assert sel == []
    assert "ts-unparseable" in only_reasons(exc, "s1")


def test_a_row_with_no_named_host_is_excluded():
    for host in (None, "", "  ", 17):
        sel, exc = ocr.select_rows([row(host=host)], THRESH, "alpha")
        assert sel == [], f"host {host!r} must not be selected"
        assert "host-not-named" in only_reasons(exc, "s1")


def test_a_peer_agents_row_is_never_written_from_this_box():
    """The ownership fence. bravo's two orphans are real and old, and this box
    still must not touch them."""
    sel, exc = ocr.select_rows([row(agent="bravo")], THRESH, "alpha")
    assert sel == []
    assert "not-the-bound-agent" in only_reasons(exc, "s1")


def test_selection_is_keyed_on_agent_and_sid_not_sid_alone():
    """MEASURED 2026-09-13: 78 carriers, 77 distinct sids -- one sid is held by
    both alpha and foxtrot on cc-08. A sid-keyed selector collapses the pair;
    this one must keep the alpha row and drop the foxtrot row."""
    shared = "3ebc753b-4acc-42d5-b94e-9d8c3bda7421"
    sel, exc = ocr.select_rows(
        [row(agent="alpha", sid=shared, host="cc-08"),
         row(agent="foxtrot", sid=shared, host="cc-08")], THRESH, "alpha")
    assert len(sel) == 1 and sel[0]["agent"] == "alpha"
    assert len(exc) == 1 and exc[0]["agent"] == "foxtrot"


def test_every_exclusion_reason_is_reported_not_just_the_first():
    """A row can fail several clauses at once, and a caller showing an exclusion
    census needs all of them or the census under-counts."""
    _, exc = ocr.select_rows(
        [row(agent="zeta", host=None, age=1.0, body_state="parked",
             held_goal="g-1-1")], THRESH, "alpha")
    reasons = only_reasons(exc, "s1")
    assert {"not-the-bound-agent", "host-not-named", "fresher-than-threshold",
            "holds-live-claim"}.issubset(set(reasons))
    assert any("not-a-LIVE-body_state" in r for r in reasons)


def test_live_body_state_inherits_the_partition_rather_than_forking_it():
    """If a future state joins CLOSED_BODY_STATES, this predicate must follow it
    automatically -- the point of routing through worker_stall's constants."""
    assert ocr.live_body_state("active") is True
    assert ocr.live_body_state(ocr.ws.PARKED_BODY_STATE) is False
    for s in ocr.ws.CLOSED_BODY_STATES:
        assert ocr.live_body_state(s) is False


def test_repair_one_preserves_ts_and_writes_only_body_state(tmp_path):
    """guard-6558's core property, exercised on real bytes: the consumer returns
    ALIVE on freshness BEFORE it reads body_state, so a repair that restamped
    `ts` would mint a phantom live Body."""
    doc = {"ts": "2026-08-27T16:47:56", "host": "cc-08", "body_state": "active",
           "agent": "alpha", "extra": {"keep": 1}}
    out = ocr.repair_one("sid-a", doc, tmp_path)
    written = json.loads((tmp_path / "body-heartbeat-sid-a.json").read_text())
    assert written["ts"] == "2026-08-27T16:47:56"
    assert written["body_state"] == "closed-stale"
    assert written["extra"] == {"keep": 1} and written["host"] == "cc-08"
    assert out["ts_preserved"] is True
    assert out["verdict"] in ("repaired", "repaired-push-failed")


def test_repair_one_does_not_mutate_the_caller_s_doc(tmp_path):
    doc = {"ts": "2026-08-27T16:47:56", "body_state": "active"}
    ocr.repair_one("sid-b", doc, tmp_path)
    assert doc["body_state"] == "active"


def test_a_repaired_carrier_classifies_benign_and_never_alive(tmp_path):
    """The end-to-end property the whole unit is for: the verdict must move from
    the alerting one to a BENIGN one, not to V_ALIVE. Runs the real classifier
    on the repaired row's own values."""
    age_minutes = 10 * DAY
    before = ocr.ws.classify_body(age_minutes, False, ocr.ws.DEFAULT_STALE_MINUTES,
                                  "active")
    after = ocr.ws.classify_body(age_minutes, False, ocr.ws.DEFAULT_STALE_MINUTES,
                                 ocr.REPAIR_STATE)
    assert ocr.ws.is_alerting(before) is True
    assert ocr.ws.is_alerting(after) is False
    assert after != ocr.ws.V_ALIVE
