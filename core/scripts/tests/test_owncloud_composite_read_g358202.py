"""Backend tests for the READ half of the composite goal-queue layout ( U2c).

THE SEAM: OwnCloudBackend._composite_whole, called from the three places that turn an S3
object into bytes: `_refresh` (the local mirror), `read_authoritative_bytes` and
`_get_remote_raw` (the remote side of a merge). A composite HEAD of an allowlisted store is
joined with its segment objects; anything else is returned untouched.

Every scenario runs against BOTH an in-memory S3 (no dependency, so these run on a box
without moto) and moto's S3 (real ETag, metadata and error semantics) when moto is importable.

WHAT THESE PINS ARE FOR. The read path's effect is a SHAPE of GETs (the head once, then only
the segments the mirror lacks), and an absence-shaped assertion is also what a dead component
produces (guard-4166). So each scenario asserts on the GET log of a spy and carries a control
that can fail: a plain object reads in exactly one GET, a head at a store off the allowlist is
not joined, a diverged mirror is not overwritten and fetches no segment.

Coverage:
  1. _refresh materializes the JOINED file, fences on the head's ETag, stamps the baseline
     with the md5 of the joined bytes
  2. a warm refresh GETs the head and exactly one segment; an unchanged store GETs nothing
  3. _get_remote_raw / read_authoritative_bytes return the joined file, touch no mirror
  4. controls: a plain or gzip whole object reads as before; a head off the allowlist is not
     joined; a diverged mirror is not overwritten (the join runs AFTER the no-clobber verdict)
  5. a collected segment (the head moved on) re-reads the head and adopts the NEWER ETag;
     a segment that stays missing raises and leaves the mirror and the fence alone
  6. the merge handler is handed the whole joined file; an old-style whole-object PUT over a
     head reverts the layout and the store stays readable
  7. pins: segment objects are excluded from the sync sweep, the allowlist is disjoint from the
     range-tail stores, and `_rel_of_key` inverts `_s3_key`

File basename starts with ``test_`` so domain-leak-check.sh skips it.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_DIR = PROJECT_ROOT / "core" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import _owncloud_codec as codec  # noqa: E402
import _owncloud_composite as comp  # noqa: E402

ENV_ID = "test-env"
BUCKET = "test-bucket"
REGION = "us-west-2"
REL = "world/aspirations.jsonl"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("MACHINE_ID", "test-machine-ci")
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "_owncloud_rt"))
    for name in ("OWNCLOUD_OBJECT_CACHE", "OWNCLOUD_GZIP_STORES", "OWNCLOUD_COMPOSITE_STORES"):
        monkeypatch.delenv(name, raising=False)


# --- the S3 doubles -------------------------------------------------------------------------------
class _Stream:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


def _etag(data):
    return '"%s"' % hashlib.md5(data).hexdigest()


def _client_error(code, op):
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


class _MemS3:
    """The few S3 calls the read and merge paths make, over a dict: quoted-md5 ETags, user
    metadata, 404 / NoSuchKey for an absent key, and If-Match (412) on put_object."""

    def __init__(self):
        self.objects = {}

    def put_object(self, *, Bucket, Key, Body, IfMatch=None, IfNoneMatch=None, Metadata=None,
                   ContentEncoding=None, **_kw):
        if IfMatch is not None and (Key not in self.objects or self.objects[Key]["ETag"] != IfMatch):
            raise _client_error("PreconditionFailed", "PutObject")
        if IfNoneMatch == "*" and Key in self.objects:
            raise _client_error("PreconditionFailed", "PutObject")
        rec = {"Body": bytes(Body), "ETag": _etag(Body), "Metadata": dict(Metadata or {}),
               "ContentEncoding": ContentEncoding}
        self.objects[Key] = rec
        return {"ETag": rec["ETag"]}

    def _shape(self, Key, op, missing_code):
        rec = self.objects.get(Key)
        if rec is None:
            raise _client_error(missing_code, op)
        out = {"ETag": rec["ETag"], "ContentLength": len(rec["Body"]), "Metadata": dict(rec["Metadata"])}
        if rec["ContentEncoding"]:
            out["ContentEncoding"] = rec["ContentEncoding"]
        return rec, out

    def head_object(self, *, Bucket, Key):
        return self._shape(Key, "HeadObject", "404")[1]

    def get_object(self, *, Bucket, Key):
        rec, out = self._shape(Key, "GetObject", "NoSuchKey")
        out["Body"] = _Stream(rec["Body"])
        return out

    def delete_object(self, *, Bucket, Key):
        self.objects.pop(Key, None)

    def list_objects_v2(self, *, Bucket, Prefix="", **_kw):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        return {"Contents": [{"Key": k} for k in keys], "KeyCount": len(keys), "IsTruncated": False}


class _Spy:
    """Records every head/get/put the backend issues. The GET SHAPE is the positive signal these
    tests assert on, never the mere absence of a GET (guard-4166)."""

    def __init__(self, real):
        self._real = real
        self.log = []  # ("head" | "get" | "put", key)
        self.puts = []  # one dict per put_object ATTEMPT: key, fences, encoding, metadata, body size
        self.after_first_get = {}  # key -> callable, run once right after that key's first GET

    def __getattr__(self, name):
        return getattr(self._real, name)

    def head_object(self, **kw):
        self.log.append(("head", kw["Key"]))
        return self._real.head_object(**kw)

    def put_object(self, **kw):
        self.log.append(("put", kw["Key"]))
        self.puts.append({"Key": kw["Key"], "IfMatch": kw.get("IfMatch"), "IfNoneMatch": kw.get("IfNoneMatch"),
                          "ContentEncoding": kw.get("ContentEncoding"), "Metadata": kw.get("Metadata"),
                          "bytes": len(kw["Body"])})
        return self._real.put_object(**kw)

    def get_object(self, **kw):
        self.log.append(("get", kw["Key"]))
        out = self._real.get_object(**kw)
        hook = self.after_first_get.pop(kw["Key"], None)
        if hook:
            hook()
        return out

    def since(self, op, mark):
        return [k for o, k in self.log[mark:] if o == op]


@pytest.fixture(params=["mem", "moto"])
def s3(request, monkeypatch):
    if request.param == "mem":
        yield _Spy(_MemS3())
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
        yield _Spy(client)


# --- fixtures: a goal-queue store, and what the U2d writer will leave in the object store ----------
def _legacy(records):
    return "".join(json.dumps(r, ensure_ascii=True) + "\n" for r in records).encode("ascii")


def _aspiration(asp, count):
    goals = [{"id": "g-%s-%d" % (asp, n), "title": "goal %d of %s" % (n, asp), "status": "pending",
              "priority": "MEDIUM", "description": "x" * 240} for n in range(1, count + 1)]
    goals.sort(key=lambda g: g["id"])  # plain string order, as the live file is
    return {"id": "asp-%s" % asp, "title": "aspiration %s" % asp, "status": "active", "goals": goals}


def _records():
    return [_aspiration(str(a), 240) for a in range(20)]  # 20 aspirations, one segment each


def _mutated():
    recs = _records()
    recs[7]["goals"][100]["status"] = "completed"
    return recs


def _backend(tmp_path, s3):
    from owncloud_backend import OwnCloudBackend  # noqa: PLC0415
    return OwnCloudBackend(env_id=ENV_ID, bucket=BUCKET, lock_table="test-locks",
                           sessions_table="test-sessions", cache_root=tmp_path, machine_id="m1",
                           region=REGION, s3=s3, ddb=object())


def _setup(tmp_path, s3):
    be = _backend(tmp_path, s3)
    p = tmp_path / "world" / "aspirations.jsonl"
    key = be._s3_key(p)
    assert key == "%s/%s" % (ENV_ID, REL)
    return be, p, key


def _publish(s3, key, raw, old_head=None, gzip_segments=False):
    """What the writer will do: the NEW segment objects first, then the head, whose metadata
    carries the plaintext md5 of the JOINED bytes. `gzip_segments` stores each segment through the
    gzip codec, as U2d will (g-358-202 outcome 6, C3); the head stays plain. Returns (head ETag,
    head bytes)."""
    plan = comp.plan_write(old_head, raw)
    for name, body in plan.segments.items():
        extra = codec.put_kwargs(body) if gzip_segments else {"Body": body}
        s3.put_object(Bucket=BUCKET, Key=comp.segment_s3_key(key, name), **extra)
    r = s3.put_object(Bucket=BUCKET, Key=key, Body=plan.head,
                      Metadata={codec.META_PLAIN_MD5: hashlib.md5(raw).hexdigest()})
    return r["ETag"], plan.head


def _manifest(tmp_path):
    p = tmp_path / "_owncloud_rt" / "owncloud-sync-manifest.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _is_segment_key(k):
    return "/%s/aspirations.jsonl/" % comp.SEGMENT_DIR in k


# --- 1-2. _refresh --------------------------------------------------------------------------------
def test_refresh_materializes_the_joined_file_not_the_head(s3, tmp_path):
    be, p, key = _setup(tmp_path, s3)
    raw = _legacy(_records())
    etag, _head = _publish(s3, key, raw)
    mark = len(s3.log)
    assert be.read_bytes(p, force_fresh=True) == raw
    assert p.read_bytes() == raw and not comp.is_head(p.read_bytes())
    assert be._etags[key] == etag  # the fence is the head's ETag
    gets = s3.since("get", mark)
    assert gets[0] == key and len([k for k in gets if _is_segment_key(k)]) == 20 and len(gets) == 21
    assert _manifest(tmp_path)[REL]["md5"] == hashlib.md5(raw).hexdigest()  # baseline = the JOINED bytes


def test_a_warm_refresh_gets_the_head_and_one_segment_and_moves_the_fence(s3, tmp_path):
    be, p, key = _setup(tmp_path, s3)
    old, new = _legacy(_records()), _legacy(_mutated())
    etag1, head1 = _publish(s3, key, old)
    assert be.read_bytes(p, force_fresh=True) == old
    etag2, _head2 = _publish(s3, key, new, head1)
    assert etag2 != etag1
    mark = len(s3.log)
    assert be.read_bytes(p, force_fresh=True) == new
    gets = s3.since("get", mark)
    assert len(gets) == 2 and gets[0] == key and "/asp-7/0." in gets[1] and _is_segment_key(gets[1])
    assert be._etags[key] == etag2 and p.read_bytes() == new


def test_refresh_of_an_unchanged_composite_store_reads_no_object_body(s3, tmp_path):
    be, p, key = _setup(tmp_path, s3)
    raw = _legacy(_records())
    _publish(s3, key, raw)
    assert be.read_bytes(p, force_fresh=True) == raw
    mark = len(s3.log)
    assert be.read_bytes(p, force_fresh=True) == raw
    assert s3.since("head", mark) == [key]  # the freshness HEAD ran (the positive signal) ...
    assert s3.since("get", mark) == []  # ... and nothing was downloaded


# --- 3. the other two read paths ------------------------------------------------------------------
def test_get_remote_raw_returns_the_joined_bytes_and_the_head_etag_without_touching_the_mirror(s3, tmp_path):
    be, p, key = _setup(tmp_path, s3)
    raw = _legacy(_records())
    etag, _head = _publish(s3, key, raw)
    assert be._get_remote_raw(key) == (raw, etag)
    assert not p.exists() and key not in be._etags
    assert be._get_remote_raw(key + ".absent") == (b"", None)


def test_read_authoritative_bytes_joins_without_touching_the_mirror(s3, tmp_path):
    be, p, key = _setup(tmp_path, s3)
    raw = _legacy(_records())
    _publish(s3, key, raw)
    assert be.read_authoritative_bytes(p) == raw
    assert not p.exists() and key not in be._etags and be._cache_check == {}


# --- 4. controls: nothing else changes -------------------------------------------------------------
def test_a_plain_or_gzip_whole_file_at_the_composite_key_reads_in_one_get(s3, tmp_path):
    be, p, key = _setup(tmp_path, s3)
    raw = _legacy(_records())
    s3.put_object(Bucket=BUCKET, Key=key, Body=raw)
    mark = len(s3.log)
    assert be.read_bytes(p, force_fresh=True) == raw
    assert s3.since("get", mark) == [key]
    new = _legacy(_mutated())  # other content: identical content would (correctly) skip the download
    s3.put_object(Bucket=BUCKET, Key=key, **codec.put_kwargs(new))
    mark = len(s3.log)
    assert be.read_bytes(p, force_fresh=True) == new
    assert s3.since("get", mark) == [key]


def test_a_head_shaped_body_off_the_allowlist_is_not_joined(s3, tmp_path):
    be = _backend(tmp_path, s3)
    other = tmp_path / "world" / "other-store.jsonl"
    okey = be._s3_key(other)
    head = comp.plan_write(None, _legacy(_records())).head
    assert comp.is_head(head)
    s3.put_object(Bucket=BUCKET, Key=okey, Body=head)
    mark = len(s3.log)
    assert be.read_bytes(other, force_fresh=True) == head  # the bytes as stored
    assert s3.since("get", mark) == [okey]


def test_a_diverged_mirror_is_not_overwritten_and_no_segment_is_fetched(s3, tmp_path):
    be, p, key = _setup(tmp_path, s3)
    a = _legacy(_records())
    _etag1, head1 = _publish(s3, key, a)
    assert be.read_bytes(p, force_fresh=True) == a  # the baseline is now A
    local_edit = _records()
    local_edit[3]["goals"][5]["title"] = "an unpushed local edit"
    p.write_bytes(_legacy(local_edit))
    _publish(s3, key, _legacy(_mutated()), head1)  # and a peer moved the store on
    mark = len(s3.log)
    assert be.read_bytes(p, force_fresh=True) == _legacy(local_edit)  # no_clobber: local stays
    assert key in be._diverged_keys
    assert s3.since("head", mark) == [key] and not any(_is_segment_key(k) for k in s3.since("get", mark))


# --- 5. the head moves on / a segment is gone ------------------------------------------------------
def test_a_collected_segment_makes_the_reader_reread_the_head_and_adopt_its_etag(s3, tmp_path):
    be, p, key = _setup(tmp_path, s3)
    a, b = _legacy(_records()), _legacy(_mutated())
    etag_a, head_a = _publish(s3, key, a)
    gone = sorted(set(comp.plan_write(None, a).segments) - set(comp.plan_write(None, b).segments))
    assert len(gone) == 1  # the control: exactly A's asp-7 segment is superseded
    committed = {}

    def writer_commits_b_and_collects():
        committed["etag"], _head = _publish(s3, key, b, head_a)
        s3.delete_object(Bucket=BUCKET, Key=comp.segment_s3_key(key, gone[0]))

    s3.after_first_get[key] = writer_commits_b_and_collects
    mark = len(s3.log)
    assert be.read_bytes(p, force_fresh=True) == b
    gets = s3.since("get", mark)
    assert comp.segment_s3_key(key, gone[0]) in gets  # the reader DID reach the collected object
    assert gets.count(key) == 2  # the head was read, then re-read
    assert be._etags[key] == committed["etag"] != etag_a  # the fence follows the bytes


def test_a_segment_that_stays_missing_raises_and_leaves_the_mirror_cold(s3, tmp_path):
    be, p, key = _setup(tmp_path, s3)
    a = _legacy(_records())
    _etag, _head = _publish(s3, key, a)
    victim = sorted(comp.plan_write(None, a).segments)[0]
    s3.delete_object(Bucket=BUCKET, Key=comp.segment_s3_key(key, victim))
    with pytest.raises(comp.IntegrityError, match="%d joins" % comp.READ_ATTEMPTS):
        be.read_bytes(p, force_fresh=True)
    assert not p.exists() and key not in be._etags


def test_a_failed_join_leaves_an_existing_mirror_and_fence_untouched(s3, tmp_path):
    be, p, key = _setup(tmp_path, s3)
    old, new = _legacy(_records()), _legacy(_mutated())
    etag_old, head_old = _publish(s3, key, old)
    assert be.read_bytes(p, force_fresh=True) == old
    _publish(s3, key, new, head_old)
    newest = sorted(set(comp.plan_write(None, new).segments) - set(comp.plan_write(None, old).segments))
    assert len(newest) == 1
    s3.delete_object(Bucket=BUCKET, Key=comp.segment_s3_key(key, newest[0]))  # the new head names a lost object
    with pytest.raises(comp.IntegrityError):
        be.read_bytes(p, force_fresh=True)
    assert p.read_bytes() == old  # no partial write
    assert be._etags[key] == etag_old


def test_a_corrupt_segment_object_is_a_loud_integrity_error_not_a_codec_error(s3, tmp_path):
    be, p, key = _setup(tmp_path, s3)
    a = _legacy(_records())
    _publish(s3, key, a)
    victim = sorted(comp.plan_write(None, a).segments)[0]
    # an object that claims gzip but carries no gzip magic is corrupt or partial
    s3.put_object(Bucket=BUCKET, Key=comp.segment_s3_key(key, victim), Body=b"not gzip at all\n",
                  ContentEncoding="gzip")
    with pytest.raises(comp.IntegrityError, match="%d joins" % comp.READ_ATTEMPTS):
        be.read_bytes(p, force_fresh=True)
    assert not p.exists() and key not in be._etags


def test_gzip_encoded_segments_join_to_the_same_file_as_plain_ones(s3, tmp_path):
    # U2d sends segments through the gzip codec and leaves the head plain ( outcome 6, C3),
    # so the reader must decode a segment by its own encoding, whatever its key.
    be, p, key = _setup(tmp_path, s3)
    raw = _legacy(_records())
    etag, _head = _publish(s3, key, raw, gzip_segments=True)
    victim = sorted(comp.plan_write(None, raw).segments)[0]
    stored = s3.get_object(Bucket=BUCKET, Key=comp.segment_s3_key(key, victim))
    # the control: the segment really is stored as gzip, so a reader that skipped the codec would fail
    assert stored["ContentEncoding"] == "gzip" and stored["Body"].read()[:2] == b"\x1f\x8b"
    assert be._get_remote_raw(key) == (raw, etag)
    assert be.read_authoritative_bytes(p) == raw
    assert be.read_bytes(p, force_fresh=True) == raw
    assert p.read_bytes() == raw and be._etags[key] == etag


# --- 6. the merge path ----------------------------------------------------------------------------
def test_merge_reconcile_hands_the_handler_the_joined_file_and_a_whole_put_reverts_the_layout(s3, tmp_path):
    be, p, key = _setup(tmp_path, s3)
    remote, local = _legacy(_records()), _legacy(_mutated())
    _publish(s3, key, remote)
    seen = {}

    def local_wins(outgoing, remote_bytes):
        seen["remote"] = remote_bytes
        return outgoing

    be._merge_reconcile_put(p, key, be._local(p), local, local_wins)
    assert seen["remote"] == remote  # the whole joined file, never a head or a segment
    stored = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    assert stored == local and not comp.is_head(stored)  # an old-style whole PUT replaced the head
    assert be.read_authoritative_bytes(p) == local  # and the store is still readable


def test_merge_reconcile_identity_skip_compares_against_the_joined_file(s3, tmp_path):
    be, p, key = _setup(tmp_path, s3)
    remote = _legacy(_records())
    etag, _head = _publish(s3, key, remote)
    mark = len(s3.log)
    be._merge_reconcile_put(p, key, be._local(p), _legacy(_mutated()), lambda outgoing, remote_bytes: remote_bytes)
    assert s3.since("put", mark) == []  # merged == the JOINED remote, so no PUT
    assert be._etags[key] == etag and p.read_bytes() == remote
    assert _manifest(tmp_path)[REL]["md5"] == hashlib.md5(remote).hexdigest()


# --- 7. pins ---------------------------------------------------------------------------------------
def test_segment_objects_are_excluded_from_the_sync_sweep():
    import owncloud_sync as sync  # noqa: PLC0415
    seg = comp.segment_s3_key("%s/%s" % (ENV_ID, REL), comp.segment_object_name("asp-1/0", "a" * 32))
    assert sync._is_excluded_dir(comp.SEGMENT_DIR)
    # the flat-LIST pull's own filter (pull_sweep): any excluded directory segment
    assert any(sync._is_excluded_dir(part) for part in seg.split("/")[:-1])
    # the control: the head's own key is an ordinary governed file
    assert not any(sync._is_excluded_dir(part) for part in ("%s/%s" % (ENV_ID, REL)).split("/")[:-1])


def test_the_composite_allowlist_is_disjoint_from_the_range_tail_stores():
    import owncloud_backend as ob  # noqa: PLC0415
    for rel in comp.ALLOWLIST:
        assert not any(rel == s or rel.startswith(s) for s in ob._RANGE_TAIL_STORES), rel


def test_rel_of_key_inverts_s3_key_in_the_default_and_a_customer_context(tmp_path):
    from owncloud_backend import reset_customer, set_customer  # noqa: PLC0415
    be = _backend(tmp_path, _MemS3())
    p = tmp_path / "world" / "aspirations.jsonl"
    assert be._rel_of_key(be._s3_key(p)) == REL
    token = set_customer("acme")
    try:
        key = be._s3_key(p)
        assert key == "acme/%s/%s" % (ENV_ID, REL) and be._rel_of_key(key) == REL
    finally:
        reset_customer(token)
    assert be._rel_of_key("other-env/%s" % REL) == ""
