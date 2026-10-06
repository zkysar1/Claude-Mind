"""Backend tests for the READ-ONLY orphan enumeration of the composite goal-queue layout ( U2e).

THE SEAM: OwnCloudBackend.composite_gc_enumerate(path, ledger, now, ...) -> GcEnumeration. It reads the
head, lists the segment directory to the end of its pagination, re-reads the head, and hands the three to
the pure planner (`plan_gc`, pinned in test_owncloud_composite_gc_g358202). It must never change the store:
the next unit, which deletes from its plan, is the only caller allowed to.

WHAT THESE PINS ARE FOR. 'Reads only' and 'listed everything' are both ABSENCE-shaped claims, and an absence
is also what a dead component produces (guard-4166). So each runs against a log of every S3 call the backend
makes (`_Wire`), and carries a control that can fail: the wire demonstrably records a delete, the listing
test adds more objects than one page holds, a head that moves mid-pass is abandoned while the same call
without the move plans. Every scenario runs against BOTH an in-memory S3 and moto (real prefix, pagination,
ETag and timestamp semantics: guard-919), through the real backend class.

Coverage:
  1. the plan names the one orphan and keeps every object the head names; the head ETag and, for each name
     to delete, its size, ETag, modification time and first sighting come back (the enumeration)
  2. the call sequence is GET head, LIST, HEAD head and nothing else, and the local mirror is untouched
  3. the listing is read to the end of its pagination, with a continuation token on every later page; a
     truncated listing with no token is an error, never a short answer or a loop
  4. a head that moved or vanished mid-pass abandons it: nothing planned, nothing to delete
  5. refusals: store not on the allowlist (no S3 call at all), head missing, not a head (no listing), a
     segment the head names that is not in the store
  6. names are relative to the segment directory; a foreign object is reported and never planned; a
     directory-marker key is ignored
  7. `max_delete` and `grace_s` reach the planner; the ledger threads from one pass to the next and the
     caller's ledger is never mutated; an S3 error propagates instead of becoming a plan

File basename starts with ``test_`` so domain-leak-check.sh skips it.
"""
from __future__ import annotations

import hashlib
import sys
import time
import types
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

