"""Backend tests for the DELETE half of orphan collection in the composite goal-queue layout ( U2e).

THE SEAMS: OwnCloudBackend.composite_gc_apply(path, ledger, now, ...) -> GcApplied, which collects the orphan
segment objects the read-only enumeration names, and composite_gc_restore(path, run_id), which puts back from
the pass's archive whatever the current head names and the store lacks. The planner is pinned in
test_owncloud_composite_gc_g358202 and the enumeration in test_owncloud_composite_gc_enumerate_g358202.

WHAT THESE PINS ARE FOR. Every destructive claim here is ABSENCE-shaped ('nothing was deleted', 'no S3 call')
or ORDER-shaped ('archived before deleted'), and each is also what a dead component produces (guard-4166), so
each runs against a call tap (`_Tap`) and carries a control that can fail: the same call with the flag set DOES
delete, the tap demonstrably records a delete, and every refusal is paired with the unchanged store. The
archive -> delete -> restore round trip (guard-1301) runs through the REAL writer and reader, with the sweep
landing between the writer's segment PUT (which answers 412, 'already there') and its head commit: the window
the whole archive exists to survive. Every scenario runs against an in-memory S3, moto, and moto with bucket
versioning on (what production is: a delete leaves a delete marker).

Coverage:
  1. inert by default: no flag, no S3 call; the flag names environments and is independent of the writer flag;
     a grace under the floor is refused before any call; refusals from the enumeration pass through and delete
     nothing
  2. a pass archives each object and reads it back, writes the receipt, then deletes by single calls and reads
     each delete back; every referenced object survives; the archive and receipt sit outside the governed roots
     and the segment directory; gzip encoding and metadata are archived verbatim
  3. an object the archive cannot verifiably hold is kept: bytes that do not hash to their name, a body that
     claims gzip and is not, a copy that reads back different; an archive that cannot be written deletes nothing
  4. the head is re-read before each batch (a re-referenced name is kept); a head that stops being a composite
     head abandons the deletes; a head that commits after the last re-read is repaired from the archive
  5. a refused delete raises and the receipt says so; a delete that does not take effect is not counted;
     max_delete caps the pass; a store with nothing to collect is not written to
  6. restore: only what the head names and the store lacks, idempotent, needs no flag, refuses a copy that no
     longer matches its receipt, and an unknown run is an error
  7. the round trip through the real writer and reader

File basename starts with ``test_`` so domain-leak-check.sh skips it.
"""
from __future__ import annotations

import hashlib
import json
import re
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
from test_owncloud_composite_gc_enumerate_g358202 import _names, _state  # noqa: E402
from test_owncloud_composite_read_g358202 import (  # noqa: E402,F401  (the doubles are shared)
    BUCKET, ENV_ID, REGION, REL, _MemS3, _Stream, _backend, _client_error, _isolate, _legacy, _mutated, _publish,
    _setup,
)
from test_owncloud_composite_write_g358202 import _another_mutation, _prepare  # noqa: E402

DAY = 86400.0
GRACE = comp.GC_GRACE_S
ENV_ROOT = ENV_ID + "/"


@pytest.fixture(autouse=True)
def _gc_flag_off(monkeypatch):
    monkeypatch.delenv(comp.GC_FLAG_ENV, raising=False)


@pytest.fixture
def gc_on(monkeypatch):
    monkeypatch.setenv(comp.GC_FLAG_ENV, ENV_ID)


@pytest.fixture(params=["mem", "mem-versioned", "moto", "moto-versioned"])
def s3(request, monkeypatch):
    if request.param in ("mem", "mem-versioned"):
        yield _MemS3(versioned=request.param == "mem-versioned")
        return
    pytest.importorskip("moto")
    import boto3  # noqa: PLC0415
    from moto import mock_aws  # noqa: PLC0415
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SECURITY_TOKEN", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(k, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        if request.param == "moto-versioned":
            client.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
        yield client


class _Tap:
    """Every S3 call the backend makes, in order, as (operation, key or prefix). `rule` runs a function around
    the first matching call: `before` (it may raise), `after` (only when the call succeeded), `finally` (whether
    or not it raised), or `instead` (its return value replaces the call, which is then never made)."""

    def __init__(self, real):
        self._real = real
        self.calls = []
        self._rules = []

    def rule(self, op, pred, fn, when="after"):
        self._rules.append({"op": op, "pred": pred, "fn": fn, "when": when, "fired": False})

    def _fire(self, op, kw, when):
        for r in self._rules:
            if r["op"] == op and r["when"] == when and not r["fired"] and r["pred"](kw):
                r["fired"] = True
                return True, r["fn"](kw)
        return False, None

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if not callable(attr):
            return attr

        def call(**kw):
            self.calls.append((name, kw.get("Key", kw.get("Prefix"))))
            self._fire(name, kw, "before")
            took, out = self._fire(name, kw, "instead")
            if took:
                return out
            try:
                out = attr(**kw)
            except Exception:
                self._fire(name, kw, "finally")
                raise
            self._fire(name, kw, "after")
            self._fire(name, kw, "finally")
            return out
        return call

    def ops(self, mark=0):
        return [op for op, _k in self.calls[mark:]]


