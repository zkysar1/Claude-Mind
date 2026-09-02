"""moto-mocked tests for the  merge-noop skip in _merge_reconcile_put.

When the union handler's output is byte-identical to the remote object
(local is a subset of remote — the steady state of the cross-box merge echo),
the backend must NOT re-PUT: a byte-identical PUT creates a new version whose
ETag movement every peer's next refresh pulls and re-merges (~23x write
amplification measured on the hot stores, 2026-09-01 census). Instead the
backend converges the LOCAL side, adopts the observed remote ETag as the
fence, stamps the manifest baseline, and bumps the merge_noop_identical
counter.

Coverage:
  1. premise guard: line-union of (subset, remote) IS remote bytes verbatim
  2. skip fires: zero put_object calls, counter++, version == remote ETag,
     fence + manifest baseline adopted, _cas_writes NOT incremented
  3. local file already == merged -> NOT rewritten (mtime preserved; a
     rewrite would re-arm the next sweep cycle -> perpetual no-op loop)
  4. stale local file -> converged to the union on skip
  5. merged != remote (genuinely new records) -> normal fenced PUT still fires
  6. absent remote object (etag None) -> create PUT still fires (no skip)
  7. entered_from_conflict + identity -> counts as a RESOLVED conflict
  8. cas_metrics() exposes merge_noop_identical
  9. the real aspirations union handler self-stabilizes: h(c, c) == c for
     c = h(x, x) — the property that makes the echo die after one cycle

Harness mirrors test_owncloud_codec_backend.py (moto mock_aws, autouse
MACHINE_ID + RUNTIME_DIR isolation). File basename starts with ``test_`` so
domain-leak-check.sh skips it.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

moto = pytest.importorskip("moto")
boto3 = pytest.importorskip("boto3")
from moto import mock_aws  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_DIR = PROJECT_ROOT / "core" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

ENV_ID = "test-env"
BUCKET = "test-bucket"
LOCKS = "test-locks"
SESSIONS = "test-sessions"
REGION = "us-west-2"


def _rec(i):
    return ('{"id": "msg-%03d", "timestamp": "2026-08-17T00:%02d:00", '
            '"text": "r%d"}\n' % (i, i, i)).encode()


REMOTE = b"".join(_rec(i) for i in range(1, 4))      # r1..r3 (the S3 state)
SUBSET = b"".join(_rec(i) for i in range(1, 3))      # r1..r2 (local behind)


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
            AttributeDefinitions=[{"AttributeName": "lock_key",
                                   "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST")
        ddb.create_table(
            TableName=SESSIONS,
            KeySchema=[{"AttributeName": "session_key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "session_key",
                                   "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST")
        yield {"s3": s3, "ddb": ddb, "root": tmp_path}


def _backend(cloud, machine_id="m1", **kw):
    from owncloud_backend import OwnCloudBackend
    return OwnCloudBackend(
        env_id=ENV_ID, bucket=BUCKET, lock_table=LOCKS,
        sessions_table=SESSIONS, cache_root=cloud["root"],
        machine_id=machine_id, region=REGION,
        s3=cloud["s3"], ddb=cloud["ddb"], **kw)


def _handler():
    from coordination_merge import merge_handler_for
    h = merge_handler_for("gate-firings.jsonl")
    assert h is not None, "gate-firings.jsonl lost its merge registration"
    return h


class _PutCounter:
    """Counting proxy around s3.put_object — the no-PUT detector."""

    def __init__(self, real):
        self.real = real
        self.count = 0

    def __call__(self, **kw):
        self.count += 1
        return self.real(**kw)


def _seed(cloud, backend, path, content=REMOTE):
    """Create the remote object via the backend's own write path (key absent
    -> plain create, no merge) so wire format matches production, and return
    the seeded object's ETag."""
    backend.write_bytes(path, content)
    key = backend._s3_key(path)
    return cloud["s3"].head_object(Bucket=BUCKET, Key=key)["ETag"]


