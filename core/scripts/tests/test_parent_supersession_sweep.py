"""test_parent_supersession_sweep.py — regression test for .

Asserts that parent-supersession-sweep.py's helper functions correctly
identify the g-268-10 canonical incident shape AND reject false-positive
cases that would have leaked through the v0 heuristic before the
aspiration-size guard was added.

Cases covered:
  1. Canonical incident: parent deferred + 2 siblings completed AFTER defer
     → both siblings identified as superseding
  2. Sibling completed BEFORE parent defer → excluded (parent was the
     consumer, not superseded)
  3. Sibling pending (not completed) → excluded
  4. Sibling title doesn't start with "Apply:" or "Design:" → excluded
  5. Parent has no defer_set_at and no created_at → empty result (cannot
     enforce temporal guard)
  6. Apply pattern matcher recognizes "Apply: foo", "  Apply : foo" (
     whitespace tolerant), rejects "Investigate: foo", "Idea: foo"
  7. Design/Apply pattern matcher recognizes "Design: foo" AND "Apply: foo"

The aspiration-size guard is tested separately via the smoke-test
documentation (see goal description) — running the script against the
live store. The unit tests here cover the per-goal logic.

Pattern: same importlib + sys.path shape as test_defer_recheck_patterns.py.
parent-supersession-sweep.py uses a hyphenated filename so we load it via
spec_from_file_location with a hyphen-free attribute name.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))


def _import_sweep():
    """Load parent-supersession-sweep.py via importlib."""
    spec = importlib.util.spec_from_file_location(
        "parent_supersession_sweep_mod",
        CORE_SCRIPTS / "parent-supersession-sweep.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load spec for parent-supersession-sweep.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _iso_hours_ago(hours: float) -> str:
    t = dt.datetime.now() - dt.timedelta(hours=hours)
    return t.isoformat(timespec="seconds")


def test_canonical_incident_shape():
    """ shape: parent deferred at T, 2 siblings completed at T+1h."""
    mod = _import_sweep()
    parent = {
        "id": "g-268-10",
        "title": "Apply: Implement BT-seed prefetch",
        "status": "pending",
        "defer_reason": "blocked_on_design",
        "defer_reason_set_at": _iso_hours_ago(48),
    }
    siblings = [
        parent,  # included to exercise the self-skip branch
        {
            "id": "g-268-15",
            "title": "Design: Option C for BT-seed",
            "status": "completed",
            "completed_at": _iso_hours_ago(47),  # AFTER parent defer (1h later)
        },
        {
            "id": "g-268-16",
            "title": "Apply: SeedGetterCache 119 LOC",
            "status": "completed",
            "completed_at": _iso_hours_ago(46),  # AFTER parent defer (2h later)
        },
    ]
    out = mod._find_superseding_siblings(parent, siblings)
    ids = [s["id"] for s in out]
    assert "g-268-15" in ids, f"expected g-268-15 in {ids}"
    assert "g-268-16" in ids, f"expected g-268-16 in {ids}"
    assert "g-268-10" not in ids, "self-skip failed"
    assert len(out) == 2


def test_sibling_completed_before_parent_defer_excluded():
    """Sibling completed BEFORE parent was deferred → parent consumed
    sibling work, not superseded by it. Exclude."""
    mod = _import_sweep()
    parent = {
        "id": "g-001",
        "title": "Apply: do thing X",
        "status": "pending",
        "defer_reason": "wait",
        "defer_reason_set_at": _iso_hours_ago(10),
    }
    siblings = [
        {
            "id": "g-002",
            "title": "Apply: thing X core",
            "status": "completed",
            "completed_at": _iso_hours_ago(20),  # BEFORE parent defer
        },
    ]
    out = mod._find_superseding_siblings(parent, siblings)
    assert out == [], f"expected empty, got {out}"


def test_sibling_pending_excluded():
    """Pending siblings don't count — only completed work supersedes."""
    mod = _import_sweep()
    parent = {
        "id": "g-001",
        "title": "Apply: X",
        "status": "pending",
        "defer_reason": "wait",
        "defer_reason_set_at": _iso_hours_ago(48),
    }
    siblings = [
        {
            "id": "g-002",
            "title": "Apply: X core",
            "status": "pending",  # not completed
        },
        {
            "id": "g-003",
            "title": "Apply: X done",
            "status": "completed",
            "completed_at": _iso_hours_ago(24),
        },
    ]
    out = mod._find_superseding_siblings(parent, siblings)
    ids = [s["id"] for s in out]
    assert ids == ["g-003"], f"expected only g-003, got {ids}"


