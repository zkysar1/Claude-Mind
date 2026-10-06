""" — `load_experiences(goal_match=...)` unions the goal's own records.

A recurring goal files each run under that run's topic, so its history is
scattered across the one dimension retrieval indexes on: measured on a goal
with 9 records under 8 categories, a category query reached 1-2 of them while
`experience-read --goal` returned all 9. `retrieve.sh --goal` then returned the
same experiences as without `--goal`, and a lesson already on disk twice was
re-derived from scratch twice.

`load_experiences` now takes an optional `goal_match` predicate. The daemon
endpoint injects the experience-read `--goal` predicate (core cannot import the
id derivation: daemon import surface). These tests pin the LOADER half:

  * the union surfaces own records whatever their category
  * it is ADDITIVE: the category selection is never trimmed or reordered
  * no predicate => the result is exactly what it was (the no-goal path)
  * an archived own record stays out, a record that is both own and
    category-matched appears once, another goal's record never rides in, and
    the own lane is capped at the depth limit
  * read_only still means no counter bump, and a non-read-only call spools the
    own records exactly as it spools any other returned record

The ENDPOINT half (the injected predicate, the real id derivation, parity with
`/v1/experience/read?goal=`, the session manifest) is
mind_api/tests/test_retrieve_goal_experiences.py.

Reachable-red (guard-1475): with `goal_match` ignored in load_experiences,
test_union_surfaces_own_records_whatever_their_category and
test_union_returns_own_history_when_no_category_matches FAIL.
"""

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _experience_stats_spool as es  # noqa: E402

GOAL = "g-901-07"
QUERY_CAT = "target-cat"


@pytest.fixture(autouse=True)
def _local_backend(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "local")


def _rec(rid, category, created, goal_id=None, retrieval_count=0, **extra):
    r = {"id": rid, "type": "goal_execution", "category": category,
         "summary": "seed " + rid, "goal_id": goal_id, "created": created,
         "archived": False,
         "retrieval_stats": {"retrieval_count": retrieval_count,
                             "times_useful": 0, "times_noise": 0,
                             "utility_ratio": 0.0, "last_retrieved": None}}
    r.update(extra)
    return r


def _write_store(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records),
                    encoding="utf-8")


def _mine(rec):
    return rec.get("goal_id") == GOAL


def _ids(rows):
    return [r["id"] for r in rows]


