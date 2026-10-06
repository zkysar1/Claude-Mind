"""Unit tests for `plan_archive_prune` in core/scripts/_owncloud_composite.py: which composite-GC archive runs
a prune pass may remove (g-358-202 U28).

Pure-Python: no backend, no daemon, no network, no S3 double. The planner takes the archive directory's top-level
listing, the parsed receipts and the head-names-what-the-store-lacks set, so every case below builds them by hand.

Coverage:
  1. an old `done` run for this store is pruned, with its object and byte counts; a run inside retention is kept
     without a receipt; the retention boundary is inclusive
  2. what is never a candidate: the `_state` and `_pruned` directories (skipped, not even reported) and every stray
     name (reported, left alone), including near-misses of the run-id shape
  3. each reason a run stays, one test per reason, each with a positive control that prunes the same run once the
     reason is removed (so the case can fail)
  4. a run holding an object the head names while the store lacks it stays; the others go
  5. refusals: `needed` unknown, a window under the floor, a `now` that is not a number; a refusal removes nothing
  6. the cap keeps the oldest first and names the rest 'over-cap'; order follows the run's time
  7. every listed run is pruned or kept exactly once, `_state` neither; the planner does not change its inputs
  8. the names the planner reads are the ones the archive's own key builders write
"""
from __future__ import annotations

import copy
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _owncloud_composite as c  # noqa: E402

STORE = "world/stub-store.jsonl"
OTHER = "world/other-stub.jsonl"
NOW = 1_790_000_000.0
DAY = 86400.0
FLOOR_DAYS = c.GC_PRUNE_AFTER_S / DAY


def _rid(days, tag="a"):
    """A real run id for a pass that ran `days` before NOW (so the planner's name space is the writer's)."""
    return c.gc_run_id(NOW - days * DAY, [tag])


def _receipt(run, n=3, size=100, store=STORE, status="done", **over):
    """A receipt shaped like the one `composite_gc_apply` writes; its object names differ from run to run."""
    r = {"kind": "composite-gc-archive", "format": 1, "run_id": run, "store": store, "status": status,
         "objects": {"asp/%s-%d.%032x.jsonl" % (run[-8:], i, i): {"size": size, "md5": "0" * 32} for i in range(n)}}
    r.update(over)
    return r


