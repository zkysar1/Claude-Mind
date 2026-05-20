"""fan_out_ratio derivation + initial_goal_count contract.

Pins the 2026-05-15 addition: aspirations carry `initial_goal_count`
(non-recurring goal count stamped once at creation) and
`progress.fan_out_ratio` = total_goals / initial_goal_count, so growth
from the seed is measurable instead of inferred. None semantics:
absent initial_goal_count (predates the metric) or 0 (empty seed).

Dual-mirror invariant: recompute_progress (core/scripts/aspirations.py)
and _recompute_progress (mind_api/src/endpoints/aspirations_write.py)
must keep the progress-dict shape identical. test_progress_keys_stable
guards that shape on the CLI mirror.

Run: py -3 core/scripts/tests/test_fan_out_ratio.py
"""
import importlib.util
import sys
import traceback
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))


def load_aspirations_module():
    """Import aspirations.py — bypasses the .py extension import quirk."""
    spec = importlib.util.spec_from_file_location(
        "aspirations_module",
        SCRIPT_DIR / "aspirations.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fan_out_ratio_basic():
    m = load_aspirations_module()
    asp = {"initial_goal_count": 2, "goals": [
        {"id": f"g-x-{i}", "status": "pending"} for i in range(6)]}
    m.recompute_progress(asp)
    assert asp["progress"]["total_goals"] == 6, asp["progress"]
    assert asp["progress"]["fan_out_ratio"] == 3.0, asp["progress"]


def test_fan_out_ratio_none_when_absent():
    m = load_aspirations_module()
    asp = {"goals": [{"id": "g-y-1", "status": "pending"}]}
    m.recompute_progress(asp)
    assert asp["progress"]["fan_out_ratio"] is None, asp["progress"]


def test_fan_out_ratio_none_when_zero():
    m = load_aspirations_module()
    asp = {"initial_goal_count": 0, "goals": [
        {"id": "g-z-1", "status": "pending"}]}
    m.recompute_progress(asp)
    assert asp["progress"]["fan_out_ratio"] is None, asp["progress"]


def test_recurring_excluded_from_ratio():
    m = load_aspirations_module()
    asp = {"initial_goal_count": 1, "goals": [
        {"id": "g-a-1", "status": "completed"},
        {"id": "g-a-2", "status": "pending", "recurring": True}]}
    m.recompute_progress(asp)
    # recurring excluded → total_goals 1, ratio 1/1 = 1.0
    assert asp["progress"]["total_goals"] == 1, asp["progress"]
    assert asp["progress"]["recurring_goals"] == 1, asp["progress"]
    assert asp["progress"]["fan_out_ratio"] == 1.0, asp["progress"]


def test_fan_out_ratio_rounds_two_dp():
    m = load_aspirations_module()
    # 3 seed → 10 now → 3.333... rounds to 3.33
    asp = {"initial_goal_count": 3, "goals": [
        {"id": f"g-b-{i}", "status": "pending"} for i in range(10)]}
    m.recompute_progress(asp)
    assert asp["progress"]["fan_out_ratio"] == 3.33, asp["progress"]


def test_progress_keys_stable():
    """Guards the dual-mirror shape invariant on the CLI mirror."""
    m = load_aspirations_module()
    asp = {"initial_goal_count": 1, "goals": [
        {"id": "g-c-1", "status": "pending"}]}
    m.recompute_progress(asp)
    assert set(asp["progress"].keys()) == {
        "completed_goals", "total_goals", "recurring_goals",
        "fan_out_ratio",
    }, asp["progress"]


def run_all():
    tests = [
        test_fan_out_ratio_basic,
        test_fan_out_ratio_none_when_absent,
        test_fan_out_ratio_none_when_zero,
        test_recurring_excluded_from_ratio,
        test_fan_out_ratio_rounds_two_dp,
        test_progress_keys_stable,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  [PASS] {t.__name__}")
        except AssertionError as e:
            print(f"  [FAIL] {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  [ERROR] {t.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print()
    if failed:
        print(f"FAILED: {failed}/{len(tests)}")
        return 1
    print(f"OK: {len(tests)}/{len(tests)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(run_all())