PROJECT_ROOT = Path(__file__).resolve().parents[3]
for _p in (str(PROJECT_ROOT / "core" / "scripts"), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _owncloud_composite as comp  # noqa: E402
from test_owncloud_composite_read_g358202 import (  # noqa: E402,F401  (the doubles are shared)
    BUCKET, _MemS3, _Spy, _aspiration, _client_error, _isolate, _legacy, _manifest, _publish, _setup, s3,
)

DAY = 86400.0
GRACE = 14 * DAY


class _Wire:
    """Every S3 call the backend makes, by name: (operation, key or prefix, continuation token). `after` runs a
    hook once, right after the first call of an operation (a writer committing in the middle of a pass)."""

    def __init__(self, real):
        self._real = real
        self.calls = []
        self.after = {}

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if not callable(attr):
            return attr

        def call(**kw):
            self.calls.append((name, kw.get("Key", kw.get("Prefix")), kw.get("ContinuationToken")))
            out = attr(**kw)
            hook = self.after.pop(name, None)
            if hook:
                hook()
            return out
        return call

    def ops(self, mark=0):
        return [op for op, _k, _t in self.calls[mark:]]


def _state(*changed_goals):
    """A goal queue of three aspirations, one segment each; `changed_goals` are (aspiration, index) pairs whose
    status differs, so the segment holding each is rewritten."""
    recs = [_aspiration("1", 240), _aspiration("2", 240), _aspiration("3", 240)]
    for asp, i in changed_goals:
        recs[asp - 1]["goals"][i]["status"] = "completed"
    return _legacy(recs)


def _names(raw):
    return {comp.segment_object_name(k, m["md5"]) for k, m in comp.split(raw).manifest.items()}


def _two_commits(tmp_path, s3):
    """A backend (over a call log) on an object store after TWO writer commits: the second changed one goal of
    asp-2, so head 2 no longer names asp-2's first segment object, which is still there: the one orphan."""
    wire = _Wire(s3)
    be, p, key = _setup(tmp_path, wire)
    raw1, raw2 = _state(), _state((2, 100))
    _etag1, head1 = _publish(s3, key, raw1)
    etag2, head2 = _publish(s3, key, raw2, old_head=head1)
    gone = _names(raw1) - _names(raw2)
    assert len(gone) == 1 and len(_names(raw2)) == 3
    return types.SimpleNamespace(be=be, p=p, key=key, wire=wire, s3=s3, head2=head2, etag=etag2,
                                 names2=_names(raw2), orphan=gone.pop(), tmp=tmp_path)


def _seg_key(w, name):
    return comp.segment_s3_key(w.key, name)


def _enumerate(w, ledger=None, now=None, **kw):
    now = time.time() + GRACE + 10 if now is None else now
    return w.be.composite_gc_enumerate(w.p, {} if ledger is None else ledger, now, grace_s=GRACE, **kw)


def _aged(w):
    return {w.orphan: time.time() - 100 * DAY}


def _is_mem(s3):
    return isinstance(s3._real, _MemS3)


# ---- 1. the plan and the enumeration -------------------------------------------------------------


def test_the_orphan_is_planned_and_everything_the_head_names_is_kept(s3, tmp_path):
    w = _two_commits(tmp_path, s3)
    t0 = time.time()
    out = _enumerate(w, {w.orphan: t0 - 100 * DAY}, now=t0 + GRACE + 10)
    assert out.plan.refused == [] and out.plan.delete == [w.orphan]
    assert out.plan.counts["listed"] == len(w.names2) + 1 and out.plan.counts["referenced"] == len(w.names2)
    assert out.head_etag == w.etag


def test_the_enumeration_names_what_would_be_deleted_with_its_evidence(s3, tmp_path):
    w = _two_commits(tmp_path, s3)
    t0 = time.time()
    out = _enumerate(w, {w.orphan: t0 - 100 * DAY}, now=t0 + GRACE + 10)
    assert list(out.items) == [w.orphan]
    item, stored = out.items[w.orphan], s3.get_object(Bucket=BUCKET, Key=_seg_key(w, w.orphan))
    assert set(item) == {"size", "last_modified", "etag", "first_seen"}
    assert item["size"] == stored["ContentLength"] and item["etag"] == stored["ETag"]
    assert abs(item["last_modified"] - t0) < 120  # epoch SECONDS, not milliseconds, and the object's own
    assert item["first_seen"] == t0 - 100 * DAY


# ---- 2. read-only --------------------------------------------------------------------------------


def test_a_pass_is_get_head_then_list_then_head_and_never_changes_the_store(s3, tmp_path):
    w = _two_commits(tmp_path, s3)
    mark = len(w.wire.calls)
    _enumerate(w, _aged(w))
    ops = w.wire.ops(mark)
    assert ops[0] == "get_object" and w.wire.calls[mark][1] == w.key
    assert ops[-1] == "head_object" and w.wire.calls[-1][1] == w.key
    middle = w.wire.calls[mark + 1:-1]
    assert middle and all(op == "list_objects_v2" and key == comp.segment_s3_key(w.key, "") for op, key, _t in middle)
    assert not w.p.exists() and _manifest(tmp_path) == {}  # the local mirror was not touched
    # the control that lets 'no put, no delete' mean something: the wire does record a delete
    mark = len(w.wire.calls)
    w.wire.delete_object(Bucket=BUCKET, Key=_seg_key(w, "asp-9/0." + "0" * 32 + ".jsonl"))
    assert w.wire.ops(mark) == ["delete_object"]


# ---- 3. pagination -------------------------------------------------------------------------------


def _extra_orphans(w, n):
    for i in range(n):
        name = "asp-9/%d.%s.jsonl" % (i % 5, hashlib.md5(b"extra-%d" % i).hexdigest())
        w.s3.put_object(Bucket=BUCKET, Key=_seg_key(w, name), Body=b"{}\n")


def test_the_listing_is_read_to_the_end_of_its_pagination(s3, tmp_path):
    w = _two_commits(tmp_path, s3)
    extra = 7 if _is_mem(s3) else 1001  # mem pages at 2 keys; moto pages at 1000, so it needs more than one page
    if _is_mem(s3):
        s3._real.page_cap = 2
    _extra_orphans(w, extra)
    mark = len(w.wire.calls)
    out = _enumerate(w, _aged(w))
    pages = [c for c in w.wire.calls[mark:] if c[0] == "list_objects_v2"]
    assert len(pages) >= 2 and pages[0][2] is None and all(t for _o, _k, t in pages[1:])
    total = len(w.names2) + 1 + extra
    assert out.plan.counts["listed"] == total and out.plan.counts["orphans"] == 1 + extra
    assert out.plan.delete == [w.orphan]  # the old one, found on whichever page it was


def test_a_listing_that_is_truncated_without_a_token_is_an_error_not_a_loop(tmp_path):
    s3 = _Spy(_MemS3())
    w = _two_commits(tmp_path, s3)
    s3._real.list_objects_v2 = lambda **kw: {"Contents": [], "IsTruncated": True}
    with pytest.raises(comp.CompositeError):
        _enumerate(w, _aged(w))


# ---- 4. a head that moves --------------------------------------------------------------------------


def test_a_head_that_moved_during_the_pass_abandons_it(s3, tmp_path):
    w = _two_commits(tmp_path, s3)
    ledger = _aged(w)
    w.wire.after["list_objects_v2"] = lambda: _publish(s3, w.key, _state((2, 100), (3, 5)), old_head=w.head2)
    out = _enumerate(w, ledger)
    assert out.plan.refused == ["head-moved-during-enumeration"]
    assert out.plan.delete == [] and out.items == {} and out.head_etag is None
    assert out.plan.ledger == ledger and out.plan.ledger is not ledger
    assert _enumerate(w, ledger).plan.refused == []  # control: the same call on the settled store plans


def test_a_head_that_vanished_during_the_pass_abandons_it(s3, tmp_path):
    w = _two_commits(tmp_path, s3)
    w.wire.after["list_objects_v2"] = lambda: s3.delete_object(Bucket=BUCKET, Key=w.key)
    out = _enumerate(w, _aged(w))
    assert out.plan.refused == ["head-moved-during-enumeration"] and out.plan.delete == []


# ---- 5. refusals -----------------------------------------------------------------------------------


def test_a_store_off_the_allowlist_is_refused_before_any_s3_call(s3, tmp_path):
    w = _two_commits(tmp_path, s3)
    mark = len(w.wire.calls)
    out = w.be.composite_gc_enumerate(tmp_path / "world" / "notes.txt", {"x": 1.0}, time.time(), grace_s=GRACE)
    assert out.plan.refused == ["store-not-allowlisted"] and out.plan.ledger == {"x": 1.0}
    assert w.wire.calls[mark:] == []


def test_a_missing_head_is_refused_and_nothing_is_listed(s3, tmp_path):
    wire = _Wire(s3)
    be, p, _key = _setup(tmp_path, wire)
    mark = len(wire.calls)
    out = be.composite_gc_enumerate(p, {}, time.time(), grace_s=GRACE)
    assert out.plan.refused == ["head-missing"] and out.head_etag is None
    assert wire.ops(mark) == ["get_object"]


def test_a_whole_object_at_the_key_is_refused_without_listing(s3, tmp_path):
    wire = _Wire(s3)
    be, p, key = _setup(tmp_path, wire)
    put = s3.put_object(Bucket=BUCKET, Key=key, Body=_state())
    mark = len(wire.calls)
    out = be.composite_gc_enumerate(p, {}, time.time(), grace_s=GRACE)
    assert out.plan.refused == ["not-a-head"] and out.head_etag == put["ETag"] and out.items == {}
    assert wire.ops(mark) == ["get_object"]  # no listing, no second read of a store that has no head


def test_a_segment_the_head_names_that_is_not_in_the_store_is_refused(s3, tmp_path):
    w = _two_commits(tmp_path, s3)
    s3.delete_object(Bucket=BUCKET, Key=_seg_key(w, sorted(w.names2)[0]))
    out = _enumerate(w, _aged(w))
    assert out.plan.refused and out.plan.refused[0].startswith("head-names-unlisted-segments")
    assert out.plan.delete == [] and out.items == {}


# ---- 6. names --------------------------------------------------------------------------------------


def test_foreign_objects_are_reported_never_planned_and_a_directory_marker_is_ignored(s3, tmp_path):
    w = _two_commits(tmp_path, s3)
    for name in ("README", "asp-9/notes.txt", ""):  # the empty name is the prefix itself: a directory marker
        s3.put_object(Bucket=BUCKET, Key=_seg_key(w, name), Body=b"x")
    ledger = {**_aged(w), "README": 1.0, "asp-9/notes.txt": 1.0}
    out = _enumerate(w, ledger)
    assert out.plan.unknown == ["README", "asp-9/notes.txt"] and out.plan.delete == [w.orphan]
    assert out.plan.counts["listed"] == len(w.names2) + 1 + 2  # the marker is not an object of the store


# ---- 7. parameters, the ledger, errors ---------------------------------------------------------------


def test_max_delete_and_the_grace_reach_the_planner(s3, tmp_path):
    w = _two_commits(tmp_path, s3)
    _etag3, _head3 = _publish(s3, w.key, _state((2, 100), (3, 5)), old_head=w.head2)  # a second orphan: asp-3's
    second = (_names(_state()) - _names(_state((3, 5)))).pop()
    ledger = {w.orphan: time.time() - 100 * DAY, second: time.time() - 100 * DAY}
    both = _enumerate(w, ledger)
    assert sorted(both.plan.delete) == sorted([w.orphan, second]) and set(both.items) == set(both.plan.delete)
    one = _enumerate(w, ledger, max_delete=1)
    assert len(one.plan.delete) == 1 and set(one.items) == set(one.plan.delete) and one.plan.counts["aged"] == 2
    later = time.time() + 20 * DAY  # past the default grace, inside a 1000-day one
    long = w.be.composite_gc_enumerate(w.p, ledger, later, grace_s=1000 * DAY)
    assert long.plan.delete == [] and long.plan.counts["orphans"] == 2
    assert len(w.be.composite_gc_enumerate(w.p, ledger, later, grace_s=GRACE).plan.delete) == 2  # the control


def test_the_ledger_threads_between_passes_and_is_never_mutated(s3, tmp_path):
    w = _two_commits(tmp_path, s3)
    now1 = time.time()
    seed = {}
    first = w.be.composite_gc_enumerate(w.p, seed, now1, grace_s=GRACE)
    assert first.plan.delete == [] and first.plan.ledger == {w.orphan: now1} and seed == {}
    kept = dict(first.plan.ledger)
    second = w.be.composite_gc_enumerate(w.p, first.plan.ledger, now1 + GRACE + 1, grace_s=GRACE)
    assert second.plan.delete == [w.orphan] and first.plan.ledger == kept
    assert second.items[w.orphan]["first_seen"] == now1


def test_an_s3_error_propagates_instead_of_becoming_a_plan(tmp_path):
    s3 = _Spy(_MemS3())
    w = _two_commits(tmp_path, s3)

    def broken(**kw):
        raise _client_error("InternalError", "ListObjectsV2")

    s3._real.list_objects_v2 = broken
    with pytest.raises(ClientError):
        _enumerate(w, _aged(w))