def _key_is(key):
    return lambda kw: kw.get("Key") == key


def _is_archive(kw):
    return comp.GC_ARCHIVE_DIR in kw.get("Key", "") and "/objects/" in kw["Key"]


def _is_receipt(kw):
    return kw.get("Key", "").endswith("/RECEIPT.json")


def _exists(s3, key):
    try:
        s3.head_object(Bucket=BUCKET, Key=key)
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise
    return True


def _body(s3, key):
    return s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()


def _world(tmp_path, s3, changes=((2, 100),), gzip_segments=False):
    """A backend over a call tap, on an object store after the writer's commits: the base state, then one more
    commit per entry of `changes` (cumulative; each changes one goal, so the segment holding it is rewritten and
    its previous object is orphaned). `orphans` is every object some head named and the last head does not."""
    tap = _Tap(s3)
    be, p, key = _setup(tmp_path, tap)
    changed = []
    raw = _state()
    etag, head = _publish(s3, key, raw, gzip_segments=gzip_segments)
    published = set(_names(raw))
    raw0 = raw
    for change in changes:
        changed.append(change)
        raw = _state(*changed)
        etag, head = _publish(s3, key, raw, old_head=head, gzip_segments=gzip_segments)
        published |= _names(raw)
    final = _names(raw)
    return types.SimpleNamespace(be=be, p=p, key=key, tap=tap, s3=s3, raw=raw, raw0=raw0, head=head, etag=etag,
                                 final=final, orphans=sorted(published - final), tmp=tmp_path)


def _seg(w, name):
    return comp.segment_s3_key(w.key, name)


def _aged(w, names=None):
    return {n: time.time() - 100 * DAY for n in (w.orphans if names is None else names)}


def _apply(w, ledger=None, now=None, **kw):
    now = time.time() + GRACE + 10 if now is None else now
    return w.be.composite_gc_apply(w.p, _aged(w) if ledger is None else ledger, now, **kw)


def _archive_key(out, name):
    return comp.gc_archive_key(ENV_ROOT, out.run_id, REL, name)


def _commit_head_of(w, raw):
    """A writer commits the head for `raw` without PUTting any segment: every object it names must already
    exist (the writer found each present, answered 412, and skipped it)."""
    w.s3.put_object(Bucket=BUCKET, Key=w.key, Body=comp.pad_head(comp.split(raw).head))


# --- 1. inert by default; the gates -------------------------------------------------------------------------
def test_the_pass_is_inert_without_its_flag_and_makes_no_s3_call(s3, tmp_path):
    w = _world(tmp_path, s3)
    mark = len(w.tap.calls)
    out = _apply(w)
    assert out.stopped == "gc-not-enabled"
    assert out.plan is None and out.run_id is None and out.deleted == [] and out.restored == []
    assert w.tap.calls[mark:] == []
    assert all(_exists(s3, _seg(w, n)) for n in w.orphans)


def test_the_same_call_with_the_flag_set_does_collect(s3, tmp_path, gc_on):
    w = _world(tmp_path, s3)
    out = _apply(w)
    assert out.stopped is None and out.deleted == w.orphans and len(w.orphans) == 1
    assert not _exists(s3, _seg(w, w.orphans[0]))


def test_the_flag_names_environments_and_is_independent_of_the_writer_flag():
    rel = comp.ALLOWLIST[0]
    assert not comp.should_gc(rel, ENV_ID, {})
    assert comp.should_gc(rel, ENV_ID, {comp.GC_FLAG_ENV: ENV_ID})
    assert comp.should_gc(rel, ENV_ID, {comp.GC_FLAG_ENV: "other, %s" % ENV_ID.upper()})
    assert comp.should_gc(rel, ENV_ID, {comp.GC_FLAG_ENV: "*"})
    assert not comp.should_gc(rel, ENV_ID, {comp.GC_FLAG_ENV: "other-env"})
    assert not comp.should_gc(rel, ENV_ID, {comp.GC_FLAG_ENV: "1"})  # a legacy truthy value names no environment
    assert not comp.should_gc("world/not-on-the-allowlist.jsonl", ENV_ID, {comp.GC_FLAG_ENV: ENV_ID})
    assert not comp.should_gc(rel, ENV_ID, {comp.FLAG_ENV: ENV_ID})  # naming an environment for the writer licenses no delete
    assert not comp.should_composite(rel, ENV_ID, {comp.GC_FLAG_ENV: ENV_ID})  # and the other way round
    assert comp.GC_FLAG_ENV != comp.FLAG_ENV


