"""Pins the MERGED two-queue ranking invariant ().

WHY THIS FILE EXISTS. g-115-6999 reported that `ranked_goals[0]` is not the
top-scoring goal -- measured 748 of 1014 candidates outscoring index 0, "array
is score-descending: FALSE", and an agent-queue goal at index 0 "regardless of
its score" -- and concluded the two queues are "merged into one array in a way
that is not score-ordered". That conclusion is REFUTED. `candidate_sort_key`
sorts strictly `(-score, -recurring_overdue_ratio, aspiration_id, goal_id)`;
there is no source-keyed splice anywhere in the merge.

What the report actually observed was the BOUNDED DRAIN LANE (g-115-4119,
decision g-115-4118(b)) firing: `apply_drain_lane` runs AFTER the sort,
REORDERS rather than rescores, and promotes ONE genuinely-starved recurring
goal to the head at most once per K selector invocations. Confirmed on cc-02
2026-08-20 -- `drain-lane-state.json` carried `last_pick_goal_id: g-001-10`
while that same iteration's `iteration-open.sh` reported `top: g-001-10 (9.36)`
against a score maximum of 14.16.

TWO COROLLARIES THE REPORT GOT BACKWARDS, both worth pinning because each reads
as a defect and is in fact the design:

  1. It is NOT true that claiming the score-maximum needs a deviation code
     merely because the lane fired. `write_scorer_verdict` is deliberately
     placed AFTER the lane (goal-selector.py, the comment at the lane call
     site says so verbatim: "when the lane fires, its pick IS the top pick"),
     so the sidecar the claim gate reads records the LANE pick as sanctioned.
  2. It is NOT true that two consumers disagree about which element is "top".
     `iteration-open.py` computes `top = d[0]` and the claim gate reads the
     index-0 sidecar. Both read index 0. The report's two differing numbers
     came from two SEPARATE selector invocations at different points in the
     K-cadence -- and the selector is stochastic (guard-3562: exploration_noise
     is a scored component) and NOT a pure read (guard-2331: every invocation
     mutates drain-lane state), so no two runs are comparable by construction.

So the invariant worth pinning is not "index 0 is always max" -- that is false
and deliberately so. It is: THE SORT PRODUCES A SCORE-DESCENDING ARRAY, AND THE
DRAIN LANE IS THE ONLY SANCTIONED PERTURBATION OF THE HEAD. A future genuine
merge regression breaks the first half; a lane change that starts perturbing
more than the head breaks the second.

Fixture is the one g-115-6999's outcome 3 names: a two-queue candidate set
where the AGENT queue's first row scores BELOW the WORLD queue's first. Under
the reported defect the agent row would sit at index 0; under the real sort it
sorts to its score position.

Fixture idiom mirrors test_goal_selector_drain_lane.py (pin MIND_AGENT around
import so module-level agent resolution is deterministic). Exercises the REAL
`gs.candidate_sort_key` / `gs.apply_drain_lane` rather than a local
reimplementation (guard-3180).
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


CONFIG = {
    "drain_lane_enabled": True,
    "drain_lane_interval_iterations": 5,
    "substantive_demotion_overdue_exempt_ratio": 5.0,
    "substantive_demotion_short_interval_hours": 6.0,
    "substantive_demotion_short_interval_exempt_ratio": 1.0,
}


def row(gid, score, source, ratio=0.0, interval=24.0, recurring=False, asp="asp-115"):
    """A scored candidate in the shape cmd_select emits, carrying `source` so a
    per-queue read is possible (that is the diagnostic g-115-6999 ran)."""
    return {
        "goal_id": gid, "aspiration_id": asp, "score": score, "source": source,
        "recurring": recurring, "recurring_overdue_ratio": ratio,
        "recurring_interval_hours": interval, "raw": {}, "breakdown": {},
    }


def two_queue_fixture():
    """The fixture  outcome 3 names: the AGENT queue's first row
    (12.00) scores BELOW the WORLD queue's first (15.31). Insertion order is
    deliberately agent-first so a merge that concatenated rather than sorted
    would leave the agent row at index 0 -- i.e. the fixture can actually
    EXHIBIT the reported defect (guard-4127: a pin needs a fixture where the
    wrong answer is reachable)."""
    return [
        row("g-001-04", 12.00, "agent"),
        row("g-001-10", 9.36, "agent"),
        row("g-335-1329", 15.31, "world"),
        row("g-115-6999", 14.16, "world"),
        row("g-115-2687", 13.66, "world"),
        row("g-115-5442", 10.47, "world"),
    ]


def primed(tmp_path, k=5):
    """An agent_dir whose counter is already at K-1, so the NEXT invocation is
    the one eligible to pick."""
    d = tmp_path / "agent"
    (d / "session").mkdir(parents=True, exist_ok=True)
    gs.write_drain_lane_state(d, {"invocations_since_pick": k - 1})
    return d


# --------------------------------------------------------------------------
# The merge is score-ordered (the half  believed was broken)
# --------------------------------------------------------------------------

def test_merged_two_queue_ranking_is_score_descending():
    scored = two_queue_fixture()
    scored.sort(key=gs.candidate_sort_key)
    s = [r["score"] for r in scored]
    assert s == sorted(s, reverse=True), (
        "candidate_sort_key did not produce a score-descending array across a "
        "merged agent+world candidate set -- this IS the g-115-6999 defect and "
        "it would be a genuine merge regression: %r" % (s,)
    )


def test_index_zero_is_the_score_maximum_when_the_lane_does_not_fire():
    scored = two_queue_fixture()
    scored.sort(key=gs.candidate_sort_key)
    assert scored[0]["goal_id"] == "g-335-1329"
    assert scored[0]["score"] == max(r["score"] for r in scored)
    assert sum(1 for r in scored if r["score"] > scored[0]["score"]) == 0


def test_low_scoring_agent_row_is_not_pinned_to_the_head():
    """The report's core claim was that "an agent-queue goal lands at index 0
    regardless of its score". Insertion order here is agent-first, so this is
    the direct refutation."""
    scored = two_queue_fixture()
    assert scored[0]["source"] == "agent"      # pre-sort: agent row IS at index 0
    scored.sort(key=gs.candidate_sort_key)
    assert scored[0]["source"] == "world"      # post-sort: score decides, not source
    agent_positions = [i for i, r in enumerate(scored) if r["source"] == "agent"]
    assert agent_positions == [3, 5], (
        "agent rows should sort to their SCORE positions (12.00 and 9.36 among "
        "15.31/14.16/13.66/10.47), got %r" % (agent_positions,)
    )


def test_each_source_sublist_is_also_score_descending():
    """A per-source read of a correctly sorted array is still descending. The
    report read its agent sublist as NOT descending and took that as merge
    evidence; a non-descending sublist is in fact a fingerprint of the LANE
    having spliced one row out of position, not of a broken sort."""
    scored = two_queue_fixture()
    scored.sort(key=gs.candidate_sort_key)
    for src in ("agent", "world"):
        sub = [r["score"] for r in scored if r["source"] == src]
        assert sub == sorted(sub, reverse=True), (src, sub)


# --------------------------------------------------------------------------
# The drain lane is the ONLY sanctioned perturbation of the head
# --------------------------------------------------------------------------

def test_drain_lane_is_the_only_head_perturbation(tmp_path):
    """When the lane fires, index 0 is deliberately NOT the score maximum --
    and everything from index 1 on must remain score-descending. That pairing
    is the whole contract: a lane that perturbed more than the head would be
    indistinguishable from the merge regression above."""
    scored = two_queue_fixture()
    # One genuinely-starved recurring row, scoring LAST, eligible for the lane.
    starved = row("g-001-99", 8.00, "agent", ratio=6.0, interval=24.0, recurring=True)
    scored.append(starved)
    scored.sort(key=gs.candidate_sort_key)
    assert scored[0]["goal_id"] == "g-335-1329"

    pick = gs.apply_drain_lane(scored, dict(CONFIG), primed(tmp_path))
    assert pick is not None, "primed counter should make this invocation lane-eligible"
    assert scored[0]["goal_id"] == "g-001-99"
    assert scored[0]["score"] < max(r["score"] for r in scored), (
        "the lane pick is deliberately below the maximum -- that is the design, "
        "not the defect g-115-6999 reported"
    )
    tail = [r["score"] for r in scored[1:]]
    assert tail == sorted(tail, reverse=True), (
        "the lane must perturb ONLY the head; index 1.. stayed unsorted: %r" % (tail,)
    )


def test_lane_cooldown_restores_index_zero_to_the_maximum(tmp_path):
    """Consecutive invocations inside K do not re-perturb the head. This is why
    a reproduction attempt can honestly disagree with the original report: the
    reporter caught a firing, the reproducer caught a cooldown (and guard-2331
    -- every selector invocation advances that counter, so the act of
    re-measuring moves the thing being measured)."""
    d = tmp_path / "agent"
    (d / "session").mkdir(parents=True, exist_ok=True)
    gs.write_drain_lane_state(d, {"invocations_since_pick": 0})
    for _ in range(3):
        scored = two_queue_fixture()
        scored.append(row("g-001-99", 8.00, "agent", ratio=6.0, recurring=True))
        scored.sort(key=gs.candidate_sort_key)
        assert gs.apply_drain_lane(scored, dict(CONFIG), d) is None
        assert scored[0]["goal_id"] == "g-335-1329"
        assert scored[0]["score"] == max(r["score"] for r in scored)
