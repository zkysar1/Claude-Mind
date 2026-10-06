"""Tests for  items (e) and (f) of the composite store: the writer's re-PUT of a segment it finds present
and old (`OwnCloudBackend._freshen_segment`), the delete pass's re-check at delete time
(`composite_gc_apply`), and `plan_write` over an old head the layout cannot read.

Three flavors through the `s3` fixture of test_owncloud_composite_gc_apply_g358202: an in-memory fake S3 (runs
anywhere), moto, and moto with bucket versioning on (both skipped, with a reason, where moto is absent). A test that
needs an object to be OLD runs on the fake alone: moto stamps last_modified when it stores an object and cannot be
aged. What those tests cannot show on moto is covered by every unchanged-object delete in the GC-apply tests, which
run the re-check against moto's own HEAD and listing timestamps.

Coverage:
  1. the two pure predicates: needs_freshen (the window, the boundary, an undated object) and moved_since_listing
     (the timestamp precision, failing closed)
  2. (f) plan_write: an old head of any unreadable shape names nothing, the reader rejects the same heads, and a
     write over a held head of that kind replaces it
  3. (e) the writer: a present OLD segment is PUT again, unconditionally and before the head, with its codec; a
     young one is only looked at; one a sweep took since the 412 is created again; a second cold write finds
     them fresh and PUTs nothing; denied and failing HEADs and PUTs are loud and never reach the head
  4. (e) the delete pass: an object re-written, gone, undated or unreadable at its re-check is kept, never counted
     as deleted
  5. the round trip through the real writer and sweeper: the freshen puts the object out of the sweep's reach,
     and the re-check catches one freshened after the sweep's head read

File basename starts with ``test_`` so domain-leak-check.sh skips it.
"""
from __future__ import annotations

import json
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

PROJECT_ROOT = Path(__file__).resolve().parents[3]
for _p in (str(PROJECT_ROOT / "core" / "scripts"), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _owncloud_composite as comp  # noqa: E402
from test_owncloud_composite_gc_apply_g358202 import (  # noqa: E402,F401  (the fixtures and doubles are shared)
    DAY, ENV_ROOT, GRACE, _aged, _apply, _body, _exists, _gc_flag_off, _revert_world, _seg, _Tap, _world, gc_on, s3,
)
from test_owncloud_composite_gc_enumerate_g358202 import _names  # noqa: E402
from test_owncloud_composite_read_g358202 import (  # noqa: E402,F401
    BUCKET, ENV_ID, REL, _backend, _isolate, _legacy, _mutated, _MemS3, _records,
)
from test_owncloud_composite_write_g358202 import _another_mutation, _prepare  # noqa: E402

_DROP = object()


@pytest.fixture(autouse=True)
def _composite_on(monkeypatch, _isolate):  # after _isolate, which unsets the flag
    monkeypatch.setenv(comp.FLAG_ENV, ENV_ID)  # the writer flag, for these tests only
    monkeypatch.setattr(comp, "MIN_RAW_BYTES", 1)


def _client_error(code, op):
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


def _only_mem(s3):
    if not isinstance(s3, _MemS3):
        pytest.skip("moto stamps last_modified when it stores an object, so it cannot be aged")


def _age(s3, key, days):
    s3.objects[key]["LastModified"] = datetime.now(timezone.utc) - timedelta(days=days)


def _is_segment(key):
    return "/%s/" % comp.SEGMENT_DIR in key


def _all_keys(s3):
    if isinstance(s3, _MemS3):
        return list(s3.objects)
    return [c["Key"] for c in s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])]


class _Log:
    """Every put, head and delete the backend issues, in order, as (operation, key, detail); a put's detail is
    its fences, size and encoding."""

    def __init__(self, real):
        self._real = real
        self.calls = []

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if name not in ("put_object", "head_object", "delete_object") or not callable(attr):
            return attr

        def call(**kw):
            detail = None
            if name == "put_object":
                detail = {"IfNoneMatch": kw.get("IfNoneMatch"), "IfMatch": kw.get("IfMatch"), "bytes": len(kw["Body"]),
                          "ContentEncoding": kw.get("ContentEncoding")}
            self.calls.append((name, kw["Key"], detail))
            return attr(**kw)
        return call

    def since(self, mark):
        return self.calls[mark:]


