"""Floor-hit Idea prose contract ().

The auto-filed "Idea: Rebase original interval for <goal>" carried two claims
that are FALSE by construction, and both steered a reader toward the wrong
remedy:

  1. "auto-contract has reached the floor ({floor}h ...)" — this branch is
     reached precisely when `proposed < floor`, i.e. the interval is ABOVE the
     floor and the NEXT contraction would cross it. Measured on g-335-09: the
     Idea read "reached the floor (7.92h)" while interval_hours was 10.67h.
     A reader (and one did) takes 7.92 for the current interval.
  2. "issues found on every fire" — an unconditional gloss on `consecutive_deep`,
     which is a STREAK, not a RATE. Measured on g-335-09: streak 3 against a
     substantive rate of 22/32 = 68.8%.

Both matter because guard-3060 and guard-2406 turn on exactly this distinction
(judge a recurring sensor's outcome class on its OWN reading, never on the
iteration's), and a reader cannot apply either guardrail from a description that
reports only the streak.

Run: STORAGE_BACKEND=local py -3 -m pytest \
       core/scripts/tests/test_cargo_cult_floor_idea_prose.py -v
"""

import argparse
import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DETECTOR_PY = REPO / "core" / "scripts" / "cargo-cult-detector.py"

# divisor 1.5 / floor_ratio 0.33 against interval 1.78, original 4.0:
#   proposed = 1.19, floor = 1.32  ->  1.19 < 1.32  ->  floor HIT
CONTRACT_CFG = {
    "deep_streak_contract_threshold": 3,
    "deep_streak_contract_divisor": 1.5,
    "contract_floor_ratio": 0.33,
    "contract_suppress_window": 5,
    "contract_suppress_min_samples": 3,
}
DETECTOR_CFG = {"multiplier": 1.5, "cap_ratio": 3.0}


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "cargo_cult_detector_floor_prose", DETECTOR_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_source(path: Path, goal: dict):
    asp = {"id": "asp-t", "status": "active", "goals": [goal]}
    path.write_text(json.dumps(asp) + "\n", encoding="utf-8")


def _mk_goal(**over):
    g = {
        "id": "g-t-01", "title": "Test recurring sensor", "recurring": True,
        "status": "pending", "interval_hours": 1.78,
        "original_interval_hours": 4.0, "consecutive_deep": 3,
    }
    g.update(over)
    return g


def _run(tmp_path, goal):
    """Drive the floor-hit branch and return the filed idea dict."""
    mod = _load_module()
    src = tmp_path / "aspirations.jsonl"
    _write_source(src, goal)
    filed = []
    mod.source_path = lambda source, agent_override=None: src
    mod.reset_consecutive_deep = lambda gid, src_: True
    mod.file_idea = lambda asp_id, source, idea: filed.append(idea) or "g-t-99"
    mod._load_streak_mult = lambda: 2.0
    # actual 1.8h vs streak_mult(2.0) * interval(1.78) = 3.56 -> NO suppression,
    # so control reaches the floor branch rather than the rebase-UP branch.
    mod._recent_actual_cadence = (
        lambda gid, window, min_samples, log_path=None: (1.8, "ok", 5))
    args = argparse.Namespace(goal_id="g-t-01", source="world", dry_run=False)
    rc = mod.cmd_contract_per_goal(args, DETECTOR_CFG, CONTRACT_CFG)
    return rc, filed


def test_positive_control_floor_branch_is_actually_reached(tmp_path):
    """Anti-vacuity (guard-1638): every assertion below is worthless if the
    floor branch never runs. Prove it files exactly one Idea first."""
    rc, filed = _run(tmp_path, _mk_goal())
    assert rc == 0
    assert len(filed) == 1, (
        "floor branch did not file — the prose assertions in this file would "
        "pass vacuously against an empty list")
    assert filed[0]["title"] == "Idea: Rebase original interval for g-t-01"


def test_description_does_not_claim_the_floor_was_reached(tmp_path):
    """The interval is ABOVE the floor in this branch, by construction."""
    _, filed = _run(tmp_path, _mk_goal())
    desc = filed[0]["description"]
    assert "has reached the floor" not in desc, (
        "regression: the branch fires when proposed < floor, i.e. the interval "
        "has NOT reached the floor")
    assert "NOT reached the floor" in desc
    # Both numbers a reader needs must be present, not just the threshold.
    assert "1.78h" in desc, "current interval_hours must be named"
    assert "1.19h" in desc, "the proposed next contraction must be named"
    assert "1.32h" in desc, "the floor threshold must still be named"


def _universality_claims(desc: str) -> int:
    """Count assertions that the goal fires deep EVERY time.

    Pins the CLAIM, not one phrasing of it. The first version of these tests
    asserted `"issues found on every fire" not in desc` — the exact string the
    fix deleted from the description HEAD — and so was blind to the identical
    claim surviving verbatim in remedy option 2 ("Investigate WHY every fire
    produces deep outcomes"), which left the rendered Idea asserting both
    "NOT every fire" and "every fire" three paragraphs apart. The mutation
    proof did not catch it either: the mutant restored the head phrase, so the
    string assertion had something to fail on and looked mutation-sensitive.
    Keying on the deleted literal makes a test that can only ever detect the
    one instance already fixed.
    """
    return desc.replace("NOT every fire", "").count("every fire")


def test_description_reports_the_rate_not_just_the_streak(tmp_path):
    """With substantive counters present, carry the goal's OWN hit rate."""
    _, filed = _run(tmp_path, _mk_goal(substantive_hits=22, substantive_runs=32))
    desc = filed[0]["description"]
    assert _universality_claims(desc) == 0, (
        "regression: the description asserts the goal fires deep on EVERY run; "
        "consecutive_deep is a STREAK, not a rate")
    assert "22/32" in desc
    assert "68.8%" in desc
    assert "NOT every fire" in desc
    # The reader must be pointed at the guardrails that turn on this distinction.
    assert "guard-3060" in desc and "guard-2406" in desc


def test_missing_counters_say_unknown_rather_than_asserting_a_rate(tmp_path):
    """A goal that tracks no counters must not get a fabricated rate, and must
    not silently fall back to the 'every fire' claim either."""
    _, filed = _run(tmp_path, _mk_goal())  # no substantive_* keys
    desc = filed[0]["description"]
    assert _universality_claims(desc) == 0
    assert "own hit RATE is unknown" in desc
    assert "a streak is not a rate" in desc
    assert "%" not in desc.split("Either:")[0].replace("68.8%", ""), (
        "no percentage may be asserted when the counters are absent")


def test_zero_runs_does_not_divide_by_zero(tmp_path):
    """substantive_runs == 0 must take the unknown branch, not raise."""
    _, filed = _run(tmp_path, _mk_goal(substantive_hits=0, substantive_runs=0))
    desc = filed[0]["description"]
    assert "own hit RATE is unknown" in desc


def test_three_remedies_are_preserved(tmp_path):
    """The prose fix must not drop the decision the goal exists to force."""
    _, filed = _run(tmp_path, _mk_goal())
    desc = filed[0]["description"]
    for token in ("Rebase original_interval_hours",
                  "Investigate WHY the deep streak keeps rebuilding", "Retire"):
        assert token in desc, f"remedy option lost from the template: {token}"
    assert filed[0]["verification"]["outcomes"] == [
        "Decision recorded: rebase / investigate / retire"]
