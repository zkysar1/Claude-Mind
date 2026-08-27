"""moto-mocked regression tests for the  per-write manifest
baseline stamp in OwnCloudBackend (_stamp_manifest_baseline).

Root fix for the cross-box lost-update lanes (tree node
system/daemon-only-architecture/owncloud-write-path-loss-lanes): _put and
_merge_reconcile_put success previously advanced only the IN-PROCESS etag
fence; the PERSISTENT sync-manifest baseline was stamped only by the periodic
owncloud_sync sweep (default 120s). In that window a peer-moved S3 object made
_overwrite_decision return "no_clobber" (local falsely read as unpushed) —
the state that let concurrent boxes read stale local inside the write lock,
mint duplicate goal ids, and clobber each other.

Coverage:
  1. _put success writes a manifest entry {mtime, md5(body)} keyed by _rel
  2. THE HEADLINE REGRESSION: after a _put, a fresh-process backend (empty
     in-process fences) whose peer moved S3 refreshes to "download" — the
     peer's bytes land locally (pre-fix: "no_clobber" kept stale local)
  3. stamp failure is fail-open: _save_manifest raising never fails the PUT
  4. merge-reconcile success path stamps the merged bytes

Harness mirrors test_owncloud_backend.py (moto mock_aws + RUNTIME_DIR
isolation via the autouse fixture pattern).
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

ENV_ID = "test-env"
BUCKET = "test-bucket"
LOCKS = "test-locks"
SESSIONS = "test-sessions"
REGION = "us-west-2"


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


def test_put_stamps_manifest_baseline(cloud, tmp_path):
    """_put success writes {mtime, md5(body)} under the _rel key."""
    b = _backend(cloud)
    p = cloud["root"] / "world" / "aspirations.jsonl"
    body = b'{"id": "asp-001"}\n'
    b.write_bytes(p, body)
    m = _read_manifest(tmp_path)
    rel = b._rel(p)
    assert rel in m, f"manifest missing baseline for {rel}; keys={list(m)}"
    assert m[rel]["md5"] == hashlib.md5(body).hexdigest()
    assert isinstance(m[rel]["mtime"], int)


def test_post_put_peer_move_refreshes_to_download(cloud, tmp_path):
    """THE HEADLINE REGRESSION (pre-fix: no_clobber kept stale local).

    Box A writes v1 (stamps baseline). A peer moves S3 to v2. A FRESH-PROCESS
    backend on box A (empty in-process fences — the daemon-restart state)
    refreshes: local == baseline (thanks to the stamp) and S3 moved, so the
    verdict is "download" and the peer's v2 lands locally. Without the stamp,
    local != (absent) baseline read as unpushed-local -> no_clobber -> stale
    local served inside write locks -> duplicate-id mints."""
    b1 = _backend(cloud)
    p = cloud["root"] / "world" / "aspirations.jsonl"
    v1 = b'{"id": "asp-001", "goals": []}\n'
    b1.write_bytes(p, v1)

    # Peer (another box) moves S3 to v2 via raw client.
    v2 = b'{"id": "asp-001", "goals": [{"id": "g-001-01"}]}\n'
    key = b1._s3_key(p)
    cloud["s3"].put_object(Bucket=BUCKET, Key=key, Body=v2)

    # Fresh-process backend: empty _etags/_cache_check (daemon restart).
    b2 = _backend(cloud)
    got = b2.refresh(p)
    assert p.read_bytes() == v2, (
        "fresh-process refresh after a peer move must pull S3 (download "
        "verdict) — stale local means the no_clobber false positive is back")


def test_stamp_failure_is_fail_open(cloud, tmp_path, monkeypatch):
    """A manifest stamp failure WARNs and never fails the PUT."""
    import owncloud_sync

    def _boom(_m):
        raise OSError("disk full (test)")

    monkeypatch.setattr(owncloud_sync, "_save_manifest", _boom)
    b = _backend(cloud)
    p = cloud["root"] / "world" / "x.jsonl"
    res = b.write_bytes(p, b"data\n")
    assert res.version, "PUT must succeed despite stamp failure"
    assert b.read_bytes(p) == b"data\n"
    assert _read_manifest(tmp_path) == {}  # stamp genuinely did not land


def test_merge_reconcile_stamps_merged_bytes(cloud, tmp_path, monkeypatch):
    """_merge_reconcile_put success stamps the MERGED bytes' md5."""
    b = _backend(cloud)
    p = cloud["root"] / "world" / "reasoning-bank.jsonl"  # registered store
    rec1 = b'{"id": "rb-001", "created": "2026-01-01", "title": "a"}\n'
    b.write_bytes(p, rec1)

    # Peer appends rec2 remotely.
    rec2 = b'{"id": "rb-002", "created": "2026-01-02", "title": "b"}\n'
    key = b._s3_key(p)
    cloud["s3"].put_object(Bucket=BUCKET, Key=key, Body=rec1 + rec2)

    # Force the both-diverged merge lane: mark the key diverged and write a
    # local-only rec3 through the backend.
    rec3 = b'{"id": "rb-003", "created": "2026-01-03", "title": "c"}\n'
    b._diverged_keys.add(key)
    b.write_bytes(p, rec1 + rec3)

    merged_local = p.read_bytes()
    # The reasoning-bank handler renumbers ids by stable (created, title)
    # identity, so assert on titles: all three records must survive the union.
    titles = {json.loads(l)["title"] for l in merged_local.decode().splitlines() if l.strip()}
    assert titles == {"a", "b", "c"}, f"merge must union all records; got {titles}"
    m = _read_manifest(tmp_path)
    rel = b._rel(p)
    assert rel in m
    assert m[rel]["md5"] == hashlib.md5(merged_local).hexdigest(), (
        "baseline must match the MERGED bytes that landed locally+S3")