def test_non_apply_design_title_excluded():
    """Investigate/Idea/Maintain title patterns are not supersession candidates."""
    mod = _import_sweep()
    parent = {
        "id": "g-001",
        "title": "Apply: X",
        "status": "pending",
        "defer_reason": "wait",
        "defer_reason_set_at": _iso_hours_ago(48),
    }
    siblings = [
        {
            "id": "g-002",
            "title": "Investigate: X cause",
            "status": "completed",
            "completed_at": _iso_hours_ago(24),
        },
        {
            "id": "g-003",
            "title": "Idea: X variant",
            "status": "completed",
            "completed_at": _iso_hours_ago(24),
        },
        {
            "id": "g-004",
            "title": "Maintain: X update",
            "status": "completed",
            "completed_at": _iso_hours_ago(24),
        },
    ]
    out = mod._find_superseding_siblings(parent, siblings)
    assert out == [], f"expected empty (no Apply/Design titles), got {out}"


def test_parent_without_reference_timestamp_returns_empty():
    """No defer_reason_set_at AND no created_at → cannot enforce temporal
    guard. Skip rather than treat all siblings as candidates."""
    mod = _import_sweep()
    parent = {
        "id": "g-001",
        "title": "Apply: X",
        "status": "pending",
        "defer_reason": "wait",
        # No defer_reason_set_at or created_at
    }
    siblings = [
        {
            "id": "g-002",
            "title": "Apply: X done",
            "status": "completed",
            "completed_at": _iso_hours_ago(24),
        },
    ]
    out = mod._find_superseding_siblings(parent, siblings)
    assert out == [], f"expected empty (no ref ts), got {out}"


def test_apply_pattern_matcher():
    """Apply-pattern recognition: case + whitespace tolerant."""
    mod = _import_sweep()
    assert mod._is_apply_goal({"title": "Apply: foo"}) is True
    assert mod._is_apply_goal({"title": "  Apply : foo"}) is True
    assert mod._is_apply_goal({"title": "apply: foo"}) is True  # case-insens
    assert mod._is_apply_goal({"title": "Investigate: foo"}) is False
    assert mod._is_apply_goal({"title": "Design: foo"}) is False
    assert mod._is_apply_goal({"title": "Idea: foo"}) is False
    assert mod._is_apply_goal({"title": ""}) is False


def test_design_or_apply_pattern_matcher():
    """Design+Apply pattern recognition for sibling filtering."""
    mod = _import_sweep()
    assert mod._is_design_or_apply({"title": "Apply: foo"}) is True
    assert mod._is_design_or_apply({"title": "Design: foo"}) is True
    assert mod._is_design_or_apply({"title": "DESIGN: foo"}) is True
    assert mod._is_design_or_apply({"title": "Investigate: foo"}) is False
    assert mod._is_design_or_apply({"title": "Idea: foo"}) is False
    assert mod._is_design_or_apply({"title": ""}) is False


def test_fallback_to_created_at_when_no_defer_set():
    """If defer_reason_set_at is missing but created_at exists, use created_at
    as the temporal reference. Siblings completed after created_at qualify."""
    mod = _import_sweep()
    parent = {
        "id": "g-001",
        "title": "Apply: X",
        "status": "pending",
        "defer_reason": "wait",
        "created_at": _iso_hours_ago(48),  # no defer_reason_set_at
    }
    siblings = [
        {
            "id": "g-002",
            "title": "Apply: X done",
            "status": "completed",
            "completed_at": _iso_hours_ago(24),  # 24h after parent.created_at
        },
    ]
    out = mod._find_superseding_siblings(parent, siblings)
    ids = [s["id"] for s in out]
    assert ids == ["g-002"], f"expected g-002 via created_at fallback, got {ids}"


if __name__ == "__main__":
    test_canonical_incident_shape()
    test_sibling_completed_before_parent_defer_excluded()
    test_sibling_pending_excluded()
    test_non_apply_design_title_excluded()
    test_parent_without_reference_timestamp_returns_empty()
    test_apply_pattern_matcher()
    test_design_or_apply_pattern_matcher()
    test_fallback_to_created_at_when_no_defer_set()
    print("All 8 tests passed.")