# --- 1. premise -------------------------------------------------------------
def test_premise_union_of_subset_is_remote_bytes():
    """The whole skip rests on this: for the line-union handler, merging a
    subset into remote returns remote VERBATIM. If serialization ever stops
    being byte-stable, this fails first and names the real problem."""
    h = _handler()
    assert h(SUBSET, REMOTE) == REMOTE
    assert h(REMOTE, REMOTE) == REMOTE


# --- 2. the skip ------------------------------------------------------------
def test_skip_no_put_when_merged_identical_to_remote(cloud, monkeypatch, tmp_path):
    b = _backend(cloud)
    p = cloud["root"] / "meta" / "gate-firings.jsonl"
    etag = _seed(cloud, b, p)
    b2 = _backend(cloud, machine_id="m2")   # fresh process, empty fences
    counter = _PutCounter(cloud["s3"].put_object)
    monkeypatch.setattr(cloud["s3"], "put_object", counter)

    res = b2.merge_put(p, SUBSET)

    assert counter.count == 0, "byte-identical merge must not PUT"
    assert res is not None and res.fallback_used is False
    assert res.version == etag, "skip must report the ADOPTED remote ETag"
    m = b2.cas_metrics()
    assert m["merge_noop_identical"] == 1
    assert m["writes"] == 0, "a skipped PUT is not a fenced write"
    assert m["conflicts"] == 0 and m["resolved"] == 0
    # fence adopted in-process
    assert b2._etags[b2._s3_key(p)] == etag
    # manifest baseline stamped against the union (skip must not leave the
    # persistent baseline stale — that would present as "unpushed local
    # writes" to the next sweep and re-trigger the merge forever)
    manifest = tmp_path / "_owncloud_rt" / "owncloud-sync-manifest.json"
    assert manifest.exists()
    import hashlib
    assert hashlib.md5(REMOTE).hexdigest() in manifest.read_text(encoding="utf-8")


def test_skip_preserves_mtime_when_local_already_merged(cloud, monkeypatch):
    """Local file already holds the union -> the skip must NOT rewrite it.
    A rewrite bumps mtime, the sweep sees a 'changed' file next cycle, and
    the no-op becomes a perpetual-motion loop."""
    b = _backend(cloud)
    p = cloud["root"] / "meta" / "gate-firings.jsonl"
    _seed(cloud, b, p)                       # local file == REMOTE
    past = 1_000_000_000
    os.utime(p, (past, past))
    counter = _PutCounter(cloud["s3"].put_object)
    monkeypatch.setattr(cloud["s3"], "put_object", counter)

    res = b.merge_put(p, SUBSET)

    assert counter.count == 0 and res is not None
    assert p.stat().st_mtime == past, "identical local must not be rewritten"
    assert p.read_bytes() == REMOTE


def test_skip_converges_stale_local_file(cloud, monkeypatch):
    """Local FILE behind remote -> the skip still converges it to the union
    (equivalent of the pull the dropped PUT used to imply)."""
    b = _backend(cloud)
    p = cloud["root"] / "meta" / "gate-firings.jsonl"
    _seed(cloud, b, p)
    p.write_bytes(SUBSET)                    # simulate a stale mirror
    counter = _PutCounter(cloud["s3"].put_object)
    monkeypatch.setattr(cloud["s3"], "put_object", counter)

    res = b.merge_put(p, SUBSET)

    assert counter.count == 0 and res is not None
    assert p.read_bytes() == REMOTE, "skip must converge the local mirror"


# --- controls: the skip must not over-fire ----------------------------------
def test_merge_with_new_records_still_puts(cloud, monkeypatch):
    b = _backend(cloud)
    p = cloud["root"] / "meta" / "gate-firings.jsonl"
    _seed(cloud, b, p)
    b2 = _backend(cloud, machine_id="m2")
    counter = _PutCounter(cloud["s3"].put_object)
    monkeypatch.setattr(cloud["s3"], "put_object", counter)

    local = b"".join(_rec(i) for i in range(2, 5))   # r2..r4 — r4 is NEW
    res = b2.merge_put(p, local)

    assert counter.count == 1, "a union that adds records must PUT"
    assert res is not None and res.fallback_used is False
    union = b"".join(_rec(i) for i in range(1, 5))
    assert b2.read_authoritative_bytes(p) == union
    m = b2.cas_metrics()
    assert m["merge_noop_identical"] == 0
    assert m["writes"] == 1