def _cold_world(s3, tmp_path, age_days=None):
    """A store the writer has migrated (a head over every segment) and a SECOND backend holding the same fence with an
    empty head cache, as after a daemon restart: the write it makes next PUTs every segment name, so each one already
    stored answers 412. `age_days` ages every stored segment (the fake only)."""
    log = _Log(_Tap(s3))
    be, p, key = _prepare(log, tmp_path)
    be.write_bytes(p, _legacy(_mutated()))
    if age_days:
        _only_mem(s3)
        for k in list(s3.objects):
            if _is_segment(k):
                _age(s3, k, age_days)
    cold = _backend(tmp_path, log)
    cold._etags[key] = be._etags[key]
    return types.SimpleNamespace(log=log, tap=log._real, be=cold, first=be, p=p, key=key, s3=s3, tmp=tmp_path)


def _unconditional_segment_puts(calls):
    return [c for c in calls if c[0] == "put_object" and _is_segment(c[1]) and c[2]["IfNoneMatch"] is None]


def _create_attempts(calls):
    return [c for c in calls if c[0] == "put_object" and _is_segment(c[1]) and c[2]["IfNoneMatch"] == "*"]


def _head_put_index(calls, key):
    return max(i for i, c in enumerate(calls) if c[0] == "put_object" and c[1] == key)


def _present_names(third):
    """The segment names of `third` that the migrated store already holds (every one but the changed segment)."""
    return sorted(n for n in comp.plan_write(None, third).segments if n in _names(_legacy(_mutated())))


# --- 1. the pure predicates -----------------------------------------------------------------------------------
def test_needs_freshen_is_true_from_grace_minus_margin_and_never_for_an_undated_object():
    edge = GRACE - comp.FRESHEN_MARGIN_S
    assert edge == 13 * DAY and 0 < comp.FRESHEN_MARGIN_S < GRACE
    assert comp.needs_freshen(0.0, edge) and comp.needs_freshen(0.0, edge + 1) and comp.needs_freshen(0.0, GRACE * 3)
    assert not comp.needs_freshen(0.0, edge - 1)  # young: the sweep cannot take it for at least the margin
    assert not comp.needs_freshen(1000.0, 999.0)  # a clock behind the store's is not an old object
    for undated in (None, float("nan"), float("inf"), "x", True):
        assert not comp.needs_freshen(undated, 10 ** 12)  # the planner never deletes one, so it is never freshened
    assert comp.needs_freshen(0.0, 20 * DAY, grace_s=30 * DAY, margin_s=11 * DAY)
    assert not comp.needs_freshen(0.0, 18 * DAY, grace_s=30 * DAY, margin_s=11 * DAY)


def test_moved_since_listing_allows_the_timestamp_precision_and_fails_closed():
    slop = comp.GC_MTIME_SLOP_S
    assert not comp.moved_since_listing(100.0, 100.0) and not comp.moved_since_listing(100.0, 99.0)  # a HEAD truncates
    assert not comp.moved_since_listing(100.0, 100.0 + slop)
    assert comp.moved_since_listing(100.0, 100.0 + slop + 0.001)
    for bad in (None, float("nan"), float("inf"), "x", True):
        assert comp.moved_since_listing(bad, 100.0) and comp.moved_since_listing(100.0, bad)


