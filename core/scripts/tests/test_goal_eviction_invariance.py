"""B9-deep: goal eviction is metric-NEUTRAL.

aspirations-evict-completed.py removes aged terminal non-recurring goals from the
live `goals` list and caches their per-status counts in `archived_census`. Every
completion consumer reads through _goal_census.effective_counts, which folds the
census back in. This test proves the core promise: every completion denominator
the codebase uses is byte-identical before and after eviction, for a fixture that
exercises all four variants plus the cadence completed-count.

Pure unit test of the evictor modifier + the census helper. No S3, no daemon."""
import importlib.util
import json
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
    # 0: eviction records IDS (merge-correct, tombstoning); the
    # derived per-status counts are unchanged, legacy by_status untouched.
    asp = items[0]
    live_ids = {g["id"] for g in asp["goals"]}
    assert live_ids == {"g-2", "g-7", "g-8", "g-9", "g-10", "g-11"}
    assert census.census_by_status(asp) == {
        "completed": 1, "skipped": 1, "superseded": 1,
        "expired": 1, "decomposed": 1,
    }
    ids = asp["archived_census"]["evicted_ids"]
    assert {s: len(v) for s, v in ids.items()} == {
        "completed": 1, "skipped": 1, "superseded": 1,
        "expired": 1, "decomposed": 1,
    }
    assert "by_status" not in asp["archived_census"]


def test_modifier_aborts_on_metric_drift(monkeypatch):
    """If the census bump is wrong, the modifier must RAISE (never write a
    corrupt queue). Simulate by neutering _bump_census."""
    items = [_fixture_asp()]
    monkeypatch.setattr(_evict, "_bump_census", lambda asp, status, goal_id: None)
    with pytest.raises(RuntimeError, match="changed a completion metric"):
        _evict._make_evictor(CUTOFF)(items)


def test_eviction_idempotent():
    items = [_fixture_asp()]
    _evict._make_evictor(CUTOFF)(items)
    after_first = {a["id"]: _evict._metric_fingerprint(a) for a in items}
    live_after_first = [dict(g) for g in items[0]["goals"]]
    census_after_first = json.loads(
        json.dumps(items[0]["archived_census"]["evicted_ids"]))

    # Second run: nothing newly aged -> no further eviction, metrics unchanged.
    _evict._make_evictor(CUTOFF)(items)
    after_second = {a["id"]: _evict._metric_fingerprint(a) for a in items}
    assert after_first == after_second
    assert [dict(g) for g in items[0]["goals"]] == live_after_first
    assert items[0]["archived_census"]["evicted_ids"] == census_after_first


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


# ---------------------------------------------------------------------------
# 8: cross-run conservation canary (pigeonhole invariant).
# A stale write resurrecting goals[] AFTER a census bump leaves the same goals
# counted twice (asp-306 signature). The evictor must REFUSE such aspirations —
# evicting from a resurrected state re-bumps census, compounding the double-count.
# ---------------------------------------------------------------------------

def _seq_goal(gid, days_ago=100, status="completed"):
    return _goal(gid, status, days_ago=days_ago)


def _violating_asp():
    """asp-306 signature in miniature: 5 sequence goals in-list (max seq 5)
    PLUS census=3 -> 8 allocations claimed against capacity 5, excess 3."""
    return {
        "id": "asp-306", "status": "active",
        "goals": [_seq_goal(f"g-306-0{i}") for i in range(1, 6)],
        census.CENSUS_KEY: {"by_status": {"completed": 3}},
    }


def test_conservation_violation_detected():
    v = _evict._conservation_violation(_violating_asp())
    assert v is not None
    assert v["asp_id"] == "asp-306"
    assert (v["goals_in_list"], v["census_sum"]) == (5, 3)
    assert (v["max_minted_seq"], v["capacity"], v["excess"]) == (5, 5, 3)


def test_conservation_clean_states_pass():
    # Healthy post-eviction state: 2 in-list of max seq 5, census 3 -> 5 <= 5.
    clean = {"id": "asp-306",
             "goals": [_seq_goal("g-306-04"), _seq_goal("g-306-05")],
             census.CENSUS_KEY: {"by_status": {"completed": 3}}}
    assert _evict._conservation_violation(clean) is None
    # No census at all -> in-list count can never exceed max seq.
    no_census = {"id": "asp-306", "goals": [_seq_goal("g-306-01")]}
    assert _evict._conservation_violation(no_census) is None
    # Non-sequence ids (legacy fixtures) -> capacity underivable -> None.
    assert _evict._conservation_violation(_fixture_asp()) is None
    assert _evict._conservation_violation({"id": "a", "goals": []}) is None


def test_conservation_suffixed_ids_extend_capacity():
    # 3 in-list, max seq 2, one suffixed re-key -> capacity 3: clean at census 0.
    goals = [_seq_goal("g-306-01"), _seq_goal("g-306-02"),
             _seq_goal("g-306-02-a")]
    asp = {"id": "asp-306", "goals": goals}
    assert _evict._conservation_violation(asp) is None
    # ... but census 1 pushes it over (4 > 3).
    asp[census.CENSUS_KEY] = {"by_status": {"completed": 1}}
    v = _evict._conservation_violation(asp)
    assert v and v["excess"] == 1 and v["suffixed_extra"] == 1


