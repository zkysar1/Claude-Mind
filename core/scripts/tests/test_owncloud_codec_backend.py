"""moto-mocked tests for the own-cloud transport codec at the backend seam
(g-358-11): the READ side (unit 1 — OwnCloudBackend decodes gzip objects
transparently on every S3 read path, and the plaintext-md5 metadata keeps
the mirror / manifest byte-identity comparisons exact) and the WRITER (unit 3
— flag-gated + allowlisted encode at the two PUT sites, default OFF).

Read-side tests seed gzip objects with a raw boto3 client using
_owncloud_codec.put_kwargs — exactly the shape the writer emits — so they
prove reader-first readiness independent of the flag (g-328-39 ordering:
every reader fleet- and downstream-wide must decode BEFORE any writer flips).
Writer tests set OWNCLOUD_GZIP_STORES=<this fixture's ENV_ID> per-test via
monkeypatch (the flag names env-ids, never a bare boolean); the process
default stays OFF.

Read-side coverage:
  1. _refresh (read_bytes) decodes a gzip object -> plaintext in the mirror
  2. ...also with NO ContentEncoding/metadata (magic-byte authoritative)
  3. read_authoritative_bytes decodes
  4. _get_remote_raw decodes (merge handlers see plaintext; ETag is the CAS token)
  5. stat().plain_md5 is the plaintext md5 for a gzip object, None for plain
  6. _overwrite_decision returns "identical" for a plaintext mirror of a gzip
     object via plain-md5 metadata — with a FORCED-FAILURE CONTROL: get_object
     is stubbed to raise, so the test fails if the identical branch is not
     taken (a needless re-download would call it)
  7. a plain body under a gzip CLAIM raises CodecError from read_bytes and
     leaves the local mirror untouched (no garbage written)
  8. manifest baseline entries carry the S3 ETag (write path + refresh path)
  9. FORCED-FAILURE CONTROL (guard-3534): with the decode seam stubbed to
     identity, read_bytes returns the raw gzip bytes — proving tests 1-4
     exercise the seam and are not vacuously green
 10. plain objects round-trip byte-for-byte unchanged (no behavior change for
     the un-flipped fleet)

Harness mirrors test_owncloud_baseline_stamp.py (moto mock_aws + RUNTIME_DIR
isolation via the autouse fixture pattern). File basename starts with
``test_`` so domain-leak-check.sh skips it.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

moto = pytest.importorskip("moto")
import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_DIR = PROJECT_ROOT / "core" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import _owncloud_codec as codec  # noqa: E402

ENV_ID = "test-env"
BUCKET = "test-bucket"
LOCKS = "test-locks"
SESSIONS = "test-sessions"
REGION = "us-west-2"

PLAIN = b'{"id": "asp-001", "goals": []}\n' * 64
PLAIN_MD5 = hashlib.md5(PLAIN).hexdigest()
GZ = codec.encode(PLAIN)
GZ_MD5 = hashlib.md5(GZ).hexdigest()


@pytest.fixture(autouse=True)
def _default_machine_id(monkeypatch):
    monkeypatch.setenv("MACHINE_ID", "test-machine-ci")


@pytest.fixture(autouse=True)
def _isolate_sync_manifest(monkeypatch, tmp_path):
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "_owncloud_rt"))


@pytest.fixture
def aws_env(monkeypatch):
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
              "AWS_SECURITY_TOKEN", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(k, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


@pytest.fixture
def cloud(aws_env, tmp_path):
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        ddb = boto3.client("dynamodb", region_name=REGION)
        s3.create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": REGION})
        ddb.create_table(
            TableName=LOCKS,
            KeySchema=[{"AttributeName": "lock_key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "lock_key", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST")
        ddb.create_table(
            TableName=SESSIONS,
            KeySchema=[{"AttributeName": "session_key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "session_key", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST")
        yield {"s3": s3, "ddb": ddb, "root": tmp_path}


def _backend(cloud, machine_id="m1", **kw):
    from owncloud_backend import OwnCloudBackend
    return OwnCloudBackend(
        env_id=ENV_ID, bucket=BUCKET, lock_table=LOCKS,
        sessions_table=SESSIONS, cache_root=cloud["root"],
        machine_id=machine_id, region=REGION,
        s3=cloud["s3"], ddb=cloud["ddb"], **kw)


def _read_manifest(tmp_path):
    p = tmp_path / "_owncloud_rt" / "owncloud-sync-manifest.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def _seed_gzip(cloud, key, plain=PLAIN, *, with_headers=True):
    """Seed S3 with an ENCODED object exactly as the unit-3 writer will emit
    it (put_kwargs), or — with_headers=False — as a foreign copy that lost its
    ContentEncoding/metadata (magic bytes only)."""
    if with_headers:
        r = cloud["s3"].put_object(Bucket=BUCKET, Key=key, **codec.put_kwargs(plain))
    else:
        r = cloud["s3"].put_object(Bucket=BUCKET, Key=key, Body=codec.encode(plain))
    return r["ETag"]


# --- 1-2. _refresh decodes ---------------------------------------------------
def test_refresh_decodes_gzip_object_into_plaintext_mirror(cloud):
    b = _backend(cloud)
    p = cloud["root"] / "world" / "aspirations.jsonl"
    etag = _seed_gzip(cloud, b._s3_key(p))
    assert etag.strip('"') == GZ_MD5  # ETag digests the COMPRESSED bytes
    assert b.read_bytes(p) == PLAIN
    assert p.read_bytes() == PLAIN, "local mirror must hold DECODED bytes"
    assert b.read_text(p) == PLAIN.decode()
    # The in-process fence adopted the (compressed-bytes) ETag, unchanged.
    assert b._etags[b._s3_key(p)] == etag


def test_refresh_decodes_gzip_object_without_headers(cloud):
    """Magic-byte authoritative: a copy stripped of ContentEncoding/metadata
    still decodes (peer on older code / server-side copy)."""
    b = _backend(cloud)
    p = cloud["root"] / "world" / "reasoning-bank.jsonl"
    _seed_gzip(cloud, b._s3_key(p), with_headers=False)
    assert b.read_bytes(p, force_fresh=True) == PLAIN
    assert p.read_bytes() == PLAIN


# --- 3-4. the other two GET paths ------------------------------------------
def test_read_authoritative_bytes_decodes(cloud):
    b = _backend(cloud)
    p = cloud["root"] / "world" / "guardrails.jsonl"
    _seed_gzip(cloud, b._s3_key(p))
    assert b.read_authoritative_bytes(p) == PLAIN
    assert not p.exists(), "read_authoritative_bytes never touches the mirror"


def test_get_remote_raw_decodes_and_keeps_etag(cloud):
    b = _backend(cloud)
    p = cloud["root"] / "world" / "board" / "coordination.jsonl"
    key = b._s3_key(p)
    etag = _seed_gzip(cloud, key)
    body, got_etag = b._get_remote_raw(key)
    assert body == PLAIN
    assert got_etag == etag  # the CAS token is the real (compressed) ETag
    assert b._get_remote_raw(key + ".absent") == (b"", None)


# --- 5. stat().plain_md5 ---------------------------------------------------
def test_stat_plain_md5_for_gzip_and_none_for_plain(cloud):
    b = _backend(cloud)
    pg = cloud["root"] / "world" / "aspirations.jsonl"
    _seed_gzip(cloud, b._s3_key(pg))
    st = b.stat(pg)
    assert st.plain_md5 == PLAIN_MD5
    assert st.version.strip('"') == GZ_MD5 != PLAIN_MD5
    assert st.size == len(GZ)  # ContentLength is the stored (compressed) size

    pp = cloud["root"] / "world" / "pipeline.jsonl"
    b.write_bytes(pp, PLAIN)  # unit 1: the writer is still PLAIN
    st2 = b.stat(pp)
    assert st2.plain_md5 is None
    assert st2.version.strip('"') == PLAIN_MD5
    assert st2.size == len(PLAIN)


# --- 6. _overwrite_decision identical via plain-md5 (forced-failure control) --
def test_overwrite_decision_identical_via_plain_md5_skips_download(cloud, tmp_path):
    """A plaintext mirror of a gzip object, seen by a FRESH-PROCESS backend
    (empty in-process fences), must classify 'identical' — the ETag can never
    equal the plaintext md5, so only the plain-md5 metadata can prove it.
    Control: get_object is stubbed to RAISE, so a needless re-download (the
    pre-codec 'download' verdict) fails the test loudly."""
    b = _backend(cloud)
    p = cloud["root"] / "world" / "aspirations.jsonl"
    key = b._s3_key(p)
    etag = _seed_gzip(cloud, key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(PLAIN)  # local mirror already holds the plaintext

    # Direct classification.
    assert b._overwrite_decision(p, p, etag, remote_plain_md5=PLAIN_MD5) == "identical"
    # ...and the pre-codec view of the same object is NOT identical (positive
    # control for the metadata being what carries the verdict).
    assert b._overwrite_decision(p, p, etag, remote_plain_md5=None) != "identical"

    # End to end through _refresh, with the GET disabled.
    def _boom(**kw):
        raise AssertionError("identical verdict must not re-download: %r" % (kw,))
    b.s3.get_object = _boom  # forced-failure control
    got = b.read_bytes(p, force_fresh=True)
    assert got == PLAIN
    assert b._etags[key] == etag  # adopted the fence without a GET
    m = _read_manifest(tmp_path)
    assert m[b._rel(p)]["md5"] == PLAIN_MD5
    assert m[b._rel(p)]["etag"] == etag.strip('"')


# --- 7. claim without magic -> loud, mirror untouched ------------------------
def test_gzip_claim_without_magic_raises_and_leaves_mirror_untouched(cloud):
    b = _backend(cloud)
    p = cloud["root"] / "world" / "aspirations.jsonl"
    key = b._s3_key(p)
    stale = b'{"id": "asp-000"}\n'
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(stale)
    cloud["s3"].put_object(Bucket=BUCKET, Key=key, Body=PLAIN,
                           ContentEncoding="gzip")  # claim, no magic
    with pytest.raises(codec.CodecError):
        b.read_bytes(p, force_fresh=True)
    assert p.read_bytes() == stale, "a corrupt object must not overwrite the mirror"
    with pytest.raises(codec.CodecError):
        b.read_authoritative_bytes(p)


# --- 8. manifest baseline carries the ETag ---------------------------------
def test_manifest_baseline_records_etag_on_write_and_refresh(cloud, tmp_path):
    b = _backend(cloud)
    p = cloud["root"] / "world" / "aspirations.jsonl"
    res = b.write_bytes(p, PLAIN)
    m = _read_manifest(tmp_path)
    ent = m[b._rel(p)]
    assert ent["md5"] == PLAIN_MD5
    assert ent["etag"] == res.version.strip('"')
    assert isinstance(ent["mtime"], int)

    # A peer moves S3 to a gzip v2; a fresh-process backend pulls and stamps.
    v2 = PLAIN + b'{"id": "asp-002"}\n'
    etag2 = _seed_gzip(cloud, b._s3_key(p), plain=v2)
    b2 = _backend(cloud)
    assert b2.read_bytes(p, force_fresh=True) == v2
    ent2 = _read_manifest(tmp_path)[b._rel(p)]
    assert ent2["md5"] == hashlib.md5(v2).hexdigest()  # PLAINTEXT md5
    assert ent2["etag"] == etag2.strip('"')             # compressed-bytes ETag


# --- 9. forced-failure control for the decode seam (guard-3534) --------------
def test_control_stubbing_decode_seam_returns_raw_gzip(cloud, monkeypatch):
    """Proves tests 1-4 are sensitive to the seam: with the backend's decode
    hook replaced by identity, read_bytes returns the raw gzip bytes."""
    import owncloud_backend as ob
    b = _backend(cloud)
    p = cloud["root"] / "world" / "aspirations.jsonl"
    _seed_gzip(cloud, b._s3_key(p))
    monkeypatch.setattr(ob, "_codec_decode_response",
                        lambda resp, key="": resp["Body"].read())
    raw = b.read_bytes(p, force_fresh=True)
    assert raw != PLAIN and codec.is_gzip(raw) and raw == GZ


# --- 10. plain objects unchanged --------------------------------------------
def test_plain_objects_round_trip_unchanged(cloud):
    """The un-flipped fleet: plain write -> plain object -> plain read, byte
    for byte, ETag == md5(plain), no metadata."""
    b = _backend(cloud)
    p = cloud["root"] / "world" / "aspirations.jsonl"
    res = b.write_bytes(p, PLAIN)
    assert res.version.strip('"') == PLAIN_MD5
    h = cloud["s3"].head_object(Bucket=BUCKET, Key=b._s3_key(p))
    assert h.get("ContentEncoding") in (None, "")
    assert not (h.get("Metadata") or {})
    b2 = _backend(cloud)
    assert b2.read_bytes(p, force_fresh=True) == PLAIN
    assert b2.read_authoritative_bytes(p) == PLAIN
    assert b2._get_remote_raw(b._s3_key(p))[0] == PLAIN


# ============================================================================
# WRITER ( unit 3) — flag-gated, allowlisted encode at the two PUT
# sites (_put / _merge_reconcile_put). meta/gate-firings.jsonl records are used
# because every allowlisted store is merge-registered and gate-firings' handler
# (merge_append_only_jsonl) is a plain line union — the simplest to reason
# about. Lines are timestamped so the union's chronological sort is stable.
# (world/board/*.jsonl is NOT allowlisted in this first flip — see
# _owncloud_codec.BOARD_PATTERN_DEFERRED — which the test below pins.)
# ============================================================================
def _rec(i):
    return ('{"id": "msg-%03d", "timestamp": "2026-08-17T00:%02d:00", '
            '"text": "r%d"}\n' % (i, i, i)).encode()


V1 = b"".join(_rec(i) for i in range(1, 4))          # r1..r3
V2 = V1 + _rec(4)                                       # r1..r4 (local append)
V1_MD5 = hashlib.md5(V1).hexdigest()
V2_MD5 = hashlib.md5(V2).hexdigest()


def _head(cloud, key):
    return cloud["s3"].head_object(Bucket=BUCKET, Key=key)


def _raw(cloud, key):
    return cloud["s3"].get_object(Bucket=BUCKET, Key=key)["Body"].read()


@pytest.fixture
def gzip_on(monkeypatch):
    """Flip the writer for THIS fixture's deployment (ENV_ID) only — the flag
    names env-ids, never a bare boolean (see _owncloud_codec module doc)."""
    monkeypatch.setenv(codec.FLAG_ENV, ENV_ID)


def test_writer_default_off_body_kwargs_plain(cloud, monkeypatch):
    monkeypatch.delenv(codec.FLAG_ENV, raising=False)
    b = _backend(cloud)
    p = cloud["root"] / "world" / "aspirations.jsonl"
    assert b._body_kwargs(p, PLAIN) == {"Body": PLAIN}


def test_writer_flag_on_encodes_allowlisted_store(cloud, tmp_path, gzip_on):
    b = _backend(cloud)
    p = cloud["root"] / "meta" / "gate-firings.jsonl"
    key = b._s3_key(p)
    res = b.write_bytes(p, V1)                       # S3 absent -> plain _put path
    h = _head(cloud, key)
    assert h["ContentEncoding"] == "gzip"
    assert h["Metadata"] == {"plain-md5": V1_MD5, "codec": "gzip"}
    gz = codec.encode(V1)
    assert h["ContentLength"] == len(gz) < len(V1)
    assert _raw(cloud, key) == gz                    # wire bytes are gzip
    assert res.version.strip('"') == hashlib.md5(gz).hexdigest()
    # Everything above the wire is plaintext.
    assert p.read_bytes() == V1                      # local mirror
    assert b.read_bytes(p) == V1
    assert b.read_authoritative_bytes(p) == V1
    ent = _read_manifest(tmp_path)[b._rel(p)]
    assert ent["md5"] == V1_MD5 and ent["etag"] == res.version.strip('"')
    st = b.stat(p)
    assert st.plain_md5 == V1_MD5 and st.size == len(gz)


def test_writer_flag_on_leaves_non_allowlisted_store_plain(cloud, gzip_on):
    b = _backend(cloud)
    p = cloud["root"] / "world" / "knowledge" / "tree" / "_tree.yaml"
    body = b"nodes: {}\n"
    res = b.write_bytes(p, body)
    key = b._s3_key(p)
    h = _head(cloud, key)
    assert h.get("ContentEncoding") in (None, "")
    assert not (h.get("Metadata") or {})
    assert _raw(cloud, key) == body
    assert res.version.strip('"') == hashlib.md5(body).hexdigest()


def test_writer_leaves_board_plain_in_first_flip(cloud, gzip_on):
    """Board channels stay PLAIN even with the flag on for this env: peer
    deployments write into them with their own reader (BOARD_PATTERN_DEFERRED)."""
    b = _backend(cloud)
    p = cloud["root"] / "world" / "board" / "coordination.jsonl"
    b.write_bytes(p, V1)
    key = b._s3_key(p)
    assert _raw(cloud, key) == V1
    assert not (_head(cloud, key).get("Metadata") or {})


def test_writer_flag_naming_another_env_leaves_this_env_plain(cloud, monkeypatch):
    """THE CROSS-DEPLOYMENT HAZARD, at the backend: the flag names a PEER
    deployment, this backend's env_id is not listed -> plain PUT, even for an
    allowlisted store. (peer-board-post pins the peer's env_id on its backend,
    so the converse — our flag listing only OUR env — keeps a peer's board
    plain when we post to it.)"""
    monkeypatch.setenv(codec.FLAG_ENV, "some-other-deployment")
    b = _backend(cloud)                          # env_id == ENV_ID ("test-env")
    p = cloud["root"] / "meta" / "gate-firings.jsonl"
    b.write_bytes(p, V1)
    key = b._s3_key(p)
    h = _head(cloud, key)
    assert h.get("ContentEncoding") in (None, "")
    assert not (h.get("Metadata") or {})
    assert _raw(cloud, key) == V1
    # ...and a bare legacy boolean enables nothing at all.
    monkeypatch.setenv(codec.FLAG_ENV, "1")
    b.write_bytes(p, V2)
    assert _raw(cloud, key) == V2
    # ...while '*' encodes every env.
    monkeypatch.setenv(codec.FLAG_ENV, "*")
    b.write_bytes(p, V1)
    assert codec.is_gzip(_raw(cloud, key)) and b.read_authoritative_bytes(p) == V1


def test_merge_reconcile_put_encodes_union_over_gzip_remote(cloud, gzip_on):
    """Registered store, remote already gzip (r1..r3), local merge_put pushes
    r3..r5: the union lands ENCODED, decodes to r1..r5, and the merge handler
    saw plaintext on both sides (else the union would be garbage)."""
    b = _backend(cloud)
    p = cloud["root"] / "meta" / "gate-firings.jsonl"
    key = b._s3_key(p)
    _seed_gzip(cloud, key, plain=V1)
    local = b"".join(_rec(i) for i in range(3, 6))   # r3..r5 (r3 overlaps)
    res = b.merge_put(p, local)
    assert res is not None
    union = b"".join(_rec(i) for i in range(1, 6))   # r1..r5, chronological
    assert b.read_authoritative_bytes(p) == union
    assert p.read_bytes() == union
    h = _head(cloud, key)
    assert h["ContentEncoding"] == "gzip"
    assert h["Metadata"]["plain-md5"] == hashlib.md5(union).hexdigest()
    assert codec.is_gzip(_raw(cloud, key))


def test_fresh_process_write_over_gzip_object_merges_and_reencodes(cloud, gzip_on):
    """Box A (flag on) writes v1 gz. A FRESH-PROCESS backend (empty fences —
    daemon restart) writes r4: the W1 head-fence path finds the key present +
    registered -> merge-reconcile on the gz ETag -> union r1..r4, re-encoded."""
    b1 = _backend(cloud)
    p = cloud["root"] / "meta" / "gate-firings.jsonl"
    key = b1._s3_key(p)
    b1.write_bytes(p, V1)
    b2 = _backend(cloud)  # fresh process
    b2.write_bytes(p, _rec(4))
    assert b2.read_authoritative_bytes(p) == V2
    assert _head(cloud, key)["Metadata"]["plain-md5"] == V2_MD5
    b3 = _backend(cloud)
    assert b3.read_bytes(p) == V2


def test_interop_plain_writer_over_gzip_then_gzip_reader_reads_plain(cloud, monkeypatch):
    """Mixed fleet, reader-first: a box with the flag OFF (or an older writer)
    writes over a gz object with a plain PUT — the object's metadata goes with
    it, plain_md5 becomes None, and the flag-ON box still reads the plaintext
    correctly on its next fresh read (ETag rule for a plain object)."""
    monkeypatch.setenv(codec.FLAG_ENV, ENV_ID)
    b1 = _backend(cloud)
    p = cloud["root"] / "meta" / "gate-firings.jsonl"
    key = b1._s3_key(p)
    b1.write_bytes(p, V1)                       # gz on the wire
    assert codec.is_gzip(_raw(cloud, key))
    monkeypatch.setenv(codec.FLAG_ENV, "")      # peer: plain writer, new reader
    b2 = _backend(cloud)
    b2.write_bytes(p, _rec(4))                  # merge -> plain PUT of the union
    h = _head(cloud, key)
    assert h.get("ContentEncoding") in (None, "")
    assert not (h.get("Metadata") or {})
    assert _raw(cloud, key) == V2               # plain bytes now
    assert b2.stat(p).plain_md5 is None
    assert h["ETag"].strip('"') == V2_MD5
    monkeypatch.setenv(codec.FLAG_ENV, ENV_ID)  # back on box A
    assert b1.read_bytes(p, force_fresh=True) == V2   # download verdict, plaintext
    assert p.read_bytes() == V2


def test_sync_one_over_gzip_object_no_thrash_and_pushes_local_change(cloud, tmp_path, gzip_on):
    """End to end through owncloud_sync._sync_one against a real (moto)
    backend holding a gz object:
      tick 1: local == S3 (plaintext) -> in_sync, NO push (pre-codec this
              read 'diverged' every tick because ETag != md5)
      local append -> tick 2: local != baseline, S3 == baseline via plain_md5
              -> push (union merge, still encoded)
      tick 3: in_sync again with the new baseline (converged, no thrash)."""
    import owncloud_sync as sync
    b = _backend(cloud)
    p = cloud["root"] / "meta" / "gate-firings.jsonl"
    key = b._s3_key(p)
    b.write_bytes(p, V1)
    assert codec.is_gzip(_raw(cloud, key))

    def _stats():
        return {"scanned": 0, "in_sync": 0, "pushed": 0, "would_push": 0,
                "conflicts": 0, "errors": 0, "skipped_unchanged": 0,
                "stale_skipped": 0, "diverged_skipped": 0,
                "nobaseline_skipped": 0, "nobaseline_reconciled": 0,
                "multipart_deferred": 0, "pruned_agents": 0,
                "push_paths": [], "error_paths": []}

    s1 = _stats()
    got1 = sync._sync_one(b, p, dry_run=False, stats=s1, baseline_md5=V1_MD5,
                          multi_machine=True, own_cloud_authority=True)
    assert got1 == V1_MD5 and s1["in_sync"] == 1 and s1["pushed"] == 0
    assert _head(cloud, key)["Metadata"]["plain-md5"] == V1_MD5  # untouched

    p.write_bytes(V2)  # a local raw append (r4)
    s2 = _stats()
    got2 = sync._sync_one(b, p, dry_run=False, stats=s2, baseline_md5=V1_MD5,
                          multi_machine=True, own_cloud_authority=True)
    assert got2 == V2_MD5, s2
    assert s2.get("pushed_merged", 0) == 1, s2   # registered store -> union push
    h = _head(cloud, key)
    assert h["ContentEncoding"] == "gzip" and h["Metadata"]["plain-md5"] == V2_MD5
    assert b.read_authoritative_bytes(p) == V2

    s3 = _stats()
    got3 = sync._sync_one(b, p, dry_run=False, stats=s3, baseline_md5=V2_MD5,
                          multi_machine=True, own_cloud_authority=True)
    assert got3 == V2_MD5 and s3["in_sync"] == 1 and s3.get("pushed_merged", 0) == 0


def test_sync_one_peer_moved_gzip_object_pulls_plaintext(cloud, gzip_on):
    """Peer wrote gz v2; this box's local == baseline v1 -> the own-cloud
    stale-cache pull lands DECODED bytes locally and returns v2's plaintext md5
    as the new baseline."""
    import owncloud_sync as sync
    b = _backend(cloud)
    p = cloud["root"] / "meta" / "gate-firings.jsonl"
    key = b._s3_key(p)
    b.write_bytes(p, V1)
    _seed_gzip(cloud, key, plain=V2)            # peer moved S3 (gz v2)
    stats = {"scanned": 0, "in_sync": 0, "pushed": 0, "would_push": 0,
             "conflicts": 0, "errors": 0, "stale_skipped": 0,
             "diverged_skipped": 0, "nobaseline_skipped": 0,
             "nobaseline_reconciled": 0, "multipart_deferred": 0,
             "push_paths": [], "error_paths": []}
    b2 = _backend(cloud)                        # fresh process on this box
    got = sync._sync_one(b2, p, dry_run=False, stats=stats, baseline_md5=V1_MD5,
                         multi_machine=True, own_cloud_authority=True)
    assert got == V2_MD5, stats
    assert stats.get("stale_pulled", 0) == 1
    assert p.read_bytes() == V2                 # decoded, not gzip bytes


def test_control_stubbing_encode_gate_off_yields_plain_even_with_flag(cloud, gzip_on, monkeypatch):
    """Forced-failure control for the writer tests: with the gate stubbed to
    False, the same flag-on write lands PLAIN — so the encode assertions above
    are sensitive to the gate, not vacuously green."""
    import owncloud_backend as ob
    monkeypatch.setattr(ob, "_codec_should_encode", lambda rel, env_id: False)
    b = _backend(cloud)
    p = cloud["root"] / "meta" / "gate-firings.jsonl"
    b.write_bytes(p, V1)
    assert _raw(cloud, b._s3_key(p)) == V1
