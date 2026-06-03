"""B9-deep: goal eviction is metric-NEUTRAL.

aspirations-evict-completed.py removes aged terminal non-recurring goals from the
live `goals` list and caches their per-status counts in `archived_census`. Every
completion consumer reads through _goal_census.effective_counts, which folds the
census back in. This test proves the core promise: every completion denominator
the codebase uses is byte-identical before and after eviction, for a fixture that
exercises all four variants plus the cadence completed-count.

Pure unit test of the evictor modifier + the census helper. No S3, no daemon."""
import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]  # core/scripts
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load(mod_name, file_name):
    spec = importlib.util.spec_from_file_location(mod_name, str(SCRIPTS / file_name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


import _goal_census as census  # clean name, direct import
_evict = _load("aspirations_evict_completed", "aspirations-evict-completed.py")


NOW = datetime(2026, 6, 1, 12, 0, 0)
CUTOFF = NOW - timedelta(days=45)   # 2026-04-17


def _goal(gid, status, days_ago=100, recurring=False, dated=True, **kw):
    g = {"id": gid, "status": status, "title": "t", "recurring": recurring,
         "description": "x" * 300}
    if dated and days_ago is not None:
        g["completed_at"] = (NOW - timedelta(days=days_ago)).strftime(
            "%Y-%m-%dT%H:%M:%S")
    g.update(kw)
    return g


def _fixture_asp():
    return {
        "id": "asp-115", "status": "active", "initial_goal_count": 4,
        "goals": [
            _goal("g-1", "completed", days_ago=100),     # EVICT
            _goal("g-2", "completed", days_ago=5),       # keep (recent)
            _goal("g-3", "skipped", days_ago=100),       # EVICT
            _goal("g-4", "superseded", days_ago=100),    # EVICT
            _goal("g-5", "expired", days_ago=100),       # EVICT
            _goal("g-6", "decomposed", days_ago=100),    # EVICT
            _goal("g-7", "pending", days_ago=None),      # keep (non-terminal)
            _goal("g-8", "blocked", days_ago=None),      # keep (non-terminal)
            _goal("g-9", "in-progress", days_ago=None),  # keep (non-terminal)
            _goal("g-10", "completed", days_ago=200, recurring=True),  # keep (recurring)
            _goal("g-11", "completed", dated=False),     # keep (undateable -> skip)
        ],
    }


# ---------------------------------------------------------------------------
# Drift guard: the four definition sites of the abandoned-status set must agree.
# ---------------------------------------------------------------------------

def test_abandoned_status_set_no_drift():
    asp_mod = _load("aspirations", "aspirations.py")
    pulse = _load("strategic_pulse_detectors", "strategic-pulse-detectors.py")
    precheck = _load("precheck_eval", "precheck-eval.py")

    canonical = set(census.ABANDONED_STATUSES)
    assert canonical == set(asp_mod.TERMINAL_GOAL_STATUSES) - {"completed"}
    assert canonical == set(pulse.ABANDONED_STATUSES)
    assert canonical == set(precheck.TERMINAL_STATUSES) - {"completed"}
    assert set(census.TERMINAL_STATUSES) == set(asp_mod.TERMINAL_GOAL_STATUSES)


# ---------------------------------------------------------------------------
# effective_counts is a pass-through when there is no census.
# ---------------------------------------------------------------------------

def test_effective_counts_passthrough_no_census():
    asp = _fixture_asp()
    # scorer "active": exclude abandoned, recurring kept.
    # live: completed g-1,g-2,g-10(recurring),g-11 ; pending g-7 ; blocked g-8 ;
    #       in-progress g-9  -> total 7, completed 4
    total, done = census.effective_counts(
        asp, exclude_statuses=census.ABANDONED_STATUSES, include_recurring=True)
    assert (total, done) == (7, 4)
    # non_recurring (recompute_progress): exclude recurring, all statuses.
    # 11 goals, g-10 recurring excluded -> 10 ; completed g-1,g-2,g-11 -> 3
    total, done = census.effective_counts(asp, include_recurring=False)
    assert (total, done) == (10, 3)


# ---------------------------------------------------------------------------
# The core promise: eviction leaves every completion metric byte-identical.
# ---------------------------------------------------------------------------

def test_eviction_metric_invariance():
    items = [_fixture_asp()]
    before = {a["id"]: _evict._metric_fingerprint(a) for a in items}

    # Independent hand-check of the BEFORE fingerprint so a systematic
    # effective_counts bug can't make before==after vacuously.
    assert before["asp-115"]["scorer_active"] == (7, 4)
    assert before["asp-115"]["non_recurring"] == (10, 3)

    _evict._make_evictor(CUTOFF)(items)

    after = {a["id"]: _evict._metric_fingerprint(a) for a in items}
    assert before == after, (
        f"eviction changed a metric:\n before={before}\n after ={after}")

    # And the eviction actually happened: 5 goals moved to the census.
    asp = items[0]
    live_ids = {g["id"] for g in asp["goals"]}
    assert live_ids == {"g-2", "g-7", "g-8", "g-9", "g-10", "g-11"}
    assert asp["archived_census"]["by_status"] == {
        "completed": 1, "skipped": 1, "superseded": 1,
        "expired": 1, "decomposed": 1,
    }


def test_modifier_aborts_on_metric_drift(monkeypatch):
    """If the census bump is wrong, the modifier must RAISE (never write a
    corrupt queue). Simulate by neutering _bump_census."""
    items = [_fixture_asp()]
    monkeypatch.setattr(_evict, "_bump_census", lambda asp, status: None)
    with pytest.raises(RuntimeError, match="changed a completion metric"):
        _evict._make_evictor(CUTOFF)(items)


def test_eviction_idempotent():
    items = [_fixture_asp()]
    _evict._make_evictor(CUTOFF)(items)
    after_first = {a["id"]: _evict._metric_fingerprint(a) for a in items}
    live_after_first = [dict(g) for g in items[0]["goals"]]
    census_after_first = dict(items[0]["archived_census"]["by_status"])

    # Second run: nothing newly aged -> no further eviction, metrics unchanged.
    _evict._make_evictor(CUTOFF)(items)
    after_second = {a["id"]: _evict._metric_fingerprint(a) for a in items}
    assert after_first == after_second
    assert [dict(g) for g in items[0]["goals"]] == live_after_first
    assert items[0]["archived_census"]["by_status"] == census_after_first


def test_recurring_never_evicted():
    asp = {"id": "a", "goals": [_goal("r", "completed", days_ago=999, recurring=True)]}
    _evict._make_evictor(CUTOFF)([asp])
    assert [g["id"] for g in asp["goals"]] == ["r"]
    assert census.CENSUS_KEY not in asp or not asp[census.CENSUS_KEY].get("by_status")


def test_undateable_terminal_skipped():
    asp = {"id": "a", "goals": [_goal("u", "completed", dated=False)]}
    _evict._make_evictor(CUTOFF)([asp])
    assert [g["id"] for g in asp["goals"]] == ["u"]


def test_census_completed_helper():
    asp = {"archived_census": {"by_status": {"completed": 3, "skipped": 2}}}
    assert census.census_completed(asp) == 3
    assert census.census_completed({}) == 0
    assert census.census_completed({"archived_census": {"by_status": {}}}) == 0
    # garbage tolerance
    assert census.census_completed({"archived_census": "junk"}) == 0


def test_precheck_tracked_counts_superseded():
    """precheck's 'tracked' denominator counts superseded (unlike the scorer)."""
    asp = _fixture_asp()
    excl = frozenset({"skipped", "expired", "decomposed"})
    before = census.effective_counts(asp, exclude_statuses=excl, include_recurring=True)
    _evict._make_evictor(CUTOFF)([asp])
    after = census.effective_counts(asp, exclude_statuses=excl, include_recurring=True)
    assert before == after
    # superseded IS counted in this denominator (unlike the scorer's): tracked =
    # g-1(c),g-2(c),g-4(superseded),g-7,g-8,g-9,g-10(c recurring),g-11(c)
    # -> total 8 ; completed = g-1,g-2,g-10,g-11 -> 4 (g-4 is superseded, not done)
    assert before == (8, 4)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