# --- 2. (f) plan_write over an old head the layout cannot read ----------------------------------------------------
def _head_variants():
    raw = _legacy(_records())
    doc = json.loads(comp.split(raw).head.decode("utf-8"))
    first = next(iter(doc["segments"]))

    def head_of(**changes):
        d = dict(doc)
        for k, v in changes.items():
            if v is _DROP:
                d.pop(k)
            else:
                d[k] = v
        return (comp.dumps(d) + "\n").encode("ascii")

    bad_entry = dict(doc["segments"])
    bad_entry[first] = 5
    no_md5 = dict(doc["segments"])
    no_md5[first] = {"bytes": 1}
    return {
        "not-json": comp._HEAD_PREFIX + b" garbage\n",
        "no-segments-field": head_of(segments=_DROP),
        "segments-is-a-list": head_of(segments=[1, 2]),
        "segments-is-null": head_of(segments=None),
        "entry-is-an-int": head_of(segments=bad_entry),
        "entry-has-no-md5": head_of(segments=no_md5),
    }


@pytest.mark.parametrize("shape", sorted(_head_variants()))
def test_an_old_head_the_layout_cannot_read_names_nothing_so_every_segment_is_put(shape):
    head = _head_variants()[shape]
    assert comp.is_head(head)
    new = _legacy(_mutated())
    out, everything = comp.plan_write(head, new), comp.plan_write(None, new)
    assert out.segments == everything.segments and out.head == everything.head
    readable = comp.split(_legacy(_records())).head  # the control: a readable old head still skips what it names
    assert len(comp.plan_write(readable, new).segments) == 1 < len(out.segments)


@pytest.mark.parametrize("shape", sorted(_head_variants()))
def test_the_reader_rejects_the_same_heads_so_no_cached_head_is_one_of_them(shape):
    head = _head_variants()[shape]
    unreadable = (comp.IntegrityError, KeyError, TypeError, AttributeError)
    with pytest.raises(unreadable):
        comp.head_object_names(head)
    with pytest.raises(unreadable):
        comp.plan_refresh(None, head)  # read_whole runs this before joining, and catches only SegmentMissing and IntegrityError


@pytest.mark.parametrize("shape", ["entry-has-no-md5", "segments-is-a-list"])
def test_a_write_over_a_held_head_the_layout_cannot_read_replaces_it(s3, tmp_path, shape):
    log = _Log(s3)
    be, p, key = _prepare(log, tmp_path)
    be.write_bytes(p, _legacy(_mutated()))
    be._composite_heads[key] = (be._etags[key], _head_variants()[shape])  # a head no reader validated: white-box
    third = _another_mutation(_mutated, 9, 3)
    mark = len(log.calls)
    be.write_bytes(p, third)  # raised KeyError or AttributeError out of _store_put before the guard
    assert len(_create_attempts(log.since(mark))) == len(comp.plan_write(None, third).segments)  # nothing assumed held
    assert be.read_authoritative_bytes(p) == third
    assert comp.is_head(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())
    assert comp.head_object_names(be._composite_heads[key][1]) == _names(third)  # the head it left is readable


# --- 3. (e) the writer ---------------------------------------------------------------------------------------------
def test_a_present_old_segment_is_put_again_unconditionally_and_before_the_head(s3, tmp_path):
    w = _cold_world(s3, tmp_path, age_days=20)
    third = _another_mutation(_mutated, 9, 3)
    names = comp.plan_write(None, third).segments
    present = _present_names(third)
    before = {n: s3.objects[comp.segment_s3_key(w.key, n)]["Body"] for n in present}
    mark = len(w.log.calls)
    w.be.write_bytes(w.p, third)
    calls = w.log.since(mark)
    again = _unconditional_segment_puts(calls)
    assert len(_create_attempts(calls)) == len(names) and len(again) == len(present) == len(names) - 1
    assert {c[1] for c in again} == {comp.segment_s3_key(w.key, n) for n in present}
    head_at = _head_put_index(calls, w.key)
    for c in again:  # each follows its own failed create attempt and a look at the object, and precedes the head
        i = calls.index(c)
        assert calls[i - 1][:2] == ("head_object", c[1]) and calls[i - 2][:2] == ("put_object", c[1]) and i < head_at
    for n in present:  # same bytes, new last_modified: the clock a sweep reads restarted
        rec = s3.objects[comp.segment_s3_key(w.key, n)]
        assert rec["Body"] == before[n] and datetime.now(timezone.utc) - rec["LastModified"] < timedelta(minutes=5)
    assert w.be.read_authoritative_bytes(w.p) == third


