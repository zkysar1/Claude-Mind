""": merge-correct evicted-ids census — kills both  phantom
producers.

FAST lane (killed): archived_census rode opaque LWW-by-`last_selected` in
_merge_aspiration_record, so a census repair reverted within ~81min whenever a
stale peer's copy won selection-time LWW. Now the census merges with EXPLICIT
per-field semantics (_merge_archived_census): `evicted_ids` unions (commutative,
idempotent), legacy `by_status` takes per-status MIN (repairs shrink; min
converges to most-repaired), never LWW (guard-1153).

SLOW lane (killed): _merge_goals unioned by id with no tombstone (guard-1072),
so an evicted goal resurrected from any stale replica and the next evict
re-bumped the census (double count). Now the evicted-id set doubles as the
tombstone — _merge_goals drops any goal carrying an evicted id — and
_bump_census is a set-add (re-evict = no-op).

Supporting invariants pinned here: mint sites allocate max+1 over live ∪
evicted ids (re-mint of an evicted seq would be tombstone-killed); displacement
re-id skips evicted seqs; capacity/conservation count evicted ids (no false
violation after evicting the max-seq goal); repair clamps LEGACY counts only
and keeps explicit zeros (so the MIN-merge propagates the shrink); legacy-only
censuses read identically (migration invariance).

Pure unit test of _goal_census + coordination_merge + the evictor modifier +
the daemon allocator. No S3, no daemon process."""
import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]  # core/scripts
PROJECT_ROOT = SCRIPTS.parents[1]
for p in (str(SCRIPTS), str(PROJECT_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _load(mod_name, file_name):
    spec = importlib.util.spec_from_file_location(mod_name, str(SCRIPTS / file_name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


import _goal_census as census  # noqa: E402
import coordination_merge as cm  # noqa: E402
_evict = _load("aspirations_evict_completed_g2430", "aspirations-evict-completed.py")

NOW = datetime(2026, 6, 1, 12, 0, 0)
CUTOFF = NOW - timedelta(days=45)
OLD = "2026-01-02T00:00:00"   # far past the cutoff


def _goal(gid, status, completed_at=None, **kw):
    g = {"id": gid, "status": status, "title": f"t-{gid}", "recurring": False,
         "created_at": "2026-01-01T00:00:00"}
    if completed_at:
        g["completed_at"] = completed_at
    g.update(kw)
    return g


def _blob(*asps):
    return ("\n".join(json.dumps(a, ensure_ascii=True) for a in asps) + "\n").encode()


def _one(merged_bytes):
    lines = [ln for ln in merged_bytes.decode().splitlines() if ln.strip()]
    assert len(lines) == 1
    return json.loads(lines[0])


# ---------------------------------------------------------------- _goal_census

def test_census_by_status_derives_legacy_plus_ids():
    asp = {"id": "asp-001", "goals": [], "archived_census": {
        "by_status": {"completed": 5, "skipped": 1},
        "evicted_ids": {"completed": ["g-001-01", "g-001-02"], "expired": ["g-001-03"]},
    }}
    assert census.census_by_status(asp) == {"completed": 7, "skipped": 1, "expired": 1}
    assert census.all_evicted_ids(asp) == ["g-001-01", "g-001-02", "g-001-03"]


def test_migration_invariance_legacy_only():
    # A pre-cutover census (counts only) must read EXACTLY as before.
    asp = {"id": "asp-002", "archived_census": {"by_status": {"completed": 9, "skipped": 2}},
           "goals": [_goal("g-002-11", "completed"), _goal("g-002-12", "pending")]}
    assert census.census_by_status(asp) == {"completed": 9, "skipped": 2}
    assert census.census_evicted_ids(asp) == {}
    total, done = census.effective_counts(asp)
    assert (total, done) == (2 + 11, 1 + 9)


def test_census_tolerates_garbage_shapes():
    for garbage in (None, "x", 7, {"evicted_ids": "nope"},
                    {"evicted_ids": {"completed": "not-a-list"}},
                    {"evicted_ids": {"completed": None}}):
        assert census.census_evicted_ids({"archived_census": garbage}) == {}
        assert census.all_evicted_ids({"archived_census": garbage}) == []


# ------------------------------------------------------------------ _bump_census

def test_bump_census_id_append_dedup_and_frozen_legacy():
    asp = {"id": "asp-014", "goals": []}
    _evict._bump_census(asp, "completed", "g-014-02")
    _evict._bump_census(asp, "completed", "g-014-01")
    _evict._bump_census(asp, "completed", "g-014-02")   # re-evict: no-op
    _evict._bump_census(asp, "skipped", "g-014-03")
    c = asp["archived_census"]
    assert c["evicted_ids"]["completed"] == ["g-014-01", "g-014-02"]  # sorted, deduped
    assert c["evicted_ids"]["skipped"] == ["g-014-03"]
    assert "by_status" not in c                      # legacy baseline untouched
    assert census.census_by_status(asp) == {"completed": 2, "skipped": 1}


# ------------------------------------------------------- _merge_archived_census

def test_merge_census_union_commutative_idempotent():
    a = {"evicted_ids": {"completed": ["g-1-02", "g-1-01"]}, "by_status": {"completed": 4}}
    b = {"evicted_ids": {"completed": ["g-1-03", "g-1-01"], "skipped": ["g-1-04"]}}
    m1 = cm._merge_archived_census(a, b)
    m2 = cm._merge_archived_census(b, a)
    assert m1 == m2
    assert m1["evicted_ids"] == {"completed": ["g-1-01", "g-1-02", "g-1-03"],
                                 "skipped": ["g-1-04"]}
    assert m1["by_status"] == {"completed": 4}       # one-sided key kept verbatim
    # idempotent fixpoint: re-merging the result with either input is stable
    assert cm._merge_archived_census(m1, a) == m1
    assert cm._merge_archived_census(m1, m1) == m1


def test_merge_census_legacy_min_and_explicit_zero():
    # Repair shrank A to 10; stale peer still carries 28 — MIN keeps the repair.
    a = {"by_status": {"completed": 10, "skipped": 0}}
    b = {"by_status": {"completed": 28, "skipped": 2, "expired": 3}}
    m = cm._merge_archived_census(a, b)
    assert m == cm._merge_archived_census(b, a)
    assert m["by_status"] == {"completed": 10, "skipped": 0, "expired": 3}


def test_merge_census_one_sided_and_absent():
    only = {"evicted_ids": {"completed": ["g-2-02", "g-2-01", "g-2-01"]}}
    m = cm._merge_archived_census(only, None)
    assert m == cm._merge_archived_census(None, only)
    assert m["evicted_ids"] == {"completed": ["g-2-01", "g-2-02"]}  # normalized
    assert cm._merge_archived_census(None, None) is None


# ------------------------------------------------- full-record merge + tombstone

def test_lww_reversion_regression_census_survives_stale_selection_winner():
    """THE  fast lane: stale peer wins LWW via newer last_selected;
    pre-fix the whole census reverted to the stale copy. Now: union survives."""
    a = {"id": "asp-009", "title": "x", "goals": [], "last_selected": "2026-06-01T00:00:00",
         "archived_census": {"by_status": {"completed": 10},
                             "census_note": "reconciled g-115-1951"}}
    b = {"id": "asp-009", "title": "x", "goals": [], "last_selected": "2026-06-05T00:00:00",
         "archived_census": {"by_status": {"completed": 28}}}
    m1, m2 = cm.merge_aspirations(_blob(a), _blob(b)), cm.merge_aspirations(_blob(b), _blob(a))
    assert m1 == m2
    rec = _one(m1)
    assert rec["archived_census"]["by_status"] == {"completed": 10}
    assert "census_note" in rec["archived_census"]


def test_lose_side_only_census_is_not_dropped():
    # Census exists ONLY on the LWW-losing side — pre-fix it vanished entirely.
    a = {"id": "asp-010", "title": "x", "goals": [], "last_selected": "2026-06-01T00:00:00",
         "archived_census": {"evicted_ids": {"completed": ["g-010-01"]}}}
    b = {"id": "asp-010", "title": "x", "goals": [], "last_selected": "2026-06-09T00:00:00"}
    rec = _one(cm.merge_aspirations(_blob(a), _blob(b)))
    assert rec["archived_census"]["evicted_ids"] == {"completed": ["g-010-01"]}


def test_tombstone_drops_resurrected_goal_both_directions():
    """THE  slow lane: stale replica still carries an evicted goal
    live; the id-union used to resurrect it. Now the evicted-id set drops it."""
    evicted_goal = _goal("g-007-01", "completed", completed_at=OLD)
    live_goal = _goal("g-007-02", "pending")
    a = {"id": "asp-007", "title": "x", "last_selected": "2026-06-01T00:00:00",
         "goals": [live_goal],
         "archived_census": {"evicted_ids": {"completed": ["g-007-01"]}}}
    b = {"id": "asp-007", "title": "x", "last_selected": "2026-06-02T00:00:00",
         "goals": [dict(live_goal), dict(evicted_goal)]}   # stale: still live
    m1, m2 = cm.merge_aspirations(_blob(a), _blob(b)), cm.merge_aspirations(_blob(b), _blob(a))
    assert m1 == m2
    rec = _one(m1)
    assert [g["id"] for g in rec["goals"]] == ["g-007-02"]
    assert rec["archived_census"]["evicted_ids"] == {"completed": ["g-007-01"]}


def test_displaced_reid_skips_evicted_seqs():
    # Two DISTINCT goals collide on g-9-03; seq 4 is evicted — the displaced
    # goal must land on 5, never on the tombstoned 4.
    g1 = _goal("g-9-03", "pending", created_at="2026-01-01T00:00:00")
    g2 = _goal("g-9-03", "pending", created_at="2026-02-01T00:00:00", title="other")
    merged = cm._merge_goals([g1], [g2], "9", evicted_ids=frozenset({"g-9-04"}))
    ids = sorted(g["id"] for g in merged)
    assert ids == ["g-9-03", "g-9-05"]
    displaced = [g for g in merged if g["id"] == "g-9-05"][0]
    assert displaced["displaced_from"] == "g-9-03"


def test_merge_goals_default_signature_unchanged():
    # Callers/tests without evicted_ids keep the exact pre-change behavior.
    g1 = _goal("g-9-01", "pending")
    g2 = _goal("g-9-02", "pending")
    merged = cm._merge_goals([g1], [g2], "9")
    assert sorted(g["id"] for g in merged) == ["g-9-01", "g-9-02"]


# ------------------------------------------------ evict → merge → re-evict cycle

def test_evict_merge_reevict_conservation():
    aged = _goal("g-011-01", "completed", completed_at=OLD)
    live = _goal("g-011-02", "pending")
    asp = {"id": "asp-011", "title": "t", "goals": [dict(aged), dict(live)]}
    out = _evict._make_evictor(CUTOFF)([asp])
    a_post = out[0]
    assert [g["id"] for g in a_post["goals"]] == ["g-011-02"]
    assert a_post["archived_census"]["evicted_ids"] == {"completed": ["g-011-01"]}
    assert census.census_by_status(a_post) == {"completed": 1}

    # Stale peer never saw the eviction; it wins LWW via newer last_selected.
    stale = {"id": "asp-011", "title": "t", "last_selected": "2026-06-09T00:00:00",
             "goals": [dict(aged), dict(live)]}
    rec = _one(cm.merge_aspirations(_blob(a_post), _blob(stale)))
    assert [g["id"] for g in rec["goals"]] == ["g-011-02"]   # no resurrection

    # Re-evict on the merged state: set-add no-op, counts stable, audit clean.
    out2 = _evict._make_evictor(CUTOFF)([rec])
    assert census.census_by_status(out2[0]) == {"completed": 1}
    assert out2[0]["archived_census"]["evicted_ids"] == {"completed": ["g-011-01"]}
    assert _evict._audit_violations(out2) == []


def test_no_false_violation_after_max_seq_eviction():
    # Evicting the MAX-seq goal used to shrink the capacity ceiling while the
    # census grew — a built-in false positive once ids left the live list.
    lives = [_goal(f"g-012-0{i}", "pending") for i in (1, 2, 3, 4)]
    aged_max = _goal("g-012-05", "completed", completed_at=OLD)
    asp = {"id": "asp-012", "title": "t", "goals": lives + [aged_max]}
    out = _evict._make_evictor(CUTOFF)([asp])[0]
    assert census.all_evicted_ids(out) == ["g-012-05"]
    assert _evict._conservation_violation(out) is None
    assert _evict._audit_violations([out]) == []
    assert _evict._capacity(out) == 5                     # evicted seq still counted


# --------------------------------------------------------------------- repair

def test_repair_clamps_legacy_only_with_explicit_zeros():
    lives = [_goal("g-013-01", "pending"), _goal("g-013-02", "pending")]
    asp = {"id": "asp-013", "title": "t", "goals": lives,
           "archived_census": {"by_status": {"completed": 5, "skipped": 1},
                               "evicted_ids": {"completed": ["g-013-03"]}}}
    # capacity 3 (evicted seq visible), in_list 2, ids_total 1 → legacy target 0.
    out = _evict._make_census_repair("2026-06-01T00:00:00")([asp])
    c = out[0]["archived_census"]
    assert c["by_status"] == {"completed": 0, "skipped": 0}   # explicit zeros
    assert c["evicted_ids"] == {"completed": ["g-013-03"]}    # ground truth kept
    assert _evict._audit_violations(out) == []
    # The MIN-merge now propagates the repair against a stale nonzero peer:
    stale_census = {"by_status": {"completed": 5, "skipped": 1},
                    "evicted_ids": {"completed": ["g-013-03"]}}
    m = cm._merge_archived_census(c, stale_census)
    assert m["by_status"] == {"completed": 0, "skipped": 0}


def test_merge_never_crashes_on_garbage_evicted_ids():
    # fresh-eyes-code finding: a one-sided garbage-shaped census reached the
    # evicted-set build un-normalized and .values() raised AttributeError,
    # crashing the whole store merge. Garbage must degrade to no-tombstones.
    for garbage in ("GARBAGE-STRING", 7, ["list"], {"completed": "not-a-list"}):
        a = {"id": "asp-020", "title": "x", "goals": [_goal("g-020-01", "pending")],
             "archived_census": {"evicted_ids": garbage}}
        b = {"id": "asp-020", "title": "x", "goals": [_goal("g-020-01", "pending")]}
        m1 = cm.merge_aspirations(_blob(a), _blob(b))
        m2 = cm.merge_aspirations(_blob(b), _blob(a))
        assert m1 == m2
        rec = _one(m1)
        assert [g["id"] for g in rec["goals"]] == ["g-020-01"]  # nothing dropped


# ------------------------------------------------------------------ mint sites

def test_daemon_allocator_skips_evicted_seqs():
    from mind_api.src.endpoints import aspirations_write as aw
    asp = {"id": "asp-115", "goals": [{"id": "g-115-03"}],
           "archived_census": {"evicted_ids": {"completed": ["g-115-07"]}}}
    assert aw._allocate_goal_id(asp) == "g-115-08"
    assert aw._allocate_goal_id({"id": "asp-115", "goals": [{"id": "g-115-03"}]}) == "g-115-04"
