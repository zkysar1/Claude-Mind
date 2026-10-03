"""Tests for the composite WRITE path of core/scripts/owncloud_backend.py ( U2d):
OwnCloudBackend._store_put, the one PUT seam of _put and _merge_reconcile_put.

Two flavors through the `s3` fixture shared with test_owncloud_composite_read_g358202: an in-memory
fake S3 (runs anywhere) and moto (real S3 semantics; skipped, with a reason, where moto is absent).

Coverage:
  1. flag on: the first write over a whole object publishes every segment, THEN the head: plain
     (never gzipped), padded to the floor, its plain-md5 metadata the md5 of the JOINED bytes;
     segments are created if absent (IfNoneMatch) and gzipped exactly when the codec flag says so
  2. steady state: one mutation PUTs one segment and the head, under the fence the write read
  3. the head PUT is the commit point: a head that loses the race leaves the winner's head and an
     orphan segment; a failed segment PUT never reaches the head; AccessDenied is loud
  4. an existing segment is not an error and is not rewritten; a cache miss attempts every name
     but stores each once
  5. the plain whole-object PUT is unchanged: flag off, flag naming another env, a store off the
     allowlist, a store under the size floor, a store the layout refuses (warned once)
  6. the next refresh downloads nothing (the head's md5 metadata); a cached head is not trusted
     once the fence has moved
  7. the merge-reconcile and mirror_put sites reach the same seam

File basename starts with ``test_`` so domain-leak-check.sh skips it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "core" / "scripts"))

import _owncloud_codec as codec  # noqa: E402
import _owncloud_composite as comp  # noqa: E402
from test_owncloud_composite_read_g358202 import (  # noqa: E402,F401  (the doubles are shared)
    BUCKET, ENV_ID, _backend, _isolate, _legacy, _mutated, _publish, _records, _setup, s3,
)

REAL_MIN_RAW_BYTES = comp.MIN_RAW_BYTES
GZIP_MAGIC = b"\x1f\x8b"


@pytest.fixture(autouse=True)
def _composite_on(monkeypatch, _isolate):
    """The writer flag names the test env, and the size floor is lowered to the fixture's size
    (the real floor has its own test)."""
    monkeypatch.setenv("OWNCLOUD_COMPOSITE_STORES", ENV_ID)
    monkeypatch.setattr(comp, "MIN_RAW_BYTES", 1)


@pytest.fixture(params=[False, True], ids=["plain", "gzip"])
def gz(request, monkeypatch):
    """Whether the gzip codec's own writer flag names the env: segments follow it, the head never does."""
    if request.param:
        monkeypatch.setenv("OWNCLOUD_GZIP_STORES", ENV_ID)
    return request.param


def _is_segment_key(k):
    return "/%s/" % comp.SEGMENT_DIR in k


def _stored(s3, key):
    obj = s3.get_object(Bucket=BUCKET, Key=key)
    return obj["Body"].read(), obj


def _prepare(s3, tmp_path, old=None):
    """A backend that has read a pre-composite store (one whole object), so its fence is that
    object's ETag: the state the first flagged write finds."""
    be, p, key = _setup(tmp_path, s3)
    old = _legacy(_records()) if old is None else old
    s3.put_object(Bucket=BUCKET, Key=key, Body=old)
    assert be.read_bytes(p, force_fresh=True) == old
    return be, p, key


def _another_mutation(base_records, asp, goal):
    recs = base_records()
    recs[asp]["goals"][goal]["status"] = "completed"
    return _legacy(recs)


