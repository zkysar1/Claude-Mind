""": Regression tests for goal.completed_at stamping at terminal transitions.

Pins the contract from the g-115-660 investigation: every goal that reaches a
terminal status (completed/skipped/expired/decomposed/superseded) MUST have a
non-null completed_at timestamp. Previously 533 goals across all agent queues
carried terminal status with completed_at=null because no writer in
aspirations.py stamped goal.completed_at.

Layer 1 (write-site stamps) and Layer 2 (_normalize_terminal_goal fallback)
together close the gap. These tests verify both layers.

Run: py -3 core/scripts/tests/test_completed_at_stamping.py
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


# Cached module reference — load once at import time
ASP = load_aspirations_module()


# ---------------------------------------------------------------------------
# Layer 2 tests: _normalize_terminal_goal stamps completed_at when missing
# ---------------------------------------------------------------------------


def test_normalize_stamps_completed_at_when_status_completed():
    """Terminal status + completed_at=None → normalizer stamps completed_at."""
    goal = {"id": "g-test-001", "status": "completed", "completed_at": None}
    ASP._normalize_terminal_goal(goal)
    assert goal["completed_at"] is not None, (
        "_normalize_terminal_goal MUST stamp completed_at when status is "
        "terminal and completed_at is None"
    )
    assert "T" in goal["completed_at"], (
        f"completed_at must be ISO datetime format (got {goal['completed_at']!r})"
    )


def test_normalize_stamps_completed_at_for_each_terminal_status():
    """All 5 terminal statuses trigger the stamp."""
    for status in ("completed", "skipped", "expired", "decomposed", "superseded"):
        goal = {"id": "g-test", "status": status, "completed_at": None}
        ASP._normalize_terminal_goal(goal)
        assert goal["completed_at"] is not None, (
            f"completed_at must be stamped for status={status!r}"
        )


def test_normalize_no_op_when_status_pending():
    """Non-terminal status → no stamp (completed_at stays None)."""
    goal = {"id": "g-test-002", "status": "pending", "completed_at": None}
    ASP._normalize_terminal_goal(goal)
    assert goal["completed_at"] is None, (
        "Non-terminal status must NOT trigger completed_at stamping; "
        "stamping pending goals would silently mis-mark active work"
    )


def test_normalize_idempotent_preserves_existing_completed_at():
    """Existing completed_at is preserved across normalizer calls."""
    original = "2026-04-15T10:30:00"
    goal = {"id": "g-test-003", "status": "completed", "completed_at": original}
    ASP._normalize_terminal_goal(goal)
    assert goal["completed_at"] == original, (
        f"Normalizer must NOT overwrite existing completed_at; "
        f"expected {original!r} preserved, got {goal['completed_at']!r}"
    )


def test_normalize_prefers_completed_date_over_now():
    """When completed_date is present (date-only), backfill uses it as anchor."""
    goal = {
        "id": "g-test-004",
        "status": "completed",
        "completed_at": None,
        "completed_date": "2026-04-15",
    }
    ASP._normalize_terminal_goal(goal)
    assert goal["completed_at"] == "2026-04-15T00:00:00", (
        f"When completed_date is date-only YYYY-MM-DD, normalizer must build "
        f"ISO datetime by appending T00:00:00 (preserves day-level history); "
        f"got {goal['completed_at']!r}"
    )


def test_normalize_uses_completed_date_iso_datetime_directly():
    """When completed_date is already full ISO datetime, use it as-is."""
    goal = {
        "id": "g-test-005",
        "status": "completed",
        "completed_at": None,
        "completed_date": "2026-04-15T14:30:00",
    }
    ASP._normalize_terminal_goal(goal)
    assert goal["completed_at"] == "2026-04-15T14:30:00", (
        f"When completed_date is full ISO datetime, normalizer must use it "
        f"unchanged; got {goal['completed_at']!r}"
    )


def test_normalize_falls_back_to_now_when_no_completed_date():
    """No completed_date → stamp current time."""
    from datetime import datetime
    goal = {"id": "g-test-006", "status": "completed", "completed_at": None}
    before = datetime.now().isoformat(timespec="seconds")
    ASP._normalize_terminal_goal(goal)
    after = datetime.now().isoformat(timespec="seconds")
    # The stamp should be a current timestamp (between before and after)
    assert before <= goal["completed_at"] <= after, (
        f"When neither completed_at nor completed_date is present, "
        f"normalizer must stamp current time; got {goal['completed_at']!r} "
        f"(expected between {before!r} and {after!r})"
    )


# ---------------------------------------------------------------------------
# Layer 2 tests: existing invariants (defer-clearing) still hold
# ---------------------------------------------------------------------------


def test_normalize_still_clears_defer_state():
    """The existing defer-clearing behavior must not regress."""
    goal = {
        "id": "g-test-007",
        "status": "completed",
        "completed_at": None,
        "defer_reason": "stale defer",
        "defer_reason_set_at": "2026-04-01T00:00:00",
        "blocker_ref": {"type": "external_signal", "external_id": "foo"},
        "blocked_since": "2026-04-01T00:00:00",
        "deferred_until": "2026-04-08",
    }
    ASP._normalize_terminal_goal(goal)
    assert goal["defer_reason"] is None
    assert goal["defer_reason_set_at"] is None
    assert "blocker_ref" not in goal
    assert "blocked_since" not in goal
    assert "deferred_until" not in goal
    # And the new behavior: completed_at stamped
    assert goal["completed_at"] is not None


# ---------------------------------------------------------------------------
# Schema invariant: VALID terminal statuses match TERMINAL_GOAL_STATUSES
# ---------------------------------------------------------------------------


def test_terminal_statuses_match_schema():
    """The 5 terminal statuses in this test must match aspirations.TERMINAL_GOAL_STATUSES."""
    expected = {"completed", "skipped", "expired", "decomposed", "superseded"}
    assert ASP.TERMINAL_GOAL_STATUSES == expected, (
        f"Test enumerates {expected} as terminal but aspirations.py defines "
        f"TERMINAL_GOAL_STATUSES = {ASP.TERMINAL_GOAL_STATUSES}. Update one or the other."
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_all():
    tests = [
        test_normalize_stamps_completed_at_when_status_completed,
        test_normalize_stamps_completed_at_for_each_terminal_status,
        test_normalize_no_op_when_status_pending,
        test_normalize_idempotent_preserves_existing_completed_at,
        test_normalize_prefers_completed_date_over_now,
        test_normalize_uses_completed_date_iso_datetime_directly,
        test_normalize_falls_back_to_now_when_no_completed_date,
        test_normalize_still_clears_defer_state,
        test_terminal_statuses_match_schema,
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
