"""goal-selector: an orphaned SID claim is a claim (, guard-4434).

THE DEFECT. `collect_candidates`' ownership filter had exactly two forms of
"someone else", and BOTH were gated on `bool(claimed_by)`:

    other_mind   = bool(claimed) and claimed != AGENT_NAME
    sibling_body = bool(claimed) and claimed == AGENT_NAME and ...

So a record with `claimed_by = None` and a live `claimed_by_sid` was invisible
to both and got offered as UNCLAIMED while a real Body held it. That record
shape is not a legacy artifact -- own-cloud fenced-PUT reconcile damage
(rb-3636 sub-mechanism B / class g-115-2306) nulls `claimed_by` while siblings
survive; 7 such records were live on 2026-08-22 (cc-02).

WHY THIS IS A UNIT TEST AND NOT A LIVE PROBE. The one live specimen carrying a
non-null sid (g-360-01) was 46h old, so the expiry ladder ALREADY let it
through -- running the real selector shows it offered both before and after the
fix, and that says nothing either way. The hazard window is a FRESH orphaned
claim, which no live record currently exhibits. The fresh case has to be
constructed, and constructing it is the only way to show the fix discriminates
(rb-5828: prove a fix by re-introducing the defect, not by observing green).

The aged case is pinned too, because "suppress an orphaned claim" must NOT mean
"freeze the goal forever" -- a dead Body's claim has to age out and become
reclaimable exactly like any other.
"""
import importlib.util
import sys
from pathlib import Path

import pytest  # noqa: F401 -- harness parity with sibling suites

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "goal_selector", _SCRIPTS / "goal-selector.py")
gs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gs)

FOREIGN_SID = "3f5b2e42-ef10-4819-a9b6-9c69ba539bd7"
MY_SID = "caeb1579-54b2-4fdc-b99f-fd23b4ebbba2"


def _asp(goal):
    return [{"id": "asp-999", "status": "active", "goals": [goal]}]


def _goal(**kw):
    g = {
        "id": "g-999-01",
        "title": "orphaned-sid claim specimen",
        "status": "pending",
        "priority": "MEDIUM",
        "category": "multi-agent-coordination",
        "participants": ["agent"],
        "created_at": gs.now_iso() if hasattr(gs, "now_iso") else "2026-08-22T00:00:00",
    }
    g.update(kw)
    return g


def _ids(goal, *, body_sid, timeout=4):
    """Run the real filter and return the surviving goal ids.

    collect_candidates emits {aspiration, goal, source} -- the flat `goal_id`
    key appears only further down the pipeline, so read through `goal`. Reading
    the wrong key here returns [None] for every INCLUDED goal, which reads as a
    failure of the fix rather than of the harness (it did, first run).
    """
    prev = gs.BODY_SID
    gs.BODY_SID = body_sid
    try:
        out = gs.collect_candidates(_asp(goal), source="world",
                                    claim_timeout_hours=timeout)
    finally:
        gs.BODY_SID = prev
    return [c["goal"]["id"] for c in out]


def _fresh():
    import datetime as dt
    return (dt.datetime.now() - dt.timedelta(minutes=5)).isoformat(timespec="seconds")


def _stale():
    import datetime as dt
    return (dt.datetime.now() - dt.timedelta(hours=46)).isoformat(timespec="seconds")


# --- the defect, and the discrimination -------------------------------------

def test_fresh_orphaned_sid_claim_is_suppressed():
    """THE FIX. claimed_by is null, but a foreign sid holds it and the claim is
    fresh -- must NOT be offered. Pre-fix this record was returned, because both
    ownership branches required a truthy claimed_by."""
    g = _goal(claimed_by=None, claimed_by_sid=FOREIGN_SID, claimed_at=_fresh())
    assert _ids(g, body_sid=MY_SID) == []


def test_fresh_orphaned_sid_claim_held_by_ME_is_still_offered():
    """Discrimination: my OWN Body's sid is not someone else. Suppressing this
    would mean a Body could never re-enter its own claimed goal."""
    g = _goal(claimed_by=None, claimed_by_sid=MY_SID, claimed_at=_fresh())
    assert _ids(g, body_sid=MY_SID) == ["g-999-01"]


def test_aged_orphaned_sid_claim_ages_out_and_is_reclaimable():
    """Suppression must not become a permanent freeze -- the orphaned claim
    rides the SAME expiry ladder as every other claim. This is also why the
    live g-360-01 (46h) is offered today and cannot serve as evidence."""
    g = _goal(claimed_by=None, claimed_by_sid=FOREIGN_SID, claimed_at=_stale())
    assert _ids(g, body_sid=MY_SID) == ["g-999-01"]


def test_null_sid_beside_null_name_is_genuinely_unclaimed():
    """The other 6 damaged records' shape: no sid at all. Damaged, but nobody
    holds it -- it must stay selectable or real work would be stranded."""
    g = _goal(claimed_by=None, claimed_by_sid=None, claimed_at=_fresh())
    assert _ids(g, body_sid=MY_SID) == ["g-999-01"]

    g2 = _goal(claimed_by=None, claimed_at=_fresh())  # key absent entirely
    assert _ids(g2, body_sid=MY_SID) == ["g-999-01"]


def test_fails_closed_when_body_sid_is_unset():
    """With no BODY_SID we cannot prove the sid is ours, so it reads foreign.
    Fail-closed is the safe direction for a claim check, and the cost is bounded:
    the population is records with a null NAME and a live SID (1 of 2239 when
    measured)."""
    g = _goal(claimed_by=None, claimed_by_sid=FOREIGN_SID, claimed_at=_fresh())
    assert _ids(g, body_sid="") == []


# --- the pre-existing behaviour must be untouched ---------------------------

def test_normal_foreign_claim_still_suppressed():
    g = _goal(claimed_by="alpha", claimed_by_sid=FOREIGN_SID, claimed_at=_fresh())
    assert _ids(g, body_sid=MY_SID) == []


def test_unclaimed_goal_still_offered():
    g = _goal()
    assert _ids(g, body_sid=MY_SID) == ["g-999-01"]