def test_concurrent_different_path_stamps_both_survive(cloud, tmp_path, monkeypatch):
    """ regression: two backend threads stamping DIFFERENT governed
    paths must not drop each other's manifest entry.

    Pre-fix, _stamp_manifest_baseline did load->mutate->save with no
    cross-path serialization; daemon locks are per-PATH, so two threads
    writing different files could both load the SAME manifest snapshot and
    each save only its own key -> last-writer-wins silently dropped the other
    thread's baseline (re-arming that key's stale-baseline no_clobber window).
    The module-level _MANIFEST_STAMP_LOCK serializes the RMW.

    A start-barrier + a sleep injected into _load_manifest force the interleave
    window the lock must close: WITH the lock the 2nd thread blocks on the lock
    before it can load, so it sees the 1st thread's save and unions its own key;
    WITHOUT the lock both threads load the stale snapshot and one entry is lost.
    """
    import threading
    import time
    import owncloud_sync

    b = _backend(cloud)
    p1 = cloud["root"] / "world" / "aspirations.jsonl"
    p2 = cloud["root"] / "world" / "reasoning-bank.jsonl"

    # Pre-create the local mirrors so local.stat() inside the stamp succeeds.
    for p, body in ((p1, b"a\n"), (p2, b"b\n")):
        lp = b._local(p)
        lp.parent.mkdir(parents=True, exist_ok=True)
        lp.write_bytes(body)

    real_load = owncloud_sync._load_manifest

    def _slow_load():
        m = real_load()
        time.sleep(0.2)  # hold the load->save window open to force the interleave
        return m

    monkeypatch.setattr(owncloud_sync, "_load_manifest", _slow_load)

    start = threading.Barrier(2)
    errors = []

    def _stamp(path, body):
        try:
            start.wait(timeout=3.0)  # both threads enter the RMW together
            b._stamp_manifest_baseline(path, body)
        except Exception as e:  # pragma: no cover  # noqa: BLE001
            errors.append(e)

    t1 = threading.Thread(target=_stamp, args=(p1, b"a\n"))
    t2 = threading.Thread(target=_stamp, args=(p2, b"b\n"))
    t1.start()
    t2.start()
    t1.join(timeout=6)
    t2.join(timeout=6)

    assert not errors, f"concurrent stamp raised: {errors}"
    m = _read_manifest(tmp_path)
    assert b._rel(p1) in m and b._rel(p2) in m, (
        "both concurrent different-path stamps must survive the RMW; "
        f"manifest keys={list(m)} (a missing key => _MANIFEST_STAMP_LOCK "
        "regression)")


def test_merge_reconcile_partial_write_over_unmoved_remote_still_unions(
        cloud, tmp_path):
    """ TRIPWIRE — do NOT re-introduce a backend-level fast-forward
    short-circuit in _merge_reconcile_put.

    The tempting fix for the tree-node merge wedge is: "if the remote object
    still equals the manifest baseline this box last reconciled against, then
    S3 has not moved, so the outgoing bytes are strictly ahead and the handler
    can be skipped." That premise is FALSE, and this test is the counterexample.

    `remote == our baseline` does NOT imply `body` is a descendant of remote,
    because for every union-merged store the outgoing body is DELIBERATELY a
    PARTIAL write: an appender writes the one record it has and the handler is
    what makes the result whole. Skipping the handler there converts a partial
    append into a full-file clobber that silently discards every record the
    appender did not happen to hold.

    Measured 2026-08-26 (bravo, cc-05) by implementing exactly that
    short-circuit: test_owncloud_codec_backend.py's
    test_fresh_process_write_over_gzip_object_merges_and_reencodes and
    test_interop_plain_writer_over_gzip_then_gzip_reader_reads_plain both
    failed, each losing three records from meta/gate-firings.jsonl. The change
    was reverted.

    The real defect (merge_tree_node_md refusing a fast-forward) is a CONTENT
    question and can only be answered where the content is understood — in the
    handler, which would need a base it is not currently given. The backend
    layer cannot decide it: at this layer a partial append and a fast-forward
    are indistinguishable.
    """
    b = _backend(cloud)
    p = cloud["root"] / "world" / "reasoning-bank.jsonl"  # registered store
    rec1 = b'{"id": "rb-001", "created": "2026-01-01", "title": "a"}\n'
    rec2 = b'{"id": "rb-002", "created": "2026-01-02", "title": "b"}\n'
    b.write_bytes(p, rec1 + rec2)  # baseline := md5(rec1+rec2); S3 holds both

    # S3 has NOT moved since that push, so a baseline probe would read
    # "fast-forward". Now write a PARTIAL body holding only a NEW record.
    rec3 = b'{"id": "rb-003", "created": "2026-01-03", "title": "c"}\n'
    b._diverged_keys.add(b._s3_key(p))
    b.write_bytes(p, rec3)

    titles = {json.loads(l)["title"]
              for l in p.read_bytes().decode().splitlines() if l.strip()}
    assert titles == {"a", "b", "c"}, (
        "a partial write over an UNMOVED remote must still union — losing "
        f"records here means a fast-forward short-circuit is back; got {titles}")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
