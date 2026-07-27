"""Verdict-logic tests for _claim_liveness.py (, guard-1151 Layer B).

The wrapper claim-liveness-check.sh maps: LIVE/INDETERMINATE -> exit 0
(fail-open), STALE -> exit 1. These tests pin the pure classification —
the harm asymmetry is encoded here: any unreadable/missing input MUST be
INDETERMINATE (never STALE), because a wrong STALE would refuse a
legitimate daemon restart.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from _claim_liveness import verdict  # noqa: E402


def _asp(goals):
    return json.dumps({"id": "asp-115", "goals": goals})


def _goal(**kw):
    base = {"id": "g-115-1", "status": "in-progress", "claimed_by": "alpha"}
    base.update(kw)
    return base


def test_live_claim():
    kind, reason = verdict(_asp([_goal()]), "alpha", "g-115-1")
    assert kind == "LIVE"
    assert "alpha" in reason


def test_stale_when_completed():
    kind, reason = verdict(_asp([_goal(status="completed")]), "alpha", "g-115-1")
    assert kind == "STALE"
    assert "completed" in reason


def test_stale_when_released_to_pending():
    kind, _ = verdict(_asp([_goal(status="pending", claimed_by=None)]),
                      "alpha", "g-115-1")
    assert kind == "STALE"


def test_stale_when_taken_over():
    # status stays in-progress but another agent owns the claim now.
    kind, reason = verdict(_asp([_goal(claimed_by="bravo")]), "alpha", "g-115-1")
    assert kind == "STALE"
    assert "bravo" in reason


def test_indeterminate_on_unparseable():
    kind, _ = verdict("not json at all", "alpha", "g-115-1")
    assert kind == "INDETERMINATE"


def test_indeterminate_on_goal_missing():
    kind, _ = verdict(_asp([_goal(id="g-115-2")]), "alpha", "g-115-1")
    assert kind == "INDETERMINATE"


def test_bare_goal_record_accepted():
    kind, _ = verdict(json.dumps(_goal()), "alpha", "g-115-1")
    assert kind == "LIVE"


def test_list_payload_accepted():
    kind, _ = verdict(json.dumps([_goal()]), "alpha", "g-115-1")
    assert kind == "LIVE"


def test_aspiration_wrapper_shape_accepted():
    payload = json.dumps({"aspiration": {"id": "asp-115", "goals": [_goal()]}})
    kind, _ = verdict(payload, "alpha", "g-115-1")
    assert kind == "LIVE"


def test_goal_id_key_variant_accepted():
    g = _goal()
    g["goal_id"] = g.pop("id")
    kind, _ = verdict(_asp([g]), "alpha", "g-115-1")
    assert kind == "LIVE"