@pytest.mark.parametrize("grace", [GRACE - 1, 7 * DAY, 0, -5, float("nan"), float("inf"), None, "14d", True])
def test_a_grace_under_the_floor_is_refused_before_any_call(s3, tmp_path, gc_on, grace):
    w = _world(tmp_path, s3)
    mark = len(w.tap.calls)
    out = _apply(w, grace_s=grace)
    assert out.stopped == "grace-below-floor" and out.deleted == [] and out.plan is None
    assert w.tap.calls[mark:] == []
    assert _exists(s3, _seg(w, w.orphans[0]))


@pytest.mark.parametrize("grace", [GRACE, GRACE + 1, 30 * DAY])
def test_a_grace_at_or_over_the_floor_proceeds(s3, tmp_path, gc_on, grace):
    w = _world(tmp_path, s3)
    out = _apply(w, grace_s=grace, now=time.time() + grace + 10)
    assert out.stopped is None and out.deleted == w.orphans


def test_the_default_grace_is_the_floor_so_the_default_is_always_allowed():
    assert comp.GC_GRACE_S == 14 * DAY and comp.gc_grace_ok(comp.GC_GRACE_S)
    assert not comp.gc_grace_ok(comp.GC_GRACE_S - 1)


def test_a_head_that_moves_during_the_enumeration_stops_the_pass_with_nothing_done(s3, tmp_path, gc_on):
    w = _world(tmp_path, s3)
    w.tap.rule("list_objects_v2", lambda kw: True,
               lambda kw: _publish(s3, w.key, _state((2, 100), (1, 5)), old_head=w.head))
    out = _apply(w)
    assert out.stopped == "head-moved-during-enumeration" and out.deleted == [] and out.run_id is None
    assert "list_objects_v2" in w.tap.ops() and "put_object" not in w.tap.ops() and "delete_object" not in w.tap.ops()
    assert _exists(s3, _seg(w, w.orphans[0]))


@pytest.mark.parametrize("breakage, why", [("missing", "head-missing"), ("whole", "not-a-head")])
def test_a_head_that_is_missing_or_not_a_head_stops_the_pass(s3, tmp_path, gc_on, breakage, why):
    w = _world(tmp_path, s3)
    if breakage == "missing":
        s3.delete_object(Bucket=BUCKET, Key=w.key)
    else:
        s3.put_object(Bucket=BUCKET, Key=w.key, Body=w.raw)
    out = _apply(w)
    assert out.stopped == why and out.deleted == []
    assert _exists(s3, _seg(w, w.orphans[0]))


# --- 2. a pass: archive, receipt, delete, read back ------------------------------------------------------
def test_a_pass_archives_each_orphan_byte_for_byte_deletes_it_and_keeps_every_referenced_object(s3, tmp_path, gc_on):
    w = _world(tmp_path, s3, changes=((2, 100), (3, 50)))
    assert len(w.orphans) == 2
    before = {n: _body(s3, _seg(w, n)) for n in w.orphans}
    out = _apply(w)
    assert out.stopped is None and sorted(out.deleted) == w.orphans and out.restored == [] and out.skipped == {}
    assert re.match(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$", out.run_id)
    for n in w.orphans:
        assert not _exists(s3, _seg(w, n))
        assert _body(s3, _archive_key(out, n)) == before[n]
    assert all(_exists(s3, _seg(w, n)) for n in w.final)
    assert w.be.read_authoritative_bytes(w.p) == w.raw  # the store still reads: the head's objects are all there


def test_every_archive_and_the_receipt_precede_the_first_delete_and_deletes_are_single_key_calls(s3, tmp_path, gc_on):
    w = _world(tmp_path, s3, changes=((2, 100), (3, 50)))
    mark = len(w.tap.calls)
    out = _apply(w)
    calls = w.tap.calls[mark:]
    ops = [op for op, _k in calls]
    deletes = [i for i, (op, _k) in enumerate(calls) if op == "delete_object"]
    archive_puts = [i for i, (op, k) in enumerate(calls) if op == "put_object" and "/objects/" in (k or "")]
    archive_gets = [i for i, (op, k) in enumerate(calls) if op == "get_object" and "/objects/" in (k or "")]
    receipt_puts = [i for i, (op, k) in enumerate(calls) if op == "put_object" and (k or "").endswith("/RECEIPT.json")]
    assert len(deletes) == 2 and len(archive_puts) == 2 and len(archive_gets) == 2  # the tap does record deletes
    assert max(archive_puts) < receipt_puts[0] < deletes[0]
    assert max(archive_gets) < receipt_puts[0]  # each copy was read back before anything was deleted
    assert "delete_objects" not in ops
    assert sorted(k for op, k in calls if op == "delete_object") == sorted(_seg(w, n) for n in w.orphans)
    head_reads_before_delete = [i for i, (op, k) in enumerate(calls[:deletes[0]]) if op == "get_object" and k == w.key]
    assert len(head_reads_before_delete) >= 2  # the enumeration's read, and the one before the first batch
    assert out.stopped is None


def test_the_receipt_is_a_top_level_RECEIPT_of_the_archive_run_naming_every_object(s3, tmp_path, gc_on):
    w = _world(tmp_path, s3, changes=((2, 100), (3, 50)))
    before = {n: _body(s3, _seg(w, n)) for n in w.orphans}
    now, ledger = time.time() + GRACE + 10, _aged(w)
    out = _apply(w, ledger=ledger, now=now)
    rkey = comp.gc_receipt_key(ENV_ROOT, out.run_id)
    run_prefix = "%s%s/%s/" % (ENV_ROOT, comp.GC_ARCHIVE_DIR, out.run_id)
    assert rkey == run_prefix + "RECEIPT.json"  # top level of the run, never inside the store it describes
    receipt = json.loads(_body(s3, rkey))
    assert receipt["kind"] == "composite-gc-archive" and receipt["run_id"] == out.run_id and receipt["store"] == REL
    assert receipt["status"] == "done" and receipt["head_key"] == w.key and receipt["grace_s"] == GRACE
    assert receipt["head_etag"] == w.etag and receipt["started"] == time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))
    assert sorted(receipt["objects"]) == w.orphans and sorted(receipt["deleted"]) == w.orphans
    assert receipt["restored"] == [] and receipt["skipped"] == {}
    for name, rec in receipt["objects"].items():
        assert rec["source_key"] == _seg(w, name) and rec["archive_key"] == _archive_key(out, name)
        assert rec["size"] == len(before[name]) and rec["md5"] == hashlib.md5(before[name]).hexdigest()
        assert rec["plain_md5"] in name and rec["first_seen"] == ledger[name]
        assert abs(rec["last_modified"] - time.time()) < 120 and rec["content_encoding"] is None
    assert "composite_gc_restore" in receipt["restore"] and "local mirror" in receipt["restore"]


