"""The reflection queue must split by resolved_by ownership ().

WHY THIS TEST EXISTS. `_reflectable.count_reflectable` had no owner term, so the
iteration-close nudge and review-hypotheses Mode 2 counted every reflectable
record while guard-5623 forbade touching any record a LIVE other agent resolved.
Following the instrument raced another agent's ABC chain onto one record (the
loser is silent); following the guardrail left the nudge firing every close with
nothing the agent was allowed to do. These tests pin the predicate that settles
it, in the instrument rather than in a guardrail (guard-1984: a guardrail cannot
outvote the instrument it guards).

THE HALF THAT PRODUCES WORK IS TESTED TOO. An ownership fix is naturally written
as "filter OUT what I do not own", and measured (echo, cc-03, 2026-09-15) that
half alone would have left the real debt untouched: an un-split count of 13 was
read as "all someone else's, abstain" for at least two iterations while the
reading agent owned three CORRECTED records inside that same queue. So
`test_mine_is_named_by_id` asserts the agent's OWN records come back BY ID, not
merely that others' are excluded.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _reflectable as R  # noqa: E402

NOW = datetime(2026, 9, 17, 12, 0, 0)
FRESH = (NOW - timedelta(hours=2)).isoformat(timespec="seconds")
STRANDED = (NOW - timedelta(hours=100)).isoformat(timespec="seconds")


def _rec(rid, owner, outcome="CONFIRMED", resolved_at=FRESH):
    r = {"id": rid, "outcome": outcome, "resolved_at": resolved_at}
    if owner is not None:
        r["resolved_by"] = owner
    return r


# ── The four cases outcome 3 names ──────────────────────────────────────────

def test_self_owned_is_mine():
    assert R.ownership_of(_rec("h1", "bravo"), "bravo", {}) == "mine"


def test_live_other_agent_is_held():
    """guard-5623: abstain on a live owner. This is the race the goal names."""
    v = R.ownership_of(_rec("h1", "alpha"), "bravo", {"alpha": "alive"}, now=NOW)
    assert v == "held"


def test_dormant_owner_is_reclaimable():
    assert R.ownership_of(_rec("h1", "alpha"), "bravo",
                          {"alpha": "dormant"}, now=NOW) == "reclaimable"


def test_retired_owner_is_reclaimable():
    assert R.ownership_of(_rec("h1", "alpha"), "bravo",
                          {"alpha": "retired"}, now=NOW) == "reclaimable"


def test_missing_resolved_by_is_unowned_and_actionable():
    """THE STATED RULE for a record with no resolved_by: nobody can be racing a
    record nobody resolved, so it is actionable rather than frozen. The live
    census found exactly one such record; leaving it `held` would strand it
    permanently, since no owner exists whose liveness could ever go dormant."""
    assert R.ownership_of(_rec("h1", None), "bravo", {}) == "unowned"
    assert "unowned" in R.OWNERSHIP_ACTIONABLE


# ── The conservative direction on unknown liveness ──────────────────────────

def test_unknown_liveness_abstains():
    """guard-5623 allows dormant/retired/unknown-WITH-CORROBORATION. This module
    cannot see corroboration, so an absent or unrecognised verdict must abstain:
    a delayed reflection is cheaper than two conflicting ABC chains."""
    assert R.ownership_of(_rec("h1", "zeta"), "bravo", {}, now=NOW) == "held"
    assert R.ownership_of(_rec("h1", "zeta"), "bravo",
                          {"zeta": "banana"}, now=NOW) == "held"


def test_self_wins_before_any_liveness_lookup():
    """guard-6259: never judge your own liveness. Even a (nonsensical) dormant
    verdict for self must not change the answer -- self short-circuits first."""
    assert R.ownership_of(_rec("h1", "bravo"), "bravo",
                          {"bravo": "dormant"}, now=NOW) == "mine"


# ── The stranded-owner rule (outcome 4) ─────────────────────────────────────

def test_live_owner_past_threshold_is_reclaimable():
    v = R.ownership_of(_rec("h1", "alpha", resolved_at=STRANDED), "bravo",
                       {"alpha": "alive"}, now=NOW)
    assert v == "reclaimable"


def test_live_owner_inside_threshold_stays_held():
    v = R.ownership_of(_rec("h1", "alpha", resolved_at=FRESH), "bravo",
                       {"alpha": "alive"}, now=NOW)
    assert v == "held"


def test_unparseable_resolved_at_stays_held():
    """An unreadable age must not free a live owner's record -- the age rule can
    only ADD reclaims, never remove the guard-5623 abstention."""
    v = R.ownership_of(_rec("h1", "alpha", resolved_at="not-a-date"), "bravo",
                       {"alpha": "alive"}, now=NOW)
    assert v == "held"


def test_threshold_is_one_owner_cadence_plus_margin():
    """72h = the owner's own learn cadence (, 60h) + 12h margin. Pinned
    so a future change to the number is a deliberate edit with a reason, not a
    drift."""
    assert R.STRANDED_HOURS == 72


# ── split_by_owner: the reporting shape both callers consume ────────────────

def test_mine_is_named_by_id():
    """The work-producing half. A caller must be able to say WHICH records are
    its own, not just how many exist -- an un-split total is read as 'someone
    else's' and never contradicted."""
    recs = [_rec("mine-1", "bravo"), _rec("mine-2", "bravo"),
            _rec("theirs", "alpha")]
    out = R.split_by_owner(recs, "bravo", {"alpha": "alive"}, now=NOW)
    assert out["mine"] == ["mine-1", "mine-2"]
    assert out["held"] == ["theirs"]
    assert out["actionable"] == 2


def test_held_by_tallies_who_we_are_waiting_on():
    recs = [_rec("a", "alpha"), _rec("b", "alpha"), _rec("c", "zeta")]
    out = R.split_by_owner(recs, "bravo",
                           {"alpha": "alive", "zeta": "alive"}, now=NOW)
    assert out["held_by"] == {"alpha": 2, "zeta": 1}
    assert out["actionable"] == 0


def test_non_reflectable_records_are_dropped_first():
    """The owner split composes with the existing outcome split -- it must not
    resurrect UNRESOLVABLE/EXPIRED records the g-115-6173 filter removed."""
    recs = [_rec("ok", "bravo"),
            _rec("dead", "bravo", outcome="UNRESOLVABLE"),
            _rec("gone", "bravo", outcome="EXPIRED"),
            {"id": "no-outcome", "resolved_by": "bravo"}]
    out = R.split_by_owner(recs, "bravo", {}, now=NOW)
    assert out["reflectable_total"] == 1
    assert out["mine"] == ["ok"]


def test_actionable_plus_held_equals_reflectable_total():
    """No record may fall out of the split -- a silently-dropped record is the
    failure mode this whole module exists to make impossible."""
    recs = [_rec("m", "bravo"), _rec("h", "alpha"), _rec("u", None),
            _rec("d", "zeta"), _rec("x", "alpha", outcome="EXPIRED")]
    out = R.split_by_owner(recs, "bravo",
                           {"alpha": "alive", "zeta": "dormant"}, now=NOW)
    assert out["actionable"] + len(out["held"]) == out["reflectable_total"] == 4


def test_owners_of_returns_distinct_owners_only():
    """The caller probes liveness once per OWNER, not once per record -- the
    queue is dominated by a single owner, so this is ~1 probe, not ~13."""
    recs = [_rec("a", "bravo"), _rec("b", "bravo"), _rec("c", "alpha"),
            _rec("d", None), _rec("e", "alpha", outcome="EXPIRED")]
    assert R.owners_of(recs) == ["bravo", "alpha"]


def test_malformed_input_is_held_not_crashed():
    assert R.ownership_of(None, "bravo", {}) == "held"
    assert R.ownership_of("nope", "bravo", {}) == "held"
    assert R.split_by_owner(None, "bravo", {})["actionable"] == 0