# --- 1. the first flagged write ---------------------------------------------------------------------
def test_the_first_write_over_a_whole_object_publishes_every_segment_then_the_head(s3, tmp_path, gz):
    be, p, key = _prepare(s3, tmp_path)
    fence = be._etags[key]
    new = _legacy(_mutated())
    mark = len(s3.puts)
    be.write_bytes(p, new)
    puts = s3.puts[mark:]
    names = comp.plan_write(None, new).segments
    segs, head = puts[:-1], puts[-1]
    assert len(segs) == len(names) and {x["Key"] for x in segs} == {comp.segment_s3_key(key, n) for n in names}
    assert all(x["IfNoneMatch"] == "*" and x["IfMatch"] is None for x in segs)
    assert head["Key"] == key and head["IfMatch"] == fence and head["IfNoneMatch"] is None
    stored, obj = _stored(s3, key)
    assert comp.is_head(stored) and len(stored) >= comp.HEAD_MIN_BYTES
    assert not obj.get("ContentEncoding")  # the head is plain even when the codec flag names the env
    assert obj["Metadata"][codec.META_PLAIN_MD5] == hashlib.md5(new).hexdigest()  # the JOINED bytes
    for name, body in names.items():
        seg_bytes, seg_obj = _stored(s3, comp.segment_s3_key(key, name))
        if gz:
            assert seg_obj.get("ContentEncoding") == "gzip" and seg_bytes[:2] == GZIP_MAGIC
        else:
            assert not seg_obj.get("ContentEncoding") and seg_bytes == body
    assert be._etags[key] != fence and p.read_bytes() == new
    assert be.read_authoritative_bytes(p) == new  # the reader joins what the writer left


# --- 2. steady state ----------------------------------------------------------------------------------
def test_a_steady_state_write_puts_one_segment_and_the_head(s3, tmp_path, gz):
    be, p, key = _prepare(s3, tmp_path)
    be.write_bytes(p, _legacy(_mutated()))  # the migration write
    head_etag = be._etags[key]
    third = _another_mutation(_mutated, 3, 5)
    mark = len(s3.puts)
    be.write_bytes(p, third)
    puts = s3.puts[mark:]
    assert len(puts) == 2
    seg, head = puts
    assert _is_segment_key(seg["Key"]) and "/asp-3/0." in seg["Key"]
    assert seg["IfNoneMatch"] == "*" and head["Key"] == key and head["IfMatch"] == head_etag
    assert seg["bytes"] * 10 < len(third)  # the churn fix: one segment, not the whole store
    assert head["bytes"] >= comp.HEAD_MIN_BYTES
    assert be.read_authoritative_bytes(p) == third


# --- 3. the head PUT is the commit point -------------------------------------------------------------------
def test_a_head_that_loses_the_race_leaves_the_winners_head_and_an_orphan_segment(s3, tmp_path):
    be, p, key = _prepare(s3, tmp_path)
    be.write_bytes(p, _legacy(_mutated()))
    new = _another_mutation(_mutated, 5, 7)
    (name, _body), = comp.plan_write(be._composite_heads[key][1], new).segments.items()
    real_put, raced = s3.put_object, []

    def racing_put(**kw):
        out = real_put(**kw)
        if _is_segment_key(kw["Key"]) and not raced:
            raced.append(1)  # another writer commits a head between our segment PUT and our head PUT
            s3._real.put_object(Bucket=BUCKET, Key=key, Body=b"another writer won the head\n")
        return out

    s3.put_object = racing_put
    try:
        with pytest.raises(ClientError) as exc:
            be._store_put(p, key, new, {"Bucket": BUCKET, "Key": key, "IfMatch": be._etags[key], "Body": new})
    finally:
        s3.put_object = real_put
    assert exc.value.response["Error"]["Code"] == "PreconditionFailed"
    assert _stored(s3, key)[0] == b"another writer won the head\n"  # our head never committed
    assert _stored(s3, comp.segment_s3_key(key, name))[0]  # the orphan is there, harmless and collectable