def test_a_present_young_segment_is_looked_at_and_not_put_again(s3, tmp_path):
    w = _cold_world(s3, tmp_path)
    third = _another_mutation(_mutated, 9, 3)
    names = comp.plan_write(None, third).segments
    mark = len(w.log.calls)
    w.be.write_bytes(w.p, third)
    calls = w.log.since(mark)
    assert len(_create_attempts(calls)) == len(names) and _unconditional_segment_puts(calls) == []
    looked = [c[1] for c in calls if c[0] == "head_object" and _is_segment(c[1])]
    assert len(looked) == len(names) - 1  # one look per 412, none for the segment that was new
    assert w.be.read_authoritative_bytes(w.p) == third


def test_a_second_cold_write_finds_the_segments_fresh_and_puts_none_again(s3, tmp_path):
    """The cost bound: a segment is re-PUT once per window, not once per write that finds it."""
    w = _cold_world(s3, tmp_path, age_days=20)
    w.be.write_bytes(w.p, _another_mutation(_mutated, 9, 3))
    assert len(_unconditional_segment_puts(w.log.calls)) == len(comp.plan_write(None, _legacy(_mutated())).segments) - 1
    again = _backend(w.tmp, w.log)  # another restart
    again._etags[w.key] = w.be._etags[w.key]
    recs = _mutated()  # the first write's change kept, one more added: no name is reverted to an old object
    recs[9]["goals"][3]["status"] = "completed"
    recs[11]["goals"][4]["status"] = "completed"
    mark = len(w.log.calls)
    again.write_bytes(w.p, _legacy(recs))
    calls = w.log.since(mark)
    assert _unconditional_segment_puts(calls) == [] and len(_create_attempts(calls)) > 1


def test_a_re_put_keeps_the_gzip_encoding_the_segment_was_stored_with(s3, tmp_path, monkeypatch):
    _only_mem(s3)
    monkeypatch.setenv("OWNCLOUD_GZIP_STORES", ENV_ID)
    w = _cold_world(s3, tmp_path, age_days=20)
    third = _another_mutation(_mutated, 9, 3)
    mark = len(w.log.calls)
    w.be.write_bytes(w.p, third)
    again = _unconditional_segment_puts(w.log.since(mark))
    assert again and all(c[2]["ContentEncoding"] == "gzip" for c in again)
    assert all(s3.objects[c[1]]["ContentEncoding"] == "gzip" for c in again)
    assert w.be.read_authoritative_bytes(w.p) == third  # the joined file decodes through the codec


def _sweep_after_the_412(w, name):
    """A sweep takes the segment right after the writer's create attempt answered 412."""
    key = comp.segment_s3_key(w.key, name)
    w.tap.rule("put_object", lambda kw: kw.get("Key") == key and kw.get("IfNoneMatch") == "*",
               lambda kw: w.s3.delete_object(Bucket=BUCKET, Key=key), when="finally")
    return key


def test_a_segment_a_sweep_took_after_the_412_is_created_again_before_the_head(s3, tmp_path):
    w = _cold_world(s3, tmp_path)
    third = _another_mutation(_mutated, 9, 3)
    key = _sweep_after_the_412(w, _present_names(third)[0])
    before = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    mark = len(w.log.calls)
    w.be.write_bytes(w.p, third)
    calls = w.log.since(mark)
    mine = [(c[0], (c[2] or {}).get("IfNoneMatch")) for c in calls if c[1] == key]
    assert mine == [("put_object", "*"), ("head_object", None), ("put_object", None)]  # 412, a look, the object again
    assert s3.get_object(Bucket=BUCKET, Key=key)["Body"].read() == before
    assert max(i for i, c in enumerate(calls) if c[1] == key) < _head_put_index(calls, w.key)
    assert w.be.read_authoritative_bytes(w.p) == third


