"""B9/B6: aspirations-compact-completed strips bulky text bodies from aged
completed non-recurring goals WITHOUT changing the goal census (so goal-selector
completion_ratio / tail_bonus / recompute_progress are byte-identical — no scorer
change). Pure unit test of the modifier; no S3, no daemon."""
import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]  # core/scripts
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# Hyphenated module name → import via importlib.
_spec = importlib.util.spec_from_file_location(
    "aspirations_compact_completed",
    str(SCRIPTS / "aspirations-compact-completed.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


NOW = datetime(2026, 6, 1, 12, 0, 0)
CUTOFF = NOW - timedelta(days=14)   # 2026-05-18


def _goal(gid, status, **kw):
    g = {"id": gid, "status": status,
         "description": "x" * 500, "verification": {"outcomes": ["y"]},
         "outcome_note": "z" * 200, "title": "t", "priority": "MEDIUM"}
    g.update(kw)
    return g


def _fixture():
    return [{
        "id": "asp-115", "status": "active",
        "goals": [
            # eligible: completed, old, non-recurring, has bodies
            _goal("g-115-1", "completed", completed_at="2026-04-01T10:00:00"),
            # NOT eligible: completed but RECENT (within 14d window)
            _goal("g-115-2", "completed", completed_at="2026-05-30T10:00:00"),
            # NOT eligible: recurring
            _goal("g-115-3", "completed", completed_at="2026-04-01T10:00:00",
                  recurring=True),
            # NOT eligible: pending (live work)
            _goal("g-115-4", "pending", completed_at=None),
            # NOT eligible: already compacted (idempotent)
            {"id": "g-115-5", "status": "completed",
             "completed_at": "2026-04-01T10:00:00", "body_compacted": True,
             "title": "t"},
            # eligible: completed, old, only completed_date present
            _goal("g-115-6", "completed", completed_date="2026-03-15"),
        ],
    }]


def test_only_eligible_goals_are_compacted():
    items = _fixture()
    _mod._make_compactor(CUTOFF)(items)
    goals = {g["id"]: g for g in items[0]["goals"]}
    # eligible → bodies stripped + marked
    for gid in ("g-115-1", "g-115-6"):
        for f in _mod.STRIP_FIELDS:
            assert f not in goals[gid], f"{gid} should have lost {f}"
        assert goals[gid].get("body_compacted") is True
        assert goals[gid]["status"] == "completed"  # status preserved
    # NOT eligible → untouched (still carry a body)
    for gid in ("g-115-2", "g-115-3", "g-115-4"):
        assert "description" in goals[gid], f"{gid} must keep its body"
        assert "body_compacted" not in goals[gid]


def test_goal_census_is_invariant():
    items = _fixture()
    before = _mod._status_counts(items)
    _mod._make_compactor(CUTOFF)(items)
    after = _mod._status_counts(items)
    assert before == after, "compaction must not change goal counts (scoring!)"
    # total goals unchanged
    assert before[1] == after[1] == 6


def test_idempotent_second_run_no_change():
    items = _fixture()
    _mod._make_compactor(CUTOFF)(items)
    snapshot = [dict(g) for g in items[0]["goals"]]
    _mod._make_compactor(CUTOFF)(items)  # second run
    assert [dict(g) for g in items[0]["goals"]] == snapshot


def test_completed_dt_parsing():
    assert _mod._completed_dt({"completed_at": "2026-04-01T10:00:00"}) == \
        datetime(2026, 4, 1, 10, 0, 0)
    assert _mod._completed_dt({"completed_date": "2026-03-15"}) == \
        datetime(2026, 3, 15)
    assert _mod._completed_dt({}) is None
    assert _mod._completed_dt({"completed_at": "garbage"}) is None


def test_undateable_completed_goal_is_skipped():
    # No parseable completion date → conservative skip (never strip).
    items = [{"id": "asp-x", "status": "active", "goals": [
        _goal("g-x-1", "completed")]}]  # no completed_at/date
    _mod._make_compactor(CUTOFF)(items)
    assert "description" in items[0]["goals"][0]
    assert "body_compacted" not in items[0]["goals"][0]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