def test_the_archive_and_receipt_are_outside_the_governed_roots_and_the_segment_directory(s3, tmp_path, gc_on):
    w = _world(tmp_path, s3)
    out = _apply(w)
    for key in (_archive_key(out, w.orphans[0]), comp.gc_receipt_key(ENV_ROOT, out.run_id)):
        rel = w.be._rel_of_key(key)
        assert rel.startswith(comp.GC_ARCHIVE_DIR + "/") and rel.split("/")[0] not in ("world", "meta", "agents")
        assert not key.startswith(comp.segment_s3_key(w.key, ""))
    enum = w.be.composite_gc_enumerate(w.p, {}, time.time())  # the archive is not part of what the next pass lists
    assert enum.plan.unknown == [] and enum.plan.counts["listed"] == len(w.final) and not enum.plan.refused


def test_gzip_encoding_and_metadata_are_archived_verbatim(s3, tmp_path, gc_on):
    w = _world(tmp_path, s3, gzip_segments=True)
    src = s3.get_object(Bucket=BUCKET, Key=_seg(w, w.orphans[0]))
    raw = src["Body"].read()
    assert src.get("ContentEncoding") == "gzip" and raw[:2] == b"\x1f\x8b" and src["Metadata"]  # the control: it is encoded
    out = _apply(w)
    assert out.deleted == w.orphans
    arch = s3.get_object(Bucket=BUCKET, Key=_archive_key(out, w.orphans[0]))
    assert arch["Body"].read() == raw
    assert arch.get("ContentEncoding") == "gzip" and arch["Metadata"] == src["Metadata"]


# --- 3. an object the archive cannot verifiably hold is kept ---------------------------------------------
def test_an_object_that_does_not_hash_to_its_name_or_claims_a_codec_it_lacks_is_kept_and_not_archived(s3, tmp_path, gc_on):
    w = _world(tmp_path, s3)
    liar = "asp-9/0.%s.jsonl" % ("a" * 32)
    fake_gzip = "asp-9/1.%s.jsonl" % ("b" * 32)
    s3.put_object(Bucket=BUCKET, Key=_seg(w, liar), Body=b'{"id": "not what the name says"}\n')
    s3.put_object(Bucket=BUCKET, Key=_seg(w, fake_gzip), Body=b"plain bytes\n", ContentEncoding="gzip")
    out = _apply(w, ledger=_aged(w, w.orphans + [liar, fake_gzip]))
    assert out.stopped is None and out.deleted == w.orphans  # the honest orphan went
    assert out.skipped == {liar: "content-does-not-match-its-name", fake_gzip: "undecodable"}
    for name in (liar, fake_gzip):
        assert _exists(s3, _seg(w, name)) and not _exists(s3, _archive_key(out, name))
        assert name in out.ledger  # still unreferenced: the next pass sees it again
    receipt = json.loads(_body(s3, comp.gc_receipt_key(ENV_ROOT, out.run_id)))
    assert receipt["skipped"] == out.skipped and sorted(receipt["objects"]) == w.orphans