def _load_retrieve(tmp_path):
    """Load retrieve.py against a tmp world with no bound agent (the same env
    guard test_experience_stats_spool uses), then the caller points EXP_PATH at
    a tmp store."""
    orig = {k: os.environ.get(k) for k in ("MIND_WORLD", "MIND_AGENT")}
    world = tmp_path / "world"
    world.mkdir()
    os.environ["MIND_WORLD"] = str(world)
    os.environ.pop("MIND_AGENT", None)
    try:
        spec = importlib.util.spec_from_file_location(
            "retrieve_exp_goal_union_test", SCRIPTS / "retrieve.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in orig.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture
def store(tmp_path):
    """One recurring goal whose runs sit under four different categories, none
    of them the query category, plus every record the union must NOT return."""
    path = tmp_path / "experience.jsonl"
    _write_store(path, [
        # the goal's own history: four runs, four categories, none = QUERY_CAT
        _rec("exp-g-901-07-r1", "cat-one", "2026-07-01T10:00:00", GOAL),
        _rec("exp-g-901-07-r2", "cat-two", "2026-07-02T10:00:00", GOAL),
        _rec("exp-g-901-07-r3", "cat-three", "2026-07-03T10:00:00", GOAL),
        _rec("exp-g-901-07-r4", "cat-four", "2026-07-04T10:00:00", GOAL),
        # own AND category-matched, and the most-proven category record
        _rec("exp-g-901-07-both", QUERY_CAT, "2026-07-05T10:00:00", GOAL,
             retrieval_count=9),
        # own but archived: the live-only rule still holds
        _rec("exp-g-901-07-old", "cat-five", "2026-06-01T10:00:00", GOAL,
             archived=True),
        # category matches for other goals
        _rec("exp-cat-a", QUERY_CAT, "2026-07-06T10:00:00", "g-902-01",
             retrieval_count=5),
        _rec("exp-cat-b", QUERY_CAT, "2026-07-07T10:00:00", "g-902-02",
             retrieval_count=3),
        _rec("exp-cat-c", QUERY_CAT, "2026-07-08T10:00:00", "g-902-03",
             retrieval_count=1),
        # another goal, another category: must never ride in
        _rec("exp-g-902-01-x", "cat-six", "2026-07-09T10:00:00", "g-902-01"),
    ])
    return path


@pytest.fixture
def r(tmp_path, store, monkeypatch):
    mod = _load_retrieve(tmp_path)
    monkeypatch.setattr(mod, "EXP_PATH", store)
    return mod


# --- the no-goal path is unchanged -------------------------------------------

def test_no_predicate_is_the_unchanged_category_selection(r):
    got = r.load_experiences([QUERY_CAT], "medium", read_only=True)
    # most-proven first, own-only records absent
    assert _ids(got) == ["exp-g-901-07-both", "exp-cat-a", "exp-cat-b",
                         "exp-cat-c"]
    assert r.load_experiences([QUERY_CAT], "medium", read_only=True,
                              goal_match=None) == got


# --- the union ---------------------------------------------------------------

def test_union_surfaces_own_records_whatever_their_category(r):
    base = _ids(r.load_experiences([QUERY_CAT], "medium", read_only=True))
    got = _ids(r.load_experiences([QUERY_CAT], "medium", read_only=True,
                                  goal_match=_mine))
    own_only = ["exp-g-901-07-r1", "exp-g-901-07-r2", "exp-g-901-07-r3",
                "exp-g-901-07-r4"]
    # ANTI-VACUITY: none of the four is reachable by the category query alone
    assert not set(own_only) & set(base)
    assert set(own_only) <= set(got)


def test_union_returns_own_history_when_no_category_matches(r):
    """The headline shape: any query, even one whose category matches nothing,
    reaches the goal's own live history."""
    got = r.load_experiences(["zzz-no-such-category"], "medium",
                             read_only=True, goal_match=_mine)
    assert set(_ids(got)) == {"exp-g-901-07-r1", "exp-g-901-07-r2",
                              "exp-g-901-07-r3", "exp-g-901-07-r4",
                              "exp-g-901-07-both"}


def test_union_is_additive_ahead_and_newest_first(r):
    base = _ids(r.load_experiences([QUERY_CAT], "medium", read_only=True))
    got = _ids(r.load_experiences([QUERY_CAT], "medium", read_only=True,
                                  goal_match=_mine))
    # the category selection survives whole, in its own order, at the tail
    assert got[-len(base):] == base
    # own records the category selection did not take come first, newest first
    assert got[:-len(base)] == ["exp-g-901-07-r4", "exp-g-901-07-r3",
                                "exp-g-901-07-r2", "exp-g-901-07-r1"]


def test_a_record_both_own_and_category_matched_appears_once(r):
    got = _ids(r.load_experiences([QUERY_CAT], "medium", read_only=True,
                                  goal_match=_mine))
    assert got.count("exp-g-901-07-both") == 1


def test_archived_own_records_stay_out(r):
    got = _ids(r.load_experiences(["cat-five"], "medium", read_only=True,
                                  goal_match=_mine))
    assert "exp-g-901-07-old" not in got


def test_other_goals_records_do_not_ride_in(r):
    got = _ids(r.load_experiences([QUERY_CAT], "medium", read_only=True,
                                  goal_match=_mine))
    assert "exp-g-902-01-x" not in got


def test_own_lane_is_capped_at_the_depth_limit(tmp_path, monkeypatch):
    mod = _load_retrieve(tmp_path)
    limit = mod.EXP_LIMITS["shallow"]
    n = limit + 7
    path = tmp_path / "many.jsonl"
    _write_store(path, [
        _rec("exp-g-901-07-n%02d" % i, "cat-%02d" % i,
             "2026-08-01T00:%02d:00" % i, GOAL)
        for i in range(n)])
    monkeypatch.setattr(mod, "EXP_PATH", path)

    got = mod.load_experiences(["zzz-no-such-category"], "shallow",
                               read_only=True, goal_match=_mine)

    # the `limit` NEWEST, newest first
    assert _ids(got) == ["exp-g-901-07-n%02d" % i
                         for i in range(n - 1, n - 1 - limit, -1)]


def test_category_selection_survives_whole_when_the_union_exceeds_the_limit(
        tmp_path, monkeypatch):
    """Additive means ADDITIVE: with the category selection already at the
    depth limit, adding own records must not push any of it out. (A seed whose
    combined size stays under the limit cannot tell a trimmed union from an
    untrimmed one — this one is built to exceed it.)"""
    mod = _load_retrieve(tmp_path)
    limit = mod.EXP_LIMITS["shallow"]
    path = tmp_path / "full.jsonl"
    cat = [_rec("exp-c%02d" % i, QUERY_CAT, "2026-07-01T00:%02d:00" % i,
                "g-902-%02d" % i, retrieval_count=100 - i)
           for i in range(limit + 3)]
    own = [_rec("exp-g-901-07-o%d" % i, "cat-o%d" % i,
                "2026-08-01T00:%02d:00" % i, GOAL) for i in range(6)]
    _write_store(path, cat + own)
    monkeypatch.setattr(mod, "EXP_PATH", path)

    base = _ids(mod.load_experiences([QUERY_CAT], "shallow", read_only=True))
    got = _ids(mod.load_experiences([QUERY_CAT], "shallow", read_only=True,
                                    goal_match=_mine))

    assert len(base) == limit, "the category selection must already be full"
    assert got[-limit:] == base, "own records pushed category matches out"
    assert len(got) == limit + len(own)


# --- counters ----------------------------------------------------------------

def _fresh_stamp(base):
    # Arm the spool's interval gate so the flush defers (see the model test,
    # test_experience_stats_spool._fresh_stamp): the drain is not under test.
    (base / es.SPOOL_DIR_NAME).mkdir(parents=True, exist_ok=True)
    (base / es.SPOOL_DIR_NAME / es.STAMP_NAME).write_text(str(time.time()))


def test_read_only_union_bumps_nothing(r, store):
    _fresh_stamp(store.parent)
    before = store.read_bytes()

    r.load_experiences([QUERY_CAT], "medium", read_only=True,
                       goal_match=_mine)

    assert store.read_bytes() == before
    assert not (es.spool_dir(store) / es.SPOOL_NAME).exists()


def test_own_records_are_spooled_like_any_returned_record(r, store):
    _fresh_stamp(store.parent)
    before = store.read_bytes()

    got = r.load_experiences([QUERY_CAT], "medium", goal_match=_mine)

    assert store.read_bytes() == before, "a retrieval must not rewrite the store"
    spooled = sorted(json.loads(line)["id"] for line in
                     (es.spool_dir(store) / es.SPOOL_NAME).read_text()
                     .splitlines())
    assert spooled == sorted(_ids(got))
    assert "exp-g-901-07-r1" in spooled
