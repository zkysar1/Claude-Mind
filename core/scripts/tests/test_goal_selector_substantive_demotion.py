"""test_goal_selector_substantive_demotion.py — FW-1 regression (2026-05-25).

Exercises apply_substantive_demotion, extracted from goal-selector.py. Six of
seven agents reported recurring sweeps perpetually out-ranking rare substantive
work; FW-1 caps a recurring goal's score to `substantive_demotion_margin` below
the best non-recurring, agent-executable candidate — unless the recurring goal
is overdue beyond `substantive_demotion_overdue_exempt_ratio`.

Pattern mirrors test_goal_selector_role_affinity.py: capture/restore MIND_AGENT
around the module-level import (goal-selector derives AGENT_DIR at import), then
call the pure function directly. No subprocess, no file I/O.
"""

from __future__ import annotations

import importlib
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "bravo")

gs = importlib.import_module("goal-selector")
apply_substantive_demotion = gs.apply_substantive_demotion
# : the FW-1 anchor-repair pass () and one REAL boost to drive
# it. The recap defect only exists in the INTERACTION — a boost lifting the
# substantive top after apply_substantive_demotion froze the cap — so a test that
# calls the recap alone cannot reproduce it.
reapply_substantive_demotion_cap = gs.reapply_substantive_demotion_cap
apply_starvation_boost = gs.apply_starvation_boost

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT


# Canonical config — defaults the selector ships with.
CFG = {
    "substantive_demotion_enabled": True,
    "substantive_demotion_margin": 0.5,
    "substantive_demotion_floor": 5.0,
    "substantive_demotion_overdue_exempt_ratio": 5.0,
}


def _goal(goal_id, score, *, recurring=False, agent_exec=2, overdue=0.0):
    """Build a minimal scored-goal dict shaped like score_goal's return value."""
    return {
        "goal_id": goal_id,
        "aspiration_id": "asp-001",
        "recurring": recurring,
        "recurring_overdue_ratio": overdue,
        "score": score,
        "breakdown": {},
        "raw": {"agent_executable": agent_exec},
    }


def _by_id(scored):
    return {s["goal_id"]: s for s in scored}


def test_fires_caps_recurring_below_top_substantive():
    """Recurring #1 (13.87) demoted below substantive (8.0): cap = 8.0 - 0.5 = 7.5."""
    scored = [
        _goal("g-rec", 13.87, recurring=True),
        _goal("g-sub", 8.0),
    ]
    apply_substantive_demotion(scored, CFG)
    m = _by_id(scored)
    assert m["g-rec"]["score"] == 7.5, m["g-rec"]["score"]
    assert m["g-sub"]["score"] == 8.0, "substantive untouched"
    # And after a sort the substantive goal ranks first.
    scored.sort(key=lambda x: -x["score"])
    assert scored[0]["goal_id"] == "g-sub"


def test_exempt_when_overdue_beyond_ratio():
    """Recurring overdue 6.0x (>= 5.0 exempt ratio) keeps full score — monitoring must not rot."""
    scored = [
        _goal("g-rec", 13.87, recurring=True, overdue=6.0),
        _goal("g-sub", 8.0),
    ]
    apply_substantive_demotion(scored, CFG)
    m = _by_id(scored)
    assert m["g-rec"]["score"] == 13.87, "overdue-exempt recurring is NOT demoted"
    assert "substantive_demotion" not in m["g-rec"]["breakdown"]


def test_overdue_just_below_ratio_still_demoted():
    """Boundary: overdue 4.99x (< 5.0) is still demoted; the exemption is strict-less-than."""
    scored = [
        _goal("g-rec", 13.87, recurring=True, overdue=4.99),
        _goal("g-sub", 8.0),
    ]
    apply_substantive_demotion(scored, CFG)
    assert _by_id(scored)["g-rec"]["score"] == 7.5


def test_no_substantive_candidate_no_change():
    """All-recurring slate: nothing to protect, no demotion."""
    scored = [
        _goal("g-rec1", 13.87, recurring=True),
        _goal("g-rec2", 9.0, recurring=True),
    ]
    apply_substantive_demotion(scored, CFG)
    m = _by_id(scored)
    assert m["g-rec1"]["score"] == 13.87
    assert m["g-rec2"]["score"] == 9.0