def test_conservation_foreign_ids_excluded():
    # Goals whose asp-number prefix doesn't match sit OUTSIDE the sequence
    # space — not counted, and their seq doesn't inflate capacity.
    asp = {"id": "asp-306",
           "goals": [_seq_goal("g-115-1937"), _seq_goal("g-306-01")],
           census.CENSUS_KEY: {"by_status": {"completed": 2}}}
    # counted=1 (), max_seq=1, capacity=1, census=2 -> violation 2.
    v = _evict._conservation_violation(asp)
    assert v and (v["goals_in_list"], v["excess"]) == (1, 2)


# ---------------------------------------------------------------------------
# 3: legacy count-only census (no recorded evicted_ids) makes the
# pigeonhole capacity a KNOWN-loose lower bound — it cannot observe the seqs or
# suffix-letters of goals evicted before 0 id-tracking, so it
# false-flags legitimate eviction. Suppress that regime (not-all-live) while
# PRESERVING the reliable all-live resurrection signature.
# ---------------------------------------------------------------------------

def _legacy_loose_asp():
    """Legacy count-only census + not-all-live: max seq 10 but only 2 live ->
    capacity 10, counted 2; census 9 -> claimed 11 > 10 (excess 1). The excess
    is capacity-undercount noise (8 low-seq ids evicted before id-tracking are
    invisible), NOT resurrection."""
    return {
        "id": "asp-306", "status": "active",
        "goals": [_seq_goal("g-306-01"), _seq_goal("g-306-10")],
        census.CENSUS_KEY: {"by_status": {"completed": 9}},
    }


def test_conservation_legacy_census_loose_suppressed_g_115_2503():
    # (a) Legacy count-only census + not-all-live -> capacity is a loose lower
    #     bound, so the pigeonhole excess is undercount noise -> SUPPRESS.
    asp = _legacy_loose_asp()
    assert _evict._conservation_violation(asp) is None
    assert _evict._audit_violations([asp]) == []

    # (b) The all-live resurrection signature (counted == capacity, census > 0)
    #     is STILL detected even with empty evicted_ids -> the guard is specific
    #     to not-all-live, never blinds the genuine asp-306 double-count.
    assert _evict._conservation_violation(_violating_asp()) is not None
    assert len(_evict._audit_violations([_violating_asp()])) == 1

    # (c) The guard predicate itself: True only when evicted_ids is empty AND
    #     not-all-live; False for all-live (in_list == capacity) and False once
    #     evicted_ids is populated (capacity becomes observable/tight).
    assert _evict._legacy_census_loose(asp, 2, 10) is True
    assert _evict._legacy_census_loose(_violating_asp(), 5, 5) is False
    tight = _legacy_loose_asp()
    tight[census.CENSUS_KEY] = {
        "evicted_ids": {"completed": [f"g-306-0{i}" for i in range(2, 10)]}}
    assert _evict._legacy_census_loose(tight, 2, 10) is False


def test_evictor_refuses_violating_asp_but_evicts_clean_sibling():
    bad = _violating_asp()
    good = _fixture_asp()
    bad_goals_before = [g["id"] for g in bad["goals"]]
    bad_census_before = json.loads(json.dumps(bad[census.CENSUS_KEY]))
    _evict._make_evictor(CUTOFF)([bad, good])
    # Violating asp untouched: goals kept, census NOT re-bumped.
    assert [g["id"] for g in bad["goals"]] == bad_goals_before
    assert bad[census.CENSUS_KEY] == bad_census_before
    # Clean sibling still evicted normally (skip is per-asp, not global).
    assert {g["id"] for g in good["goals"]} == {"g-2", "g-7", "g-8", "g-9",
                                                "g-10", "g-11"}


def test_plan_excludes_violating_asp():
    bad = _violating_asp()
    good = _fixture_asp()
    report, total_n, freed, violations = _evict._plan(
        [bad, good], CUTOFF)
    assert [v["asp_id"] for v in violations] == ["asp-306"]
    assert [r[0] for r in report] == ["asp-115"]  # bad asp not planned
    assert total_n == 5  # good asp's 5 eligible goals only


def test_audit_mode_reports_without_mutating(tmp_path, monkeypatch, capsys):
    world = tmp_path / "world"
    world.mkdir()
    path = world / "aspirations.jsonl"
    blob = "\n".join(json.dumps(a, ensure_ascii=True)
                     for a in (_violating_asp(), _fixture_asp())) + "\n"
    path.write_text(blob, encoding="utf-8")
    monkeypatch.setattr(_evict, "WORLD_DIR", world)
    monkeypatch.setattr(sys, "argv", ["aspirations-evict-completed.py",
                                      "--audit"])
    rc = _evict.main()
    assert rc == 1  # violation found
    assert path.read_text(encoding="utf-8") == blob  # read-only sweep
    # --audit reports on stdout (1 contract, adopted at the
    # 2026-07-11 origin merge over the 8 stderr machine-line form —
    # no consumers grep the old CONSERVATION-VIOLATION stderr line).
    out = capsys.readouterr().out
    assert "CONSERVATION VIOLATION" in out and "asp-306" in out
    # Clean store -> exit 0.
    path.write_text(json.dumps(_fixture_asp(), ensure_ascii=True) + "\n",
                    encoding="utf-8")
    assert _evict.main() == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