def test_a_copy_that_reads_back_different_is_kept_and_its_source_is_not_deleted(s3, tmp_path, gc_on):
    w = _world(tmp_path, s3, changes=((2, 100), (3, 50)))
    bad = w.orphans[0]

    def corrupt(kw):
        obj = s3.get_object(**kw)
        body = obj["Body"].read()
        obj["Body"] = _Stream(bytes([body[0] ^ 1]) + body[1:])  # same length, one bit off
        return obj

    w.tap.rule("get_object", lambda kw: kw["Key"].endswith("/objects/%s/%s" % (REL, bad)), corrupt, when="instead")
    out = _apply(w)
    assert out.skipped == {bad: "archive-readback-mismatch"}
    assert out.deleted == [n for n in w.orphans if n != bad]
    assert _exists(s3, _seg(w, bad))


@pytest.mark.parametrize("code", ["AccessDenied", "InternalError"])
def test_an_archive_that_cannot_be_written_stops_the_pass_before_any_delete(s3, tmp_path, gc_on, code):
    from owncloud_backend import OwnCloudPermissionError  # noqa: PLC0415
    w = _world(tmp_path, s3, changes=((2, 100), (3, 50)))

    def refuse(kw):
        raise _client_error(code, "PutObject")

    w.tap.rule("put_object", _is_archive, refuse, when="before")
    with pytest.raises(OwnCloudPermissionError if code == "AccessDenied" else ClientError):
        _apply(w)
    assert "delete_object" not in w.tap.ops()
    assert all(_exists(s3, _seg(w, n)) for n in w.orphans)
    assert not any(k.endswith("/RECEIPT.json") for _op, k in w.tap.calls if k)


# --- 4. the head is re-read; the window is repaired ---------------------------------------------------------
def test_a_name_the_head_lists_again_before_the_batch_is_kept(s3, tmp_path, gc_on):
    w = _world(tmp_path, s3)
    orphan = w.orphans[0]
    w.tap.rule("put_object", _is_receipt, lambda kw: _commit_head_of(w, w.raw0), when="after")
    out = _apply(w)
    assert out.stopped is None and out.deleted == [] and out.skipped == {orphan: "re-referenced"}
    assert _exists(s3, _seg(w, orphan))
    assert w.be.read_authoritative_bytes(w.p) == w.raw0  # the store reads the state the writer committed
    assert json.loads(_body(s3, comp.gc_receipt_key(ENV_ROOT, out.run_id)))["skipped"] == out.skipped


def test_a_head_that_stops_being_a_composite_head_abandons_the_deletes(s3, tmp_path, gc_on):
    w = _world(tmp_path, s3, changes=((2, 100), (3, 50)))
    w.tap.rule("put_object", _is_receipt, lambda kw: s3.put_object(Bucket=BUCKET, Key=w.key, Body=w.raw), when="after")
    out = _apply(w)
    assert out.stopped == "head-changed-layout" and out.deleted == [] and out.restored == []
    assert all(_exists(s3, _seg(w, n)) for n in w.orphans)
    assert "delete_object" not in w.tap.ops()
    receipt = json.loads(_body(s3, comp.gc_receipt_key(ENV_ROOT, out.run_id)))
    assert receipt["status"] == "stopped: head-changed-layout"  # the final receipt says where the pass stopped


def test_a_head_committed_after_the_last_re_read_is_repaired_from_the_archive(s3, tmp_path, gc_on):
    w = _world(tmp_path, s3)
    orphan = w.orphans[0]
    before = _body(s3, _seg(w, orphan))
    w.tap.rule("delete_object", lambda kw: True, lambda kw: _commit_head_of(w, w.raw0), when="after")
    out = _apply(w)
    assert out.stopped is None and out.deleted == [orphan] and out.restored == [orphan]
    assert _body(s3, _seg(w, orphan)) == before  # back, byte for byte
    assert w.be.read_authoritative_bytes(w.p) == w.raw0
    receipt = json.loads(_body(s3, comp.gc_receipt_key(ENV_ROOT, out.run_id)))
    assert receipt["restored"] == [orphan] and receipt["status"] == "done"
    assert orphan not in out.ledger