def test_a_denied_re_put_is_a_loud_permission_error_and_the_head_never_commits(s3, tmp_path):
    from owncloud_backend import OwnCloudPermissionError  # noqa: PLC0415
    w = _cold_world(s3, tmp_path)
    third = _another_mutation(_mutated, 9, 3)
    key = _sweep_after_the_412(w, _present_names(third)[0])
    stored, etag = s3.get_object(Bucket=BUCKET, Key=w.key)["Body"].read(), w.be._etags[w.key]

    def deny(kw):
        raise _client_error("AccessDenied", "PutObject")

    w.tap.rule("put_object", lambda kw: kw.get("Key") == key and kw.get("IfNoneMatch") is None, deny, when="before")
    mark = len(w.log.calls)
    with pytest.raises(OwnCloudPermissionError):
        w.be.write_bytes(w.p, third)
    assert all(c[1] != w.key for c in w.log.since(mark) if c[0] == "put_object")  # the head PUT was never attempted
    assert s3.get_object(Bucket=BUCKET, Key=w.key)["Body"].read() == stored and w.be._etags[w.key] == etag


@pytest.mark.parametrize("code", ["AccessDenied", "InternalError"])
def test_a_head_that_fails_for_another_reason_than_absence_is_loud_and_the_head_never_commits(s3, tmp_path, code):
    from owncloud_backend import OwnCloudPermissionError  # noqa: PLC0415
    w = _cold_world(s3, tmp_path)

    def fail(kw):
        raise _client_error(code, "HeadObject")

    w.tap.rule("head_object", lambda kw: _is_segment(kw.get("Key", "")), fail, when="before")
    mark = len(w.log.calls)
    with pytest.raises(OwnCloudPermissionError if code == "AccessDenied" else ClientError):
        w.be.write_bytes(w.p, _another_mutation(_mutated, 9, 3))
    assert all(c[1] != w.key for c in w.log.since(mark) if c[0] == "put_object")


# --- 4. (e) the delete pass's re-check ---------------------------------------------------------------------------
def test_an_object_re_written_between_the_listing_and_its_delete_is_kept(s3, tmp_path, gc_on):
    _only_mem(s3)
    w = _world(tmp_path, s3)
    orphan = w.orphans[0]
    key, now, ledger = _seg(w, orphan), time.time(), _aged(w)
    _age(s3, key, 20)  # old enough to collect on the real clock
    enum = w.be.composite_gc_enumerate(w.p, ledger, now)
    assert enum.plan.delete == [orphan]  # the control: it is eligible
    same = s3.objects[key]["Body"]
    w.tap.rule("head_object", lambda kw: kw.get("Key") == key,
               lambda kw: s3.put_object(Bucket=BUCKET, Key=key, Body=same), when="before")  # a writer's re-PUT
    out = w.be.composite_gc_apply(w.p, ledger, now)
    assert out.stopped is None and out.deleted == [] and out.restored == []
    assert out.skipped[orphan] == "rewritten-since-listing"
    assert _exists(s3, key) and _body(s3, key) == same
    assert out.ledger[orphan] == enum.plan.ledger[orphan]  # still waiting in the ledger, not forgotten
    receipt = json.loads(_body(s3, comp.gc_receipt_key(ENV_ROOT, out.run_id)))
    assert receipt["skipped"][orphan] == "rewritten-since-listing" and receipt["deleted"] == []


def test_an_object_that_is_gone_at_its_re_check_is_skipped_not_counted_as_deleted(s3, tmp_path, gc_on):
    w = _world(tmp_path, s3)
    orphan = w.orphans[0]
    key = _seg(w, orphan)
    w.tap.rule("head_object", lambda kw: kw.get("Key") == key,
               lambda kw: s3.delete_object(Bucket=BUCKET, Key=key), when="before")  # another process took it
    out = _apply(w)
    assert out.stopped is None and out.deleted == [] and out.skipped[orphan] == "gone-since-archive"
    assert out.restored == [] and not _exists(s3, key)
    assert _exists(s3, comp.gc_archive_key(ENV_ROOT, out.run_id, REL, orphan))  # it was archived first