def test_a_failed_segment_put_never_reaches_the_head_put(s3, tmp_path):
    be, p, key = _prepare(s3, tmp_path)
    old, etag = p.read_bytes(), be._etags[key]
    real_put = s3.put_object

    def failing(**kw):
        if _is_segment_key(kw["Key"]):
            raise ClientError({"Error": {"Code": "InternalError", "Message": "boom"}}, "PutObject")
        return real_put(**kw)

    mark = len(s3.puts)
    s3.put_object = failing
    try:
        with pytest.raises(ClientError):
            be.write_bytes(p, _legacy(_mutated()))
    finally:
        s3.put_object = real_put
    assert all(x["Key"] != key for x in s3.puts[mark:])  # the head PUT was never attempted
    stored, obj = _stored(s3, key)
    assert stored == old and obj["ETag"] == etag  # still the old object
    assert p.read_bytes() == old and be._etags[key] == etag  # and the mirror and the fence did not move


def test_an_access_denied_segment_put_is_a_loud_permission_error(s3, tmp_path):
    from owncloud_backend import OwnCloudPermissionError  # noqa: PLC0415
    be, p, key = _prepare(s3, tmp_path)
    real_put = s3.put_object

    def denied(**kw):
        if _is_segment_key(kw["Key"]):
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "PutObject")
        return real_put(**kw)

    s3.put_object = denied
    try:
        with pytest.raises(OwnCloudPermissionError):
            be.write_bytes(p, _legacy(_mutated()))
    finally:
        s3.put_object = real_put


# --- 4. immutable segments --------------------------------------------------------------------------------
def test_a_segment_that_already_exists_is_not_an_error_and_is_not_rewritten(s3, tmp_path):
    be, p, key = _prepare(s3, tmp_path)
    new = _legacy(_mutated())
    names = comp.plan_write(None, new).segments
    for name, body in names.items():  # every segment of the new store is already there, marked
        s3.put_object(Bucket=BUCKET, Key=comp.segment_s3_key(key, name), Body=body, Metadata={"marker": "kept"})
    mark = len(s3.puts)
    be.write_bytes(p, new)
    puts = s3.puts[mark:]
    assert len(puts) == len(names) + 1 and puts[-1]["Key"] == key  # every name attempted, then the head
    for name in names:
        assert _stored(s3, comp.segment_s3_key(key, name))[1]["Metadata"].get("marker") == "kept"
    assert be.read_authoritative_bytes(p) == new


def test_a_cache_miss_attempts_every_segment_but_stores_each_name_once(s3, tmp_path):
    be, p, key = _prepare(s3, tmp_path)
    be.write_bytes(p, _legacy(_mutated()))
    prefix = comp.segment_s3_key(key, "")

    def stored_segments():
        return len(s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix).get("Contents", []))

    before = stored_segments()
    restarted = _backend(tmp_path, s3)  # an empty head cache, as after a daemon restart
    restarted._etags[key] = be._etags[key]
    third = _another_mutation(_mutated, 9, 3)
    mark = len(s3.puts)
    restarted.write_bytes(p, third)
    assert len(s3.puts[mark:]) == len(comp.plan_write(None, third).segments) + 1
    assert stored_segments() == before + 1  # only the one changed segment was actually stored