# --- 5. failures, caps, and the quiet store --------------------------------------------------------------------
def test_a_refused_delete_raises_and_the_receipt_says_so(s3, tmp_path, gc_on):
    from owncloud_backend import OwnCloudPermissionError  # noqa: PLC0415
    w = _world(tmp_path, s3)

    def deny(kw):
        raise _client_error("AccessDenied", "DeleteObject")

    w.tap.rule("delete_object", lambda kw: True, deny, when="before")
    with pytest.raises(OwnCloudPermissionError):
        _apply(w)
    assert _exists(s3, _seg(w, w.orphans[0]))
    keys = [k for op, k in w.tap.calls if op == "put_object" and k and k.endswith("/RECEIPT.json")]
    assert len(keys) == 2  # before the first delete, and again at the end
    receipt = json.loads(_body(s3, keys[-1]))
    assert receipt["status"] == "stopped: OwnCloudPermissionError" and receipt["deleted"] == []
    assert _exists(s3, receipt["objects"][w.orphans[0]]["archive_key"])


def test_a_delete_that_does_not_take_effect_is_not_counted_as_deleted(s3, tmp_path, gc_on):
    w = _world(tmp_path, s3)
    w.tap.rule("delete_object", lambda kw: True, lambda kw: {}, when="instead")
    out = _apply(w)
    assert out.deleted == [] and out.skipped == {w.orphans[0]: "delete-not-effective"}
    assert _exists(s3, _seg(w, w.orphans[0])) and w.orphans[0] in out.ledger


def test_max_delete_caps_the_pass_and_the_rest_waits_in_the_ledger(s3, tmp_path, gc_on):
    w = _world(tmp_path, s3, changes=((2, 100), (3, 50)))
    out = _apply(w, max_delete=1)
    assert len(out.deleted) == 1 and out.deleted[0] in w.orphans
    (left,) = [n for n in w.orphans if n != out.deleted[0]]
    assert _exists(s3, _seg(w, left)) and left in out.ledger and out.deleted[0] not in out.ledger
    again = _apply(w, ledger=out.ledger, max_delete=1)
    assert again.deleted == [left]


@pytest.mark.parametrize("batch, head_read_between_deletes", [(1, True), (50, False)])
def test_the_head_is_re_read_before_each_batch_of_deletes(s3, tmp_path, gc_on, batch, head_read_between_deletes):
    w = _world(tmp_path, s3, changes=((2, 100), (3, 50)))
    mark = len(w.tap.calls)
    out = _apply(w, batch=batch)
    calls = w.tap.calls[mark:]
    deletes = [i for i, (op, _k) in enumerate(calls) if op == "delete_object"]
    assert len(deletes) == 2 and sorted(out.deleted) == w.orphans
    between = [k for op, k in calls[deletes[0]:deletes[1]] if op == "get_object" and k == w.key]
    assert bool(between) is head_read_between_deletes


@pytest.mark.parametrize("age", ["young", "none"])
def test_a_store_with_nothing_to_collect_is_not_written_to(s3, tmp_path, gc_on, age):
    w = _world(tmp_path, s3, changes=((2, 100),) if age == "young" else ())
    mark = len(w.tap.calls)
    out = _apply(w, ledger={})  # nothing has been seen unreferenced for a window: the orphan, if any, is young
    assert out.stopped is None and out.deleted == [] and out.run_id is None and out.plan.delete == []
    assert out.plan.counts["orphans"] == len(w.orphans)
    ops = set(w.tap.ops(mark))
    assert ops and ops <= {"get_object", "list_objects_v2", "head_object"}  # the pass read, and wrote nothing


def test_an_orphan_that_vanishes_between_the_listing_and_the_pass_is_skipped_not_fatal(s3, tmp_path, gc_on):
    w = _world(tmp_path, s3, changes=((2, 100), (3, 50)))
    vanished = w.orphans[0]

    def vanish(kw):
        raise _client_error("NoSuchKey", "GetObject")

    w.tap.rule("get_object", _key_is(_seg(w, vanished)), vanish, when="before")
    out = _apply(w)
    assert out.stopped is None and out.skipped == {vanished: "gone-since-listing"}
    assert out.deleted == [n for n in w.orphans if n != vanished]


def test_the_grace_reaches_the_planner(s3, tmp_path, gc_on):
    w = _world(tmp_path, s3)
    now = time.time() + 20 * DAY  # past the default window, inside a longer one
    held = _apply(w, now=now, grace_s=30 * DAY)
    assert held.stopped is None and held.deleted == [] and held.plan.counts["aged"] == 0
    assert _exists(s3, _seg(w, w.orphans[0]))
    assert _apply(w, now=now).deleted == w.orphans  # the control: the default window has passed