def test_substantive_below_floor_no_change():
    """Substantive top score 3.0 < floor 5.0 — don't suppress maintenance for stragglers."""
    scored = [
        _goal("g-rec", 13.87, recurring=True),
        _goal("g-sub", 3.0),
    ]
    apply_substantive_demotion(scored, CFG)
    assert _by_id(scored)["g-rec"]["score"] == 13.87


def test_non_agent_executable_substantive_ignored():
    """A non-recurring goal this agent can't execute (agent_executable=0) is not 'substantive'."""
    scored = [
        _goal("g-rec", 13.87, recurring=True),
        _goal("g-sub", 8.0, agent_exec=0),
    ]
    apply_substantive_demotion(scored, CFG)
    assert _by_id(scored)["g-rec"]["score"] == 13.87, "no eligible substantive work → no demotion"


def test_disabled_no_change():
    cfg = dict(CFG, substantive_demotion_enabled=False)
    scored = [
        _goal("g-rec", 13.87, recurring=True),
        _goal("g-sub", 8.0),
    ]
    apply_substantive_demotion(scored, cfg)
    assert _by_id(scored)["g-rec"]["score"] == 13.87


def test_recurring_already_below_cap_untouched():
    """Recurring (4.0) already below cap (7.5) is left alone — the guard is score > cap."""
    scored = [
        _goal("g-rec", 4.0, recurring=True),
        _goal("g-sub", 8.0),
    ]
    apply_substantive_demotion(scored, CFG)
    m = _by_id(scored)
    assert m["g-rec"]["score"] == 4.0
    assert "substantive_demotion" not in m["g-rec"]["breakdown"]


def test_multiple_recurring_all_capped():
    """Every recurring goal above the cap is demoted to it (not just #1)."""
    scored = [
        _goal("g-rec1", 13.87, recurring=True),
        _goal("g-rec2", 9.0, recurring=True),
        _goal("g-sub", 8.0),
    ]
    apply_substantive_demotion(scored, CFG)
    m = _by_id(scored)
    assert m["g-rec1"]["score"] == 7.5
    assert m["g-rec2"]["score"] == 7.5
    assert m["g-sub"]["score"] == 8.0


def test_telemetry_recorded_on_demotion():
    """Demoted goals carry breakdown + raw telemetry; pre-score preserved."""
    scored = [
        _goal("g-rec", 13.87, recurring=True),
        _goal("g-sub", 8.0),
    ]
    apply_substantive_demotion(scored, CFG)
    rec = _by_id(scored)["g-rec"]
    assert rec["breakdown"]["substantive_demotion"] == round(7.5 - 13.87, 2)
    assert rec["raw"]["substantive_demotion_applied"] is True
    assert rec["raw"]["substantive_demotion_pre_score"] == 13.87


def test_single_candidate_no_change():
    """< 2 candidates: nothing to compare against."""
    scored = [_goal("g-rec", 13.87, recurring=True)]
    apply_substantive_demotion(scored, CFG)
    assert scored[0]["score"] == 13.87


# ── : reapply_substantive_demotion_cap (the FW-1 anchor repair) ──────
#  shipped this pass with ZERO tests referencing it across all three of
# pytest.ini's testpaths — the third reproduction of the same frozen-cap defect,
# and the reason attempt #2 was caught is that tests existed for it.
#
# THE DEFECT IS AN INTERACTION, NOT A FUNCTION. apply_substantive_demotion freezes
# `cap = top_sub - margin`; four boost passes then run before the sort. Every
# substantive row they lift raises the true top without moving the cap, so the
# bound actually delivered is `top_sub_post - cap` (measured 3.32 / 3.45 / 4.08
# against a contracted 0.5). So these tests drive the REAL pass sequence with a
# REAL boost — calling the recap alone cannot reproduce what it repairs.