# --- 5. the plain whole-object PUT is unchanged ------------------------------------------------------------
@pytest.mark.parametrize("flag", [None, "some-other-env"], ids=["unset", "another-env"])
def test_with_the_flag_off_the_write_is_one_plain_whole_object_put(s3, tmp_path, monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv("OWNCLOUD_COMPOSITE_STORES")
    else:
        monkeypatch.setenv("OWNCLOUD_COMPOSITE_STORES", flag)
    be, p, key = _prepare(s3, tmp_path)
    fence, new = be._etags[key], _legacy(_mutated())
    mark = len(s3.puts)
    be.write_bytes(p, new)
    puts = s3.puts[mark:]
    assert [x["Key"] for x in puts] == [key] and puts[0]["IfMatch"] == fence
    stored, _obj = _stored(s3, key)
    assert stored == new and not comp.is_head(stored)


def test_a_store_off_the_allowlist_stays_a_plain_whole_object_put_with_the_flag_on(s3, tmp_path):
    be = _backend(tmp_path, s3)
    p = tmp_path / "world" / "pipeline.jsonl"
    key = be._s3_key(p)
    assert not comp.reads_composite("world/pipeline.jsonl")  # the control: this store is not on the allowlist
    old, new = _legacy(_records()), _legacy(_mutated())
    s3.put_object(Bucket=BUCKET, Key=key, Body=old)
    assert be.read_bytes(p, force_fresh=True) == old
    mark = len(s3.puts)
    be.write_bytes(p, new)
    assert [x["Key"] for x in s3.puts[mark:]] == [key]
    stored, _obj = _stored(s3, key)
    assert stored == new and not comp.is_head(stored)


def test_a_store_under_the_size_floor_goes_whole(s3, tmp_path, monkeypatch):
    new = _legacy(_mutated())
    assert len(new) < REAL_MIN_RAW_BYTES  # the control: this fixture is under the real floor
    monkeypatch.setattr(comp, "MIN_RAW_BYTES", REAL_MIN_RAW_BYTES)
    be, p, key = _prepare(s3, tmp_path)
    mark = len(s3.puts)
    be.write_bytes(p, new)
    assert [x["Key"] for x in s3.puts[mark:]] == [key]
    stored, _obj = _stored(s3, key)
    assert stored == new and not comp.is_head(stored)


def test_a_store_the_layout_refuses_goes_whole_and_is_warned_once_per_reason(s3, tmp_path, caplog):
    be, p, key = _prepare(s3, tmp_path)
    dup = _legacy(_mutated()) + _legacy([{"id": "asp-3", "goals": []}])  # a duplicate aspiration id
    caplog.set_level(logging.WARNING)
    for content in (dup, dup + _legacy([{"id": "asp-4", "goals": []}])):  # the same reason twice
        mark = len(s3.puts)
        be.write_bytes(p, content)
        assert [x["Key"] for x in s3.puts[mark:]] == [key]
        stored, _obj = _stored(s3, key)
        assert stored == content and not comp.is_head(stored)
    warned = [r for r in caplog.records if "goes whole" in r.getMessage()]
    assert len(warned) == 1 and "duplicate aspiration id" in warned[0].getMessage()
    be.write_bytes(p, _legacy(_mutated()).rstrip(b"\n"))  # a different reason: no trailing newline
    assert len([r for r in caplog.records if "goes whole" in r.getMessage()]) == 2


def test_a_short_head_is_padded_to_the_floor_and_a_long_one_is_stored_as_it_is(s3, tmp_path, monkeypatch):
    be, p, key = _prepare(s3, tmp_path)
    new = _legacy(_mutated())
    be.write_bytes(p, new)
    stored, _obj = _stored(s3, key)
    plain = comp.plan_write(None, new).head
    assert len(plain) < comp.HEAD_MIN_BYTES  # the control: this fixture's head is short
    assert len(stored) == comp.HEAD_MIN_BYTES and stored.rstrip() == plain.rstrip()
    assert be.read_authoritative_bytes(p) == new  # a padded head joins like any other
    monkeypatch.setattr(comp, "HEAD_MIN_BYTES", 1024)
    third = _another_mutation(_mutated, 2, 1)
    assert len(comp.plan_write(None, third).head) > 1024  # the control: now the head is at or above the floor
    be.write_bytes(p, third)
    assert _stored(s3, key)[0] == comp.plan_write(None, third).head


# --- 6. the refresh and the fence ---------------------------------------------------------------------------
def test_the_next_refresh_after_a_composite_write_downloads_nothing(s3, tmp_path):
    be, p, key = _prepare(s3, tmp_path)
    new = _legacy(_mutated())
    be.write_bytes(p, new)
    mark = len(s3.log)
    assert be.read_bytes(p, force_fresh=True) == new
    assert s3.since("get", mark) == []  # the head's plain-md5 metadata is the md5 of the JOINED bytes


def test_a_cached_head_is_not_trusted_once_the_fence_has_moved(s3, tmp_path):
    be, p, key = _prepare(s3, tmp_path)
    be.write_bytes(p, _legacy(_mutated()))  # the head is cached at its ETag
    mine = p.read_bytes()
    whole_etag = s3.put_object(Bucket=BUCKET, Key=key, Body=mine)["ETag"]  # a peer's whole PUT of the same bytes
    assert be.read_bytes(p, force_fresh=True) == mine  # same content: nothing to download
    assert be._etags[key] == whole_etag  # but the fence follows the object
    third = _another_mutation(_mutated, 4, 2)
    mark = len(s3.puts)
    be.write_bytes(p, third)
    # nothing is assumed stored: every segment name is attempted, then the head
    assert len(s3.puts[mark:]) == len(comp.plan_write(None, third).segments) + 1


# --- 7. the other write sites ---------------------------------------------------------------------------
def test_merge_reconcile_writes_a_composite_fenced_on_the_fresh_head(s3, tmp_path):
    be, p, key = _setup(tmp_path, s3)
    head_etag, _head = _publish(s3, key, _legacy(_records()))  # a peer left a composite
    local = _legacy(_mutated())
    mark = len(s3.puts)
    be._merge_reconcile_put(p, key, be._local(p), local, lambda outgoing, remote_bytes: outgoing)
    puts = s3.puts[mark:]
    assert puts[-1]["Key"] == key and puts[-1]["IfMatch"] == head_etag
    assert len(puts) == 2 and _is_segment_key(puts[0]["Key"])  # the head the read saw made it one segment
    stored, obj = _stored(s3, key)
    assert comp.is_head(stored) and obj["Metadata"][codec.META_PLAIN_MD5] == hashlib.md5(local).hexdigest()
    assert be.read_authoritative_bytes(p) == local and p.read_bytes() == local


def test_the_registered_merge_handler_reconciles_a_composite_head_a_peer_committed(s3, tmp_path):
    """The real handler (coordination_merge.merge_aspirations), not a stub: a peer commits a composite
    head between our read and our write, so our write is refused, the handler merges our file with
    the peer's JOINED file, and the merged file is written through the seam as a composite."""
    be, p, key = _prepare(s3, tmp_path)
    peer_goal = _records()[3]["goals"][5]["id"]
    mine_goal = _mutated()[7]["goals"][100]["id"]
    peer_etag, _head = _publish(s3, key, _another_mutation(_records, 3, 5))  # replaces the whole object we read
    be.write_bytes(p, _legacy(_mutated()))
    assert s3.puts[-1]["Key"] == key and s3.puts[-1]["IfMatch"] == peer_etag  # the commit is fenced on the peer's head
    merged = be.read_authoritative_bytes(p)
    status = {g["id"]: g["status"] for r in map(json.loads, merged.splitlines()) for g in r["goals"]}
    assert status[peer_goal] == "completed" and status[mine_goal] == "completed"  # both writers' changes survive
    assert len(status) == len(_records()) * 240 and p.read_bytes() == merged
    assert comp.is_head(_stored(s3, key)[0])  # the merged file went back out as a composite, not whole


def test_mirror_put_reaches_the_same_seam_and_leaves_the_mirror_alone(s3, tmp_path):
    be, p, key = _prepare(s3, tmp_path)
    fence, new = be._etags[key], _legacy(_mutated())
    p.write_bytes(new)  # a raw local write that the sync sweep now pushes
    mark = len(s3.puts)
    be.mirror_put(p, new, expected_version=fence, local_is_source=True)
    puts = s3.puts[mark:]
    assert puts[-1]["Key"] == key and puts[-1]["IfMatch"] == fence
    assert len(puts) > 1 and all(_is_segment_key(x["Key"]) for x in puts[:-1])
    assert comp.is_head(_stored(s3, key)[0]) and be.read_authoritative_bytes(p) == new