# --- 6. restore --------------------------------------------------------------------------------------------------
def test_restore_puts_back_only_what_the_head_names_and_the_store_lacks_and_is_idempotent(s3, tmp_path, gc_on):
    w = _world(tmp_path, s3)
    orphan = w.orphans[0]
    before = _body(s3, _seg(w, orphan))
    out = _apply(w)
    assert out.deleted == [orphan]
    assert w.be.composite_gc_restore(w.p, out.run_id) == []  # the head does not name it: it stays deleted
    assert not _exists(s3, _seg(w, orphan))
    _commit_head_of(w, w.raw0)  # now a writer's head names it
    assert w.be.composite_gc_restore(w.p, out.run_id) == [orphan]
    assert _body(s3, _seg(w, orphan)) == before
    assert w.be.composite_gc_restore(w.p, out.run_id) == []  # present now: nothing to do
    assert w.be.read_authoritative_bytes(w.p) == w.raw0


def test_restore_needs_no_gc_flag(s3, tmp_path, monkeypatch):
    w = _world(tmp_path, s3)
    monkeypatch.setenv(comp.GC_FLAG_ENV, ENV_ID)
    out = _apply(w)
    monkeypatch.delenv(comp.GC_FLAG_ENV)
    assert not comp.should_gc(REL, ENV_ID) and out.deleted == w.orphans  # the control: the flag is off now
    _commit_head_of(w, w.raw0)
    assert w.be.composite_gc_restore(w.p, out.run_id) == w.orphans


def test_restore_refuses_a_copy_that_no_longer_matches_its_receipt_and_puts_nothing_back(s3, tmp_path, gc_on):
    w = _world(tmp_path, s3)
    out = _apply(w)
    s3.put_object(Bucket=BUCKET, Key=_archive_key(out, w.orphans[0]), Body=b"tampered\n")
    _commit_head_of(w, w.raw0)
    with pytest.raises(comp.CompositeError):
        w.be.composite_gc_restore(w.p, out.run_id)
    assert not _exists(s3, _seg(w, w.orphans[0]))


def test_restore_puts_back_a_gzip_segment_with_its_encoding_and_metadata(s3, tmp_path, gc_on):
    w = _world(tmp_path, s3, gzip_segments=True)
    orphan = w.orphans[0]
    src = s3.get_object(Bucket=BUCKET, Key=_seg(w, orphan))
    raw, meta = src["Body"].read(), src["Metadata"]
    assert src.get("ContentEncoding") == "gzip" and meta  # the control: it is encoded
    out = _apply(w)
    assert out.deleted == [orphan]
    _commit_head_of(w, w.raw0)
    assert w.be.composite_gc_restore(w.p, out.run_id) == [orphan]
    back = s3.get_object(Bucket=BUCKET, Key=_seg(w, orphan))
    assert back["Body"].read() == raw and back.get("ContentEncoding") == "gzip" and back["Metadata"] == meta
    assert w.be.read_authoritative_bytes(w.p) == w.raw0


def test_restore_refuses_a_receipt_that_describes_another_store(s3, tmp_path, gc_on):
    w = _world(tmp_path, s3)
    out = _apply(w)
    rkey = comp.gc_receipt_key(ENV_ROOT, out.run_id)
    receipt = json.loads(_body(s3, rkey))
    rec = receipt["objects"][w.orphans[0]]
    store_dir = "/%s/%s/" % (comp.SEGMENT_DIR, REL.rsplit("/", 1)[1])
    rec["source_key"] = rec["source_key"].replace(store_dir, "/%s/other.jsonl/" % comp.SEGMENT_DIR)
    assert rec["source_key"] != _seg(w, w.orphans[0])  # the control: the edit took
    s3.put_object(Bucket=BUCKET, Key=rkey, Body=json.dumps(receipt).encode("utf-8"))
    _commit_head_of(w, w.raw0)
    with pytest.raises(comp.CompositeError):
        w.be.composite_gc_restore(w.p, out.run_id)
    assert not _exists(s3, _seg(w, w.orphans[0]))


def test_restore_of_a_run_with_no_receipt_is_an_error_not_an_empty_answer(s3, tmp_path):
    w = _world(tmp_path, s3)
    with pytest.raises(ClientError):
        w.be.composite_gc_restore(w.p, "19700101T000000Z-00000000")


def test_restore_refuses_a_store_that_is_not_composite(s3, tmp_path):
    w = _world(tmp_path, s3)
    with pytest.raises(comp.CompositeError):
        w.be.composite_gc_restore(tmp_path / "world" / "other.jsonl", "19700101T000000Z-00000000")