# apply_starvation_boost's own config shape (). Held locally and
# explicitly rather than read from gs.STARVATION_CONFIG so the arithmetic below is
# deterministic and cannot drift when the shipped defaults are retuned.
STARVE_CFG = {
    "enabled": True,
    "min_age_hours": 12.0,
    "full_boost_age_hours": 36.0,
    "max_boost": 4.0,
    "priority_multipliers": {"HIGH": 1.0},
}


def _aged_high(goal_id, score, *, hours_old=48.0, agent_exec=2):
    """A non-recurring HIGH goal old enough for the starvation boost's FULL lift.

    Additive wrapper around _goal rather than new kwargs on it: the eleven tests
    above share that helper and none of them wants a priority or a created_at.
    """
    g = _goal(goal_id, score, agent_exec=agent_exec)
    g["raw"]["priority"] = gs.PRIORITY_MAP["HIGH"]
    g["created_at"] = (datetime.now() - timedelta(hours=hours_old)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    return g


def _code_lines(src):
    """Source with comment-ONLY lines dropped. SECONDARY defense for the pin below.

    Measured, because an approximate reason here would be worse than none. A
    naive scan for the bare symbol `reapply_substantive_demotion_cap` lands at
    goal-selector.py:6078 — the `def` — which is 1,204 lines ABOVE the call at
    :7282, so the pin would measure the wrong position and still look green.
    That hazard is CODE, not a comment, so stripping comments does not address
    it: the primary defense is anchoring on the full call EXPRESSION including
    its module-level config global, verified to occur exactly once.

    This strip handles the smaller remaining case — one comment-only line,
    :7281 (`# boosts that just fired. See reapply_substantive_demotion_cap.`),
    sitting directly above the call. Cheap, and it keeps the pin honest if
    someone later shortens the anchor. A pin that reads its own rationale as
    code is worse than no pin.
    """
    return [ln for ln in src.splitlines() if not ln.strip().startswith("#")]


def test_recap_restores_the_contracted_margin_after_a_post_freeze_boost():
    """The realized margin collapses back to `substantive_demotion_margin`.

    guard-5491: the expected value is read from CFG, never written as a literal,
    and the PRE-recap gap is measured HERE as the collapsed-state baseline. Both
    controls run before the recap, so under sabotage only the final behavioural
    assertion can fire — the failure is attributable, not ambiguous.
    """
    margin = CFG["substantive_demotion_margin"]
    scored = [
        _goal("g-rec", 13.87, recurring=True),
        _aged_high("g-sub", 8.0),
    ]
    apply_substantive_demotion(scored, CFG)
    assert _by_id(scored)["g-rec"]["score"] == 7.5, "first pass froze cap at 8.0-0.5"

    apply_starvation_boost(scored, STARVE_CFG)
    boosted_top = _by_id(scored)["g-sub"]["score"]
    assert boosted_top > 8.0, (
        "POSITIVE CONTROL: the starvation boost did not lift the substantive top, "
        "so this fixture no longer reproduces the stale-anchor defect and the "
        f"behavioural assertion below would pass vacuously. score={boosted_top}"
    )
    pre_recap_gap = round(boosted_top - _by_id(scored)["g-rec"]["score"], 2)
    assert pre_recap_gap > margin, (
        "POSITIVE CONTROL: the gap is already at the contracted margin before the "
        f"recap runs ({pre_recap_gap} vs {margin}) — nothing left to repair, so a "
        "green below would prove nothing."
    )

    reapply_substantive_demotion_cap(scored, CFG)
    m = _by_id(scored)
    realized = round(m["g-sub"]["score"] - m["g-rec"]["score"], 2)
    assert realized == margin, (
        f"realized margin {realized} != contracted {margin} (pre-recap gap was "
        f"{pre_recap_gap}) — the frozen-anchor defect is back"
    )
    assert m["g-sub"]["score"] == boosted_top, "relax-only: substantive top untouched"
    assert m["g-rec"]["raw"]["substantive_demotion_recap_applied"] is True


def test_recap_is_relax_only_and_restores_no_further_than_the_pre_score():
    """min(pre_score, cap): a row whose pre-score sits BELOW the new cap stops there.

    Pins the relax-only invariant in the direction that a cap-only implementation
    would get wrong — restoring to the cap (11.5) rather than to the score the row
    actually had (9.0) would INVENT 2.5 points the goal never earned.
    """
    scored = [
        _goal("g-rec", 9.0, recurring=True),
        _aged_high("g-sub", 8.0),
    ]
    apply_substantive_demotion(scored, CFG)
    before_recap = {s["goal_id"]: s["score"] for s in scored}
    assert before_recap["g-rec"] == 7.5

    apply_starvation_boost(scored, STARVE_CFG)
    apply_starvation_boost_applied = _by_id(scored)["g-sub"]["score"]
    assert apply_starvation_boost_applied > 8.0, "POSITIVE CONTROL: boost must fire"

    reapply_substantive_demotion_cap(scored, CFG)
    m = _by_id(scored)
    assert m["g-rec"]["score"] == 9.0, (
        "restored to its own pre-score, not to the re-derived cap: "
        f"got {m['g-rec']['score']}"
    )
    # No row may DECREASE across the recap — the property that lets this pass run
    # after four boosts without fighting them (guard-5601 anti-correlated terms).
    for s in scored:
        assert s["score"] >= before_recap[s["goal_id"]], (
            f"{s['goal_id']} was LOWERED by the recap "
            f"({before_recap[s['goal_id']]} -> {s['score']}); the pass is relax-only"
        )


def test_recap_is_a_no_op_when_no_boost_moved_the_substantive_top():
    """Row already at the re-derived cap is left untouched, telemetry included.

    This is the no-regression half: with no boost between the two passes the cap
    re-derives to the same value, `new_score <= s["score"]` holds, and the pass
    must change nothing at all.
    """
    scored = [
        _goal("g-rec", 13.87, recurring=True),
        _goal("g-sub", 8.0),
    ]
    apply_substantive_demotion(scored, CFG)
    reapply_substantive_demotion_cap(scored, CFG)
    rec = _by_id(scored)["g-rec"]
    assert rec["score"] == 7.5, "unchanged — cap re-derived to the same 7.5"
    assert "substantive_demotion_recap_applied" not in rec["raw"], (
        "no recap telemetry may be written when the row was not moved"
    )
    assert _by_id(scored)["g-sub"]["score"] == 8.0


def test_recap_call_site_runs_after_every_boost_and_before_the_sort():
    """WIRING pin: the three tests above certify the FUNCTION, never its POSITION.

    The pass is only correct where it sits — it must see the FINAL substantive top
    (so: after every boost) and its result must drive the ranking (so: before the
    sort). A behavioural test cannot see that, because it calls the function
    itself; deleting the call site in cmd_select leaves all three green. This pin
    is what goes red for that mutation.

    Anchored on the exact call EXPRESSIONS including their module-level config
    globals — each verified to occur exactly once in the file — so neither the
    `def` at module scope nor any docstring mentioning these names can match.
    """
    src = (CORE_SCRIPTS / "goal-selector.py").read_text(encoding="utf-8")
    code = "\n".join(_code_lines(src))

    recap_call = "reapply_substantive_demotion_cap(scored, RECURRING_CONFIG)"
    boost_calls = [
        "apply_starvation_boost(scored, STARVATION_CONFIG)",
        "apply_pull_boost(scored, PULL_CONFIG)",
        "apply_fan_in_boost(scored, all_aspirations, FAN_IN_CONFIG)",
    ]
    sort_call = "scored.sort(key=candidate_sort_key)"

    recap_at = code.find(recap_call)
    assert recap_at != -1, (
        f"{recap_call!r} is not called in goal-selector.py — the FW-1 anchor repair "
        "is unwired, so the frozen-cap defect is live again (g-358-104/g-115-10091)"
    )
    for call in boost_calls:
        at = code.find(call)
        assert at != -1, f"{call!r} vanished — re-derive this pin before trusting it"
        assert at < recap_at, (
            f"{call!r} runs AFTER the recap; the recap would then re-derive its cap "
            "from a pre-boost top and deliver the stale bound it exists to fix"
        )
    sort_at = code.find(sort_call, recap_at)
    assert sort_at != -1, (
        f"no {sort_call!r} after the recap — its result must drive the ranking"
    )
