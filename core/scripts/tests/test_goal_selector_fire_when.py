"""test_goal_selector_fire_when.py — Magic Wand #4 regression.

Verifies fire_when is appended to the precondition list in BOTH
collect_candidates and collect_blocked, and that:
  - passing fire_when → goal in candidates
  - failing fire_when → goal in blocked
  - missing fire_when → behaves as before (no precondition gate added)
  - malformed fire_when (string, or dict without "type") → ignored gracefully

Pattern: direct module import (no subprocess), call helpers with synthetic
aspiration fixtures. Mirrors test_quiescence_fragmentation_downgrade.py.

The SYMMETRY invariant from goal-selector.py L789 ("If you change one,
change the other.") is what this test guards: changes to fire_when
handling must apply to both candidate and blocked paths together.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# importlib because the module name has a hyphen.
GS_PATH = CORE_SCRIPTS / "goal-selector.py"
spec = importlib.util.spec_from_file_location("goal_selector", GS_PATH)
gs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gs)


def _make_aspiration(goal_overrides: dict) -> dict:
    """Minimal active aspiration carrying ONE pending goal with the given overrides."""
    base_goal = {
        "id": "g-test-01",
        "title": "Test goal",
        "status": "pending",
        "priority": "MEDIUM",
        "skill": None,
        "category": "test",
    }
    base_goal.update(goal_overrides)
    return {
        "id": "asp-test",
        "title": "Test aspiration",
        "status": "active",
        "scope": "sprint",
        "goals": [base_goal],
    }


def _ids_in(seq) -> set:
    """Extract goal_ids from collect_candidates and collect_blocked output.

    The two helpers return DIFFERENT shapes — candidates wrap the goal
    inside {'aspiration', 'goal', 'source'}, blocked uses flat
    {'goal_id': ...}. Tolerate both so the same helper works for both
    sides of the SYMMETRY check.
    """
    out = set()
    for entry in seq:
        if not isinstance(entry, dict):
            continue
        gid = entry.get("goal_id")
        if gid is None and isinstance(entry.get("goal"), dict):
            gid = entry["goal"].get("id")
        if gid:
            out.add(gid)
    return out


def main() -> int:
    failures = []

    # Predicate fixtures: file_check is the simplest synchronous predicate
    # (no subprocess, no after-ref resolution). Pointing at a real tempfile
    # means we can swap pass/fail by adjusting the path argument.
    with tempfile.NamedTemporaryFile(suffix=".fire-when-test", delete=False) as tmp:
        existing_path = tmp.name
    nonexistent_path = existing_path + ".does-not-exist"

    try:
        passing_fire_when = {
            "type": "file_check",
            "path": existing_path,
            "condition": "exists",
        }
        failing_fire_when = {
            "type": "file_check",
            "path": nonexistent_path,
            "condition": "exists",
        }

        # Case 1 — passing fire_when → goal lands in candidates, NOT in blocked.
        asps = [_make_aspiration({"fire_when": passing_fire_when})]
        cands = gs.collect_candidates(asps, source="agent")
        blocked = gs.collect_blocked(asps)
        if "g-test-01" not in _ids_in(cands):
            failures.append("[FAIL] passing-fire_when: goal missing from candidates")
        if "g-test-01" in _ids_in(blocked):
            failures.append("[FAIL] passing-fire_when: goal incorrectly in blocked")
        if not failures:
            print("  [PASS] passing-fire_when: goal in candidates, not blocked")

        # Case 2 — failing fire_when → goal in blocked, NOT in candidates.
        asps = [_make_aspiration({"fire_when": failing_fire_when})]
        cands = gs.collect_candidates(asps, source="agent")
        blocked = gs.collect_blocked(asps)
        if "g-test-01" in _ids_in(cands):
            failures.append("[FAIL] failing-fire_when: goal incorrectly in candidates")
        if "g-test-01" not in _ids_in(blocked):
            failures.append("[FAIL] failing-fire_when: goal missing from blocked")
        else:
            print("  [PASS] failing-fire_when: goal in blocked, not candidates")

        # Case 3 — no fire_when → goal in candidates (no gate to apply).
        asps = [_make_aspiration({})]
        cands = gs.collect_candidates(asps, source="agent")
        blocked = gs.collect_blocked(asps)
        if "g-test-01" not in _ids_in(cands):
            failures.append("[FAIL] no-fire_when: goal missing from candidates")
        if "g-test-01" in _ids_in(blocked):
            failures.append("[FAIL] no-fire_when: goal incorrectly in blocked")
        if "g-test-01" in _ids_in(cands) and "g-test-01" not in _ids_in(blocked):
            print("  [PASS] no-fire_when: behaves as if no gate")

        # Case 4 — malformed fire_when (string instead of dict) → ignored.
        # The `isinstance(fw, dict)` guard in goal-selector should skip it.
        asps = [_make_aspiration({"fire_when": "not-a-dict"})]
        cands = gs.collect_candidates(asps, source="agent")
        blocked = gs.collect_blocked(asps)
        if "g-test-01" not in _ids_in(cands):
            failures.append("[FAIL] string-fire_when: goal missing from candidates (string should be ignored)")
        else:
            print("  [PASS] string-fire_when: ignored, goal in candidates")

        # Case 5 — fire_when dict missing "type" → ignored (the `"type" in fw`
        # guard in goal-selector should skip it).
        asps = [_make_aspiration({"fire_when": {"path": existing_path}})]
        cands = gs.collect_candidates(asps, source="agent")
        blocked = gs.collect_blocked(asps)
        if "g-test-01" not in _ids_in(cands):
            failures.append("[FAIL] typeless-fire_when: goal missing from candidates")
        else:
            print("  [PASS] typeless-fire_when: ignored, goal in candidates")

    finally:
        Path(existing_path).unlink(missing_ok=True)

    print()
    if failures:
        for f in failures:
            print(f)
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("All 5 fire_when cases verified (candidate + blocked SYMMETRY).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