# --- 7. the round trip through the real writer and reader (guard-1301) ----------------------------------------
def _revert_world(s3, tmp_path, monkeypatch):
    """A store after the writer's commits, one orphan in it, and a writer about to revert to the state that
    orphan belongs to: that segment's PUT answers 412 (present). `sweeper` is another process, the box that
    sweeps; `sweep()` runs one pass of it over that orphan with a first sighting long ago, and records it."""
    monkeypatch.setenv(comp.FLAG_ENV, ENV_ID)  # the writer flag, for these tests only
    monkeypatch.setattr(comp, "MIN_RAW_BYTES", 1)
    tap = _Tap(s3)
    be, p, key = _prepare(tap, tmp_path)
    raw_b = _legacy(_mutated())
    be.write_bytes(p, raw_b)  # the migration write: a head over every segment
    raw_c = _another_mutation(_mutated, 3, 5)
    be.write_bytes(p, raw_c)  # steady state: one segment replaced, its predecessor orphaned
    (orphan,) = _names(raw_b) - _names(raw_c)
    sweeper = _backend(tmp_path, s3)
    swept = []

    def sweep(kw=None):
        swept.append(sweeper.composite_gc_apply(p, {orphan: time.time() - 100 * DAY}, time.time() + GRACE + 10))

    return types.SimpleNamespace(tap=tap, be=be, p=p, key=key, orphan=orphan, raw_b=raw_b, sweeper=sweeper,
                                 swept=swept, sweep=sweep, seg_key=comp.segment_s3_key(key, orphan))


def test_a_sweep_after_the_writers_412_is_repaired_by_the_writer_itself(s3, tmp_path, monkeypatch, gc_on):
    r = _revert_world(s3, tmp_path, monkeypatch)
    r.tap.rule("put_object", _key_is(r.seg_key), r.sweep, when="finally")  # the sweep lands right after the 412
    r.be.write_bytes(r.p, r.raw_b)  # a revert: its segment PUT answers 412 (present), the sweep runs, then the head commits
    (first,) = r.swept
    assert first.stopped is None and first.deleted == [r.orphan] and first.restored == []  # the head did not name it yet
    assert _exists(s3, r.seg_key)  # the writer looked, found it gone, and PUT it again before it committed its head
    assert r.sweeper.read_authoritative_bytes(r.p) == r.raw_b  # so there was no window at all
    assert r.sweeper.composite_gc_restore(r.p, first.run_id) == []  # and nothing for the restore to do


def test_a_sweep_between_the_writers_look_and_its_head_commit_is_still_repaired_from_the_archive(s3, tmp_path,
                                                                                               monkeypatch, gc_on):
    """The gap the writer's re-PUT does not close (guard-1301: pinned, not claimed away): the writer has just
    HEADed the object and found it young, the sweep then takes it, and the head commits over it."""
    r = _revert_world(s3, tmp_path, monkeypatch)
    r.tap.rule("head_object", _key_is(r.seg_key), r.sweep, when="after")
    r.be.write_bytes(r.p, r.raw_b)
    (first,) = r.swept
    assert first.stopped is None and first.deleted == [r.orphan] and first.restored == []  # the head did not name it yet
    assert not _exists(s3, r.seg_key)
    with pytest.raises(comp.CompositeError):
        r.sweeper.read_authoritative_bytes(r.p)  # the head names an object the sweep deleted: the window
    assert r.sweeper.composite_gc_restore(r.p, first.run_id) == [r.orphan]
    assert r.sweeper.read_authoritative_bytes(r.p) == r.raw_b  # repaired from the archive, byte for byte
    assert r.sweeper.composite_gc_restore(r.p, first.run_id) == []


# --- 8. the pure helpers ---------------------------------------------------------------------------------------
def test_head_object_names_is_what_the_planner_treats_as_referenced():
    raw = _state((2, 100))
    head = comp.split(raw).head
    assert comp.head_object_names(head) == _names(raw) and len(_names(raw)) == 3
    assert comp.head_object_names(comp.pad_head(head)) == _names(raw)
    with pytest.raises(comp.IntegrityError):
        comp.head_object_names(raw)


def test_the_run_id_binds_the_delete_set_in_any_order_and_the_start_second():
    a, b = "asp-1/0.%s.jsonl" % ("1" * 32), "asp-2/0.%s.jsonl" % ("2" * 32)
    assert comp.gc_run_id(0, [a, b]) == comp.gc_run_id(0, [b, a]) == "19700101T000000Z-" + comp.gc_run_id(0, [a, b])[-8:]
    assert comp.gc_run_id(0, [a]) != comp.gc_run_id(0, [a, b])
    assert comp.gc_run_id(0, [a, b]) != comp.gc_run_id(1, [a, b])
    assert comp.gc_run_id(1_900_000_000, [a, b]).startswith("20300317T174640Z-")


def test_the_archive_keys_sit_beside_the_governed_roots_not_inside_one():
    name = "asp-1/0.%s.jsonl" % ("1" * 32)
    assert comp.GC_ARCHIVE_DIR not in ("world", "meta", "agents", comp.SEGMENT_DIR)
    assert comp.gc_archive_key("e/", "r1", REL, name) == "e/%s/r1/objects/%s/%s" % (comp.GC_ARCHIVE_DIR, REL, name)
    assert comp.gc_receipt_key("e/", "r1") == "e/%s/r1/RECEIPT.json" % comp.GC_ARCHIVE_DIR