def _plan(listing, receipts, needed=(), **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("store", STORE)
    return c.plan_archive_prune(listing, receipts, needed, **kw)


# 1. the plain case and the retention edge

def test_an_old_done_run_is_pruned_and_its_items_describe_it():
    run = _rid(20)
    plan = _plan([run], {run: _receipt(run, n=4, size=250)})
    assert plan.refused == [] and plan.prune == [run] and plan.kept == {} and plan.unknown == []
    assert plan.items == {run: {"objects": 4, "bytes": 1000, "age_s": 20 * 86400}}
    assert plan.counts == {"listed": 1, "runs": 1, "reserved": 0, "unknown": 0, "eligible": 1, "prunable": 1,
                           "prunable_objects": 4, "prunable_bytes": 1000}


def test_a_run_inside_retention_is_kept_and_needs_no_receipt():
    young = _rid(FLOOR_DAYS - 0.1)
    plan = _plan([young], {})
    assert plan.prune == [] and plan.kept == {young: "inside-retention"}


def test_the_retention_boundary_is_inclusive_to_the_second():
    at = c.gc_run_id(NOW - c.GC_PRUNE_AFTER_S, ["edge"])
    one_short = c.gc_run_id(NOW - c.GC_PRUNE_AFTER_S + 1, ["edge"])
    plan = _plan([at, one_short], {at: _receipt(at), one_short: _receipt(one_short)})
    assert plan.prune == [at] and plan.kept == {one_short: "inside-retention"}


def test_a_run_dated_after_now_is_inside_retention():
    future = c.gc_run_id(NOW + 5 * DAY, ["skew"])
    plan = _plan([future], {future: _receipt(future)})
    assert plan.prune == [] and plan.kept == {future: "inside-retention"}


# 2. what is never a candidate

@pytest.mark.parametrize("reserved", [c.GC_STATE_DIR, c.GC_PRUNED_DIR])
def test_a_reserved_directory_is_skipped_not_pruned_not_kept_not_reported(reserved):
    run = _rid(20)
    plan = _plan([run, reserved], {run: _receipt(run)})
    assert plan.prune == [run] and reserved not in plan.kept and plan.unknown == []
    assert plan.counts["reserved"] == 1
    alone = _plan([reserved], {})
    assert alone.prune == [] and alone.kept == {} and alone.unknown == [] and alone.counts["reserved"] == 1


def test_both_reserved_names_are_counted_and_neither_is_a_run_id():
    plan = _plan([c.GC_STATE_DIR, c.GC_PRUNED_DIR], {})
    assert plan.counts["reserved"] == 2 and plan.counts["runs"] == 0 and plan.unknown == []
    assert c.gc_run_time(c.GC_STATE_DIR) is None and c.gc_run_time(c.GC_PRUNED_DIR) is None
    assert c.GC_STATE_DIR != c.GC_PRUNED_DIR and set(c.GC_ARCHIVE_RESERVED) == {c.GC_STATE_DIR, c.GC_PRUNED_DIR}


@pytest.mark.parametrize("stray", [
    "README", "_state-old", "_pruned-old", "_State", "20261004T000000Z", "20261004T000000Z-ABCDEF12", "20261004T000000Z-abcdef1",
    "20261004T000000Z-abcdef123", "x20261004T000000Z-abcdef12", "20261004T000000Z-abcdef12/", "",
])
def test_a_stray_name_is_reported_and_never_touched(stray):
    run = _rid(20)
    plan = _plan([run, stray], {run: _receipt(run), stray: _receipt(run)})
    assert plan.prune == [run] and plan.unknown == [stray] and stray not in plan.kept
    assert plan.counts["unknown"] == 1 and plan.counts["runs"] == 1


# 3. each reason a run stays, with a control that prunes it once the reason is gone

def test_a_run_without_a_receipt_is_kept():
    run = _rid(20)
    for receipts in ({}, {run: None}, {run: ["not", "a", "dict"]}, {run: "text"}):
        plan = _plan([run], receipts)
        assert plan.prune == [] and plan.kept == {run: "no-receipt"}, receipts
    assert _plan([run], {run: _receipt(run)}).prune == [run]


@pytest.mark.parametrize("over", [{"kind": "something-else"}, {"kind": None}, {"format": 2}, {"format": None},
                                  {"run_id": "20260101T000000Z-00000000"}, {"run_id": None}])
def test_a_receipt_that_is_not_this_runs_format_1_archive_receipt_is_kept(over):
    run = _rid(20)
    plan = _plan([run], {run: _receipt(run, **over)})
    assert plan.prune == [] and plan.kept == {run: "receipt-not-recognized"}
    assert _plan([run], {run: _receipt(run)}).prune == [run]


def test_a_run_of_another_store_is_kept_by_this_stores_pass():
    run = _rid(20)
    plan = _plan([run], {run: _receipt(run, store=OTHER)})
    assert plan.prune == [] and plan.kept == {run: "other-store"}
    assert _plan([run], {run: _receipt(run, store=OTHER)}, store=OTHER).prune == [run]


@pytest.mark.parametrize("status,reason", [
    ("pruned", "status-not-done"), ("archived", "status-not-done"), ("stopped: ClientError", "status-not-done"),
    (None, "status-not-done"), ("", "status-not-done"), ("DONE", "status-not-done"),
])
def test_only_a_done_run_is_pruned(status, reason):
    run = _rid(20)
    plan = _plan([run], {run: _receipt(run, status=status)})
    assert plan.prune == [] and plan.kept == {run: reason}


@pytest.mark.parametrize("break_it", [
    lambda r: r.pop("objects"), lambda r: r.update(objects=None), lambda r: r.update(objects=["a", "b"]),
    lambda r: r.update(objects={"asp/x.jsonl": "not a record"}), lambda r: r.update(objects={"asp/x.jsonl": None}),
])
def test_a_receipt_whose_objects_are_not_records_is_kept(break_it):
    run = _rid(20)
    receipt = _receipt(run)
    break_it(receipt)
    plan = _plan([run], {run: receipt})
    assert plan.prune == [] and plan.kept == {run: "receipt-malformed"}


def test_a_done_run_that_holds_nothing_is_pruned_with_zero_counts():
    run = _rid(20)
    plan = _plan([run], {run: _receipt(run, n=0)})
    assert plan.prune == [run] and plan.items[run] == {"objects": 0, "bytes": 0, "age_s": 20 * 86400}


def test_a_record_without_a_numeric_size_counts_as_zero_bytes():
    run = _rid(20)
    receipt = _receipt(run, n=3, size=10)
    names = sorted(receipt["objects"])
    receipt["objects"][names[0]] = {"md5": "0" * 32}
    receipt["objects"][names[1]] = {"size": "big"}
    plan = _plan([run], {run: receipt})
    assert plan.items[run]["objects"] == 3 and plan.items[run]["bytes"] == 10


# 4. what the head still wants

def test_a_run_holding_an_object_the_head_names_and_the_store_lacks_is_kept():
    a, b = _rid(20, "a"), _rid(30, "b")
    ra, rb = _receipt(a), _receipt(b)
    needed = {sorted(ra["objects"])[1]}
    plan = _plan([a, b], {a: ra, b: rb}, needed)
    assert plan.prune == [b] and plan.kept == {a: "head-needs-archived-object"}
    control = _plan([a, b], {a: ra, b: rb}, set())
    assert control.prune == [b, a] and control.kept == {}


def test_needed_may_be_any_collection_of_names():
    run = _rid(20)
    receipt = _receipt(run)
    name = sorted(receipt["objects"])[0]
    for needed in ({name}, [name], (name,), frozenset([name]), {name: 1}):
        assert _plan([run], {run: receipt}, needed).kept == {run: "head-needs-archived-object"}, needed
    for harmless in ((), set(), [], {"asp/unrelated.0.jsonl"}):
        assert _plan([run], {run: receipt}, harmless).prune == [run], harmless


# 5. refusals

def test_needed_unknown_refuses_and_removes_nothing():
    run = _rid(20)
    plan = _plan([run, c.GC_STATE_DIR, "stray"], {run: _receipt(run)}, None)
    assert plan.refused == ["needed-unknown"] and plan.prune == [] and plan.kept == {} and plan.items == {}
    assert plan.unknown == ["stray"] and plan.counts["runs"] == 1 and "prunable" not in plan.counts
    assert _plan([run], {run: _receipt(run)}, set()).prune == [run]


@pytest.mark.parametrize("window", [c.GC_PRUNE_AFTER_S - 1, 0, -1.0, float("nan"), float("inf"), "x", None, True])
def test_a_window_under_the_floor_or_not_a_number_refuses(window):
    run = _rid(20)
    plan = _plan([run], {run: _receipt(run)}, prune_after_s=window)
    assert plan.refused == ["retention-below-floor"] and plan.prune == [] and plan.kept == {}


def test_a_longer_window_is_honored_and_the_floor_itself_is_allowed():
    twenty, forty = _rid(20, "a"), _rid(40, "b")
    receipts = {twenty: _receipt(twenty), forty: _receipt(forty)}
    plan = _plan([twenty, forty], receipts, prune_after_s=30 * DAY)
    assert plan.refused == [] and plan.prune == [forty] and plan.kept == {twenty: "inside-retention"}
    assert _plan([twenty], receipts, prune_after_s=c.GC_PRUNE_AFTER_S).prune == [twenty]


@pytest.mark.parametrize("now", [float("nan"), float("inf"), float("-inf"), None, "x", True])
def test_a_now_that_is_not_a_finite_number_refuses(now):
    run = _rid(20)
    plan = _plan([run], {run: _receipt(run)}, now=now)
    assert plan.refused == ["now-invalid"] and plan.prune == [] and plan.kept == {}


def test_every_failing_input_is_named_in_order():
    run = _rid(20)
    plan = _plan([run], {run: _receipt(run)}, None, now=float("nan"), prune_after_s=1)
    assert plan.refused == ["now-invalid", "retention-below-floor", "needed-unknown"]


# 6. the cap and the order

def test_the_cap_keeps_the_oldest_first_and_names_the_rest_over_cap():
    runs = [_rid(15 + i, "r%d" % i) for i in range(5)]  # r0 is the youngest
    receipts = {r: _receipt(r) for r in runs}
    plan = _plan(runs, receipts, max_runs=3)
    assert plan.prune == [runs[4], runs[3], runs[2]]
    assert plan.kept == {runs[1]: "over-cap", runs[0]: "over-cap"}
    assert plan.counts["eligible"] == 5 and plan.counts["prunable"] == 3 and plan.counts["prunable_objects"] == 9


@pytest.mark.parametrize("cap", [0, -3])
def test_a_cap_of_nothing_prunes_nothing_and_every_eligible_run_is_over_cap(cap):
    run = _rid(20)
    plan = _plan([run], {run: _receipt(run)}, max_runs=cap)
    assert plan.prune == [] and plan.kept == {run: "over-cap"} and plan.items == {}
    assert plan.counts["eligible"] == 1 and plan.counts["prunable"] == 0


def test_the_default_cap_is_the_constant():
    runs = [_rid(15 + i, "r%d" % i) for i in range(c.GC_PRUNE_MAX_RUNS + 3)]
    plan = _plan(runs, {r: _receipt(r) for r in runs})
    assert len(plan.prune) == c.GC_PRUNE_MAX_RUNS
    assert sum(1 for why in plan.kept.values() if why == "over-cap") == 3


def test_order_follows_the_runs_time_whatever_the_listing_order():
    runs = [_rid(d, "t%d" % d) for d in (31, 16, 24, 45, 19)]
    shuffled = list(reversed(runs))
    random.Random(3).shuffle(shuffled)
    plan = _plan(shuffled, {r: _receipt(r) for r in runs})
    assert [c.gc_run_time(r) for r in plan.prune] == sorted(c.gc_run_time(r) for r in runs)


def test_two_runs_in_one_second_go_by_run_id():
    first = c.gc_run_id(NOW - 20 * DAY, ["one"])
    second = c.gc_run_id(NOW - 20 * DAY, ["two"])
    assert c.gc_run_time(first) == c.gc_run_time(second) and first != second
    plan = _plan([second, first], {first: _receipt(first), second: _receipt(second)})
    assert plan.prune == sorted([first, second])


# 7. accounting and purity

def test_a_name_listed_twice_counts_once():
    run = _rid(20)
    plan = _plan([run, run, c.GC_STATE_DIR, c.GC_STATE_DIR, "stray", "stray"], {run: _receipt(run)})
    assert plan.prune == [run] and plan.unknown == ["stray"]
    assert plan.counts["listed"] == 3 and plan.counts["runs"] == 1 and plan.counts["reserved"] == 1


def test_every_listed_run_is_pruned_or_kept_exactly_once_and_state_neither():
    rng = random.Random(11)
    for case in range(120):
        listing, receipts = [c.GC_STATE_DIR, c.GC_PRUNED_DIR, "stray-%d" % case], {}
        for i in range(rng.randint(0, 9)):
            run = _rid(rng.choice([1, 5, 13, 14, 15, 20, 90]), "c%d-%d" % (case, i))
            listing.append(run)
            kind = rng.choice(["ok", "ok", "none", "wrong-store", "archived", "pruned", "needed", "bad-kind", "bad-objects"])
            r = _receipt(run, store=OTHER if kind == "wrong-store" else STORE,
                         status={"archived": "archived", "pruned": "pruned"}.get(kind, "done"))
            if kind == "bad-kind":
                r["kind"] = "x"
            if kind == "bad-objects":
                r["objects"] = []
            receipts[run] = None if kind == "none" else r
        needed = {sorted(r["objects"])[0] for r in receipts.values() if r and isinstance(r["objects"], dict) and r["objects"]
                  and rng.random() < 0.2}
        plan = _plan(listing, receipts, needed, max_runs=rng.choice([0, 1, 2, 12]))
        runs = {n for n in listing if c.gc_run_time(n) is not None}
        assert set(plan.prune) | set(plan.kept) == runs and not set(plan.prune) & set(plan.kept), case
        assert not set(c.GC_ARCHIVE_RESERVED) & (set(plan.prune) | set(plan.kept)), case
        assert plan.unknown == ["stray-%d" % case], case
        assert len(plan.prune) == len(set(plan.prune)) and len(plan.prune) <= plan.counts["eligible"], case


def test_the_totals_cover_only_the_pruned_runs():
    a, b, c3 = _rid(20, "a"), _rid(25, "b"), _rid(30, "c")
    receipts = {a: _receipt(a, n=2, size=10), b: _receipt(b, n=3, size=100), c3: _receipt(c3, n=4, size=1000)}
    plan = _plan([a, b, c3], receipts, max_runs=2)
    assert plan.prune == [c3, b] and plan.kept == {a: "over-cap"}
    assert plan.counts["prunable_objects"] == 7 and plan.counts["prunable_bytes"] == 4300
    assert sum(i["objects"] for i in plan.items.values()) == 7 and sum(i["bytes"] for i in plan.items.values()) == 4300


def test_the_planner_changes_none_of_its_inputs_and_repeats_itself():
    a, b = _rid(20, "a"), _rid(30, "b")
    listing = [a, b, c.GC_STATE_DIR, "stray"]
    receipts = {a: _receipt(a), b: _receipt(b, status="archived")}
    needed = {"asp/unrelated.0.jsonl"}
    snapshot = copy.deepcopy((listing, receipts, needed))
    first = _plan(listing, receipts, needed)
    second = _plan(listing, receipts, needed)
    assert (listing, receipts, needed) == snapshot and first == second


# 8. the names are the archive's own

def test_the_top_level_names_the_planner_reads_are_the_ones_the_key_builders_write():
    root = "cust/env/"
    run = _rid(20)
    base = root + c.GC_ARCHIVE_DIR + "/"
    top = lambda key: key[len(base):].split("/")[0]  # noqa: E731
    assert top(c.gc_state_key(root, STORE, "state")) == c.GC_STATE_DIR
    assert top(c.gc_state_key(root, STORE, "ledger")) == c.GC_STATE_DIR
    assert top(c.gc_receipt_key(root, run)) == run and top(c.gc_archive_key(root, run, STORE, "asp/t.0.jsonl")) == run
    listing = {top(k) for k in (c.gc_state_key(root, STORE, "state"), c.gc_state_key(root, STORE, "ledger"),
                                c.gc_receipt_key(root, run), c.gc_archive_key(root, run, STORE, "asp/t.0.jsonl"))}
    plan = _plan(listing, {run: _receipt(run)})
    assert plan.prune == [run] and plan.unknown == [] and plan.counts["reserved"] == 1


def test_the_constants_keep_the_rule_they_state():
    assert c.GC_PRUNE_AFTER_S >= c.GC_GRACE_S  # recoverable as long after the delete as the orphan waited for it
    assert isinstance(c.GC_PRUNE_MAX_RUNS, int) and c.GC_PRUNE_MAX_RUNS >= 1
    assert c.gc_run_time(_rid(20)) == NOW - 20 * DAY