def test_a_failed_re_check_head_stops_the_pass_with_nothing_deleted(s3, tmp_path, gc_on):
    from owncloud_backend import OwnCloudPermissionError  # noqa: PLC0415
    w = _world(tmp_path, s3)
    key = _seg(w, w.orphans[0])

    def deny(kw):
        raise _client_error("AccessDenied", "HeadObject")

    w.tap.rule("head_object", lambda kw: kw.get("Key") == key, deny, when="before")
    with pytest.raises(OwnCloudPermissionError):
        _apply(w)
    assert _exists(s3, key)  # the object is still there
    receipts = [k for k in _all_keys(s3) if k.endswith("/RECEIPT.json")]
    assert len(receipts) == 1 and json.loads(_body(s3, receipts[0]))["status"] == "stopped: OwnCloudPermissionError"


def test_an_object_whose_re_check_carries_no_last_modified_is_kept(s3, tmp_path, gc_on):
    w = _world(tmp_path, s3)
    orphan = w.orphans[0]
    key = _seg(w, orphan)
    w.tap.rule("head_object", lambda kw: kw.get("Key") == key, lambda kw: {"ETag": '"x"', "ContentLength": 1},
               when="instead")  # a server that returns no Last-Modified
    out = _apply(w)
    assert out.deleted == [] and out.skipped[orphan] == "rewritten-since-listing" and _exists(s3, key)


# --- 5. the round trip through the real writer and sweeper -------------------------------------------------------
def test_a_writer_that_freshens_before_its_head_commit_puts_the_object_out_of_the_sweeps_reach(s3, tmp_path,
                                                                                               monkeypatch, gc_on):
    _only_mem(s3)
    r = _revert_world(s3, tmp_path, monkeypatch)
    _age(s3, r.seg_key, 20)
    now, ledger = time.time(), {r.orphan: time.time() - 100 * DAY}
    assert r.sweeper.composite_gc_enumerate(r.p, ledger, now).plan.delete == [r.orphan]  # the control: collectable
    sweeps = []
    r.tap.rule("put_object", lambda kw: kw.get("Key") == r.key,
               lambda kw: sweeps.append(r.sweeper.composite_gc_apply(r.p, ledger, now)), when="before")
    r.be.write_bytes(r.p, r.raw_b)  # a revert: its segment PUT answers 412 and the object is 20 days old
    (swept,) = sweeps  # a sweep that landed after the writer's re-PUT and before its head commit
    assert swept.stopped is None and swept.plan.delete == [] and swept.deleted == []  # fresh: not even a candidate
    assert _exists(s3, r.seg_key)
    assert r.sweeper.read_authoritative_bytes(r.p) == r.raw_b


def test_the_re_check_keeps_an_object_the_writer_freshened_after_the_sweeps_head_read(s3, tmp_path, monkeypatch,
                                                                                    gc_on):
    _only_mem(s3)
    r = _revert_world(s3, tmp_path, monkeypatch)
    _age(s3, r.seg_key, 20)
    sweeper_tap = _Tap(s3)
    sweeper = _backend(tmp_path, sweeper_tap)
    sweeper_tap.rule("head_object", lambda kw: kw.get("Key") == r.seg_key, lambda kw: r.be.write_bytes(r.p, r.raw_b),
                     when="before")  # the writer's whole revert runs between the sweep's head read and its re-check
    out = sweeper.composite_gc_apply(r.p, {r.orphan: time.time() - 100 * DAY}, time.time())
    assert out.stopped is None and out.deleted == [] and out.skipped[r.orphan] == "rewritten-since-listing"
    assert out.restored == []  # nothing for the late restore to repair: the re-check kept it
    assert _exists(s3, r.seg_key) and sweeper.read_authoritative_bytes(r.p) == r.raw_b