def test_absent_remote_object_still_creates(cloud, monkeypatch):
    """remote_etag None (no object yet) must never skip — the create PUT is
    how the store is born."""
    b = _backend(cloud)
    p = cloud["root"] / "meta" / "gate-firings.jsonl"
    counter = _PutCounter(cloud["s3"].put_object)
    monkeypatch.setattr(cloud["s3"], "put_object", counter)

    res = b.merge_put(p, SUBSET)

    assert counter.count == 1
    assert res is not None
    assert b.read_authoritative_bytes(p) == _handler()(SUBSET, b"")
    assert b.cas_metrics()["merge_noop_identical"] == 0


# --- conflict-entry identity ------------------------------------------------
def test_conflict_entry_resolved_by_identity(cloud, monkeypatch):
    """A 412 whose re-merge adds nothing (the racing writer already carried
    our records) resolves by identity: counted as a RESOLVED conflict, no
    further PUT."""
    b = _backend(cloud)
    p = cloud["root"] / "meta" / "gate-firings.jsonl"
    etag = _seed(cloud, b, p)
    counter = _PutCounter(cloud["s3"].put_object)
    monkeypatch.setattr(cloud["s3"], "put_object", counter)

    res = b._merge_reconcile_put(
        p, b._s3_key(p), b._local(p), SUBSET, _handler(),
        entered_from_conflict=True)

    assert counter.count == 0
    assert res.version == etag
    m = b.cas_metrics()
    assert m["merge_noop_identical"] == 1
    assert m["resolved"] == 1, "identity after a real 412 IS a resolution"


# --- metrics surface --------------------------------------------------------
def test_cas_metrics_carries_noop_counter(cloud):
    b = _backend(cloud)
    m = b.cas_metrics()
    assert m["merge_noop_identical"] == 0
    assert set(m) >= {"writes", "conflicts", "resolved", "conflict_rate",
                      "merge_noop_identical"}


# --- 9. the real hot-store handler self-stabilizes --------------------------
_ASP_FIXTURE = (
    b'{"id": "asp-001", "title": "Alpha lane", "status": "active", '
    b'"priority": "HIGH", "goals": [{"id": "g-001-01", "title": "one", '
    b'"status": "pending"}, {"id": "g-001-02", "title": "two", '
    b'"status": "completed", "completed_date": "2026-08-30"}]}\n'
    b'{"id": "asp-002", "title": "Beta lane", "status": "active", '
    b'"priority": "MEDIUM", "goals": [{"id": "g-002-01", "title": "rec", '
    b'"status": "pending", "recurring": true, "achievedCount": 3, '
    b'"lastAchievedAt": "2026-08-29T10:00:00"}]}\n'
)


def test_aspirations_handler_self_stabilizes_to_fixed_point():
    """The production echo dies only if the hot store's handler is identity
    on its own output: c = h(x, x) may re-serialize once, but h(c, c) MUST
    equal c byte-for-byte, or no steady state exists and the skip never
    engages. Pins the fixed-point property on a realistic fixture."""
    from coordination_merge import merge_handler_for
    h = merge_handler_for("aspirations.jsonl")
    assert h is not None, "aspirations.jsonl lost its merge registration"
    canonical = h(_ASP_FIXTURE, _ASP_FIXTURE)
    assert canonical, "handler returned empty for a non-empty store"
    assert h(canonical, canonical) == canonical
    # and the subset direction (the echo's actual shape): union of a
    # single-record subset into canonical is canonical, byte-for-byte
    first_line = canonical.split(b"\n", 1)[0] + b"\n"
    assert h(first_line, canonical) == canonical
