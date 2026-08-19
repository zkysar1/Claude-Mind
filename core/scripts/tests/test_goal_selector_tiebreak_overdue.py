"""Pin the final-ranking tiebreak: among EQUAL scores, the most-overdue recurring
row sorts first (g-306-284 stall, 2026-08-17).

apply_substantive_demotion writes `score = cap` onto every non-exempt recurring
row above the cap, so the tie it produces is all-recurring and carries no
ordering information. Under the old key `(-score, aspiration_id, goal_id)` that
cluster was ordered by aspiration-id STRING, so a reducer-only 8h drain lane in
asp-306 that had the HIGHEST pre-demotion score of ~1150 candidates in four
consecutive selector runs sorted LAST of the tied cluster every time — behind
rows 0.8x overdue while it stood at 1.7x — and went 22h unpicked while three
dependents waited on the merge it performs.

The fix is candidate_sort_key: `(-score, -overdue_ratio, aspiration_id,
goal_id)`. Each test below is RED under the old key by construction (the old
key is asserted as the CONTROL in test_control_old_key_orders_by_asp_string).

Fixture idiom mirrors test_goal_selector_drain_lane.py: pin MIND_AGENT around
import so module-level agent resolution is deterministic.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "alpha")

gs = importlib.import_module("goal-selector")

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT


DEMOTION_CFG = {
    "substantive_demotion_enabled": True,
    "substantive_demotion_margin": 0.5,
    "substantive_demotion_floor": 5.0,
    "substantive_demotion_overdue_exempt_ratio": 5.0,
    "substantive_demotion_short_interval_hours": 6.0,
    "substantive_demotion_short_interval_exempt_ratio": 1.0,
}


def row(gid, score, ratio=0.0, interval=24.0, recurring=True, asp="asp-115",
        executable=True):
    return {
        "goal_id": gid, "aspiration_id": asp, "score": score,
        "recurring": recurring, "recurring_overdue_ratio": ratio,
        "recurring_interval_hours": interval,
        "raw": {"agent_executable": 2 if executable else 0}, "breakdown": {},
    }


OLD_KEY = lambda x: (-x["score"], x["aspiration_id"], x["goal_id"])  # noqa: E731


def _measured_cluster():
    """The 2026-08-17 snapshot, shape-faithful: one substantive leader, four
    recurring rows above the cap with the drain lane (asp-306) the STALEST-BY-
    INTERVAL and highest-scoring, but the last aspiration id."""
    return [
        row("g-350-254", 14.16, recurring=False),
        row("g-115-16", 14.53, ratio=0.814, interval=24.0),
        row("g-115-2687", 13.70, ratio=2.243, interval=24.0),
        row("g-115-708", 14.39, ratio=4.410, interval=10.67),
        row("g-306-284", 15.75, ratio=1.726, interval=8.0, asp="asp-306"),
    ]


def test_control_old_key_orders_by_asp_string():
    """CONTROL: reproduce the defect. After demotion the four recurring rows tie
    at cap 13.66 and the OLD key puts asp-306 last, behind a 0.8x-overdue row."""
    scored = gs.apply_substantive_demotion(_measured_cluster(), DEMOTION_CFG)
    tied = [s for s in scored if s["recurring"]]
    assert {s["score"] for s in tied} == {13.66}, "precondition: all four demoted to cap"
    scored.sort(key=OLD_KEY)
    assert [s["goal_id"] for s in scored][-1] == "g-306-284"
    assert [s["goal_id"] for s in scored][1] == "g-115-16"


def test_tie_orders_most_overdue_first():
    scored = gs.apply_substantive_demotion(_measured_cluster(), DEMOTION_CFG)
    scored.sort(key=gs.candidate_sort_key)
    ids = [s["goal_id"] for s in scored]
    # Substantive leader still first: the demotion margin is untouched.
    assert ids[0] == "g-350-254"
    # Then the cluster by overdue ratio DESC: 4.41, 2.24, 1.73, 0.81.
    assert ids[1:] == ["g-115-708", "g-115-2687", "g-306-284", "g-115-16"]


def test_asp_string_no_longer_decides_a_recurring_tie():
    """Two rows at the same score: the later aspiration id wins when it is the
    staler one. This is exactly the row the old key penalised."""
    scored = [
        row("g-115-16", 13.66, ratio=0.814),
        row("g-306-284", 13.66, ratio=1.726, interval=8.0, asp="asp-306"),
    ]
    scored.sort(key=gs.candidate_sort_key)
    assert [s["goal_id"] for s in scored] == ["g-306-284", "g-115-16"]
    scored.sort(key=OLD_KEY)
    assert [s["goal_id"] for s in scored] == ["g-115-16", "g-306-284"]


def test_non_recurring_ties_are_byte_identical_to_old_key():
    """Non-recurring rows carry ratio 0.0, so their order under the new key
    equals the old key's on every tie — no behaviour change outside clusters
    that include a recurring row."""
    scored = [
        row("g-326-370", 13.37, recurring=False, asp="asp-326"),
        row("g-115-6469", 13.37, recurring=False),
        row("g-001-02", 13.37, recurring=False, asp="asp-001"),
        row("g-115-6470", 13.37, recurring=False),
        row("g-326-368", 13.53, recurring=False, asp="asp-326"),
    ]
    a = sorted(scored, key=gs.candidate_sort_key)
    b = sorted(scored, key=OLD_KEY)
    assert [s["goal_id"] for s in a] == [s["goal_id"] for s in b]
    assert [s["goal_id"] for s in a][0] == "g-326-368"


def test_equal_ratio_falls_through_to_asp_then_goal_id():
    """Determinism: equal score AND equal ratio still resolves by asp id then
    goal id (string order, matching the pre-existing convention), so a tie can
    never reorder run-to-run."""
    scored = [
        row("g-115-9", 9.0, ratio=7.0),
        row("g-115-2", 9.0, ratio=7.0),
        row("g-001-5", 9.0, ratio=7.0, asp="asp-001"),
    ]
    scored.sort(key=gs.candidate_sort_key)
    assert [s["goal_id"] for s in scored] == ["g-001-5", "g-115-2", "g-115-9"]


def test_missing_or_null_ratio_is_treated_as_zero():
    """A row without the field (or with None) must not raise and must sort as
    ratio 0.0 — the shape non-recurring rows have."""
    a = {"goal_id": "g-a", "aspiration_id": "asp-115", "score": 10.0}
    b = {"goal_id": "g-b", "aspiration_id": "asp-115", "score": 10.0,
         "recurring_overdue_ratio": None}
    c = row("g-c", 10.0, ratio=0.5)
    out = sorted([a, b, c], key=gs.candidate_sort_key)
    assert [s["goal_id"] for s in out] == ["g-c", "g-a", "g-b"]


def test_cmd_select_uses_candidate_sort_key():
    """Source pin: the live ranking must go through candidate_sort_key, not a
    re-typed inline lambda that could drift back to the asp-string tiebreak."""
    src = Path(gs.__file__).read_text(encoding="utf-8")
    assert "scored.sort(key=candidate_sort_key)" in src
    assert 'scored.sort(key=lambda x: (-x["score"], x["aspiration_id"], x["goal_id"]))' not in src
