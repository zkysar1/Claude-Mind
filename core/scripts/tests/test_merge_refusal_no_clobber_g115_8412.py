"""Refused union-merge push does NOT clobber the peer's S3 edit ().

The open question this answers: when the tree-node section-union handler
REFUSES a push (same-heading divergence -> merge_tree_node_md returns None ->
_merge_reconcile_put raises ConflictError "no write attempted"), does a
non-merging write path below the decline then persist the local file anyway —
i.e. is a refused push secretly a whole-file overwrite?

Code answer (verified against HEAD, 2026-09-01): NO. The push lane's
merge gate (`_try_merge_put`, g-115-2297) discriminates three outcomes:
  _MERGE_NA  -> store not merge-registered: caller falls through to the
                fenced mirror_put (the only lane a blind PUT can ride).
  None       -> merge ATTEMPTED but failed (ConflictError included): counted
                as an error, this sweep SKIPS. mirror_put is never reached.
  md5        -> union merged + pushed.
A REFUSAL is the None arm, not the _MERGE_NA arm — so the fall-through PUT
is structurally unreachable for a merge-registered store. The historical
clobber (2026-07-16 03:09:14, meta/gate-firings.jsonl head replacement via
the sync_file lane) is exactly what g-115-2297 closed.

These tests prove it LIVE, end-to-end through the production hook shape:
sync_file -> _sync_one(multi_machine=False) -> _try_merge_put ->
OwnCloudBackend.merge_put -> _merge_reconcile_put -> merge_tree_node_md,
against moto's real S3 conditional-write semantics — two backends, two
machines, two cache roots, ONE shared S3 key. Sibling coverage
(test_owncloud_sync_merge_lanes.py) drives the lanes on a FakeBackend and
has no refusal-path test; this file is the real-backend refusal pin.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

moto = pytest.importorskip("moto")
boto3 = pytest.importorskip("boto3")
from moto import mock_aws  # noqa: E402

BUCKET = "zds-data"
LOCKS = "zds-locks"
SESSIONS = "zds-sessions"
REGION = "us-east-2"
ENV_ID = "ayoai-mind"

NODE_REL = "knowledge/tree/system/g8412-two-writer-node.md"
BASE = b"# Node\n\n## Section\nA line v1\n"


@pytest.fixture(autouse=True)
def _default_machine_id(monkeypatch):
    monkeypatch.setenv("MACHINE_ID", "test-machine-ci")


@pytest.fixture(autouse=True)
def _isolate_sync_manifest(monkeypatch, tmp_path):
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "_owncloud_rt"))


@pytest.fixture
def cloud(monkeypatch, tmp_path):
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
              "AWS_SECURITY_TOKEN", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(k, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        ddb = boto3.client("dynamodb", region_name=REGION)
        s3.create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": REGION})
        for table, key in ((LOCKS, "lock_key"), (SESSIONS, "session_key")):
            ddb.create_table(
                TableName=table,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[
                    {"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST")
        yield {"s3": s3, "ddb": ddb, "root": tmp_path}


def _machine(cloud, machine_id):
    """One simulated machine: its own world cache root + backend, sharing
    the mock S3/DDB with every other machine. root_map (not cache_root) so
    the local path maps to the SAME `<env>/world/...` S3 key on both boxes —
    the production shape two writers actually collide on."""
    from owncloud_backend import OwnCloudBackend
    world_root = cloud["root"] / machine_id / "world"
    world_root.mkdir(parents=True, exist_ok=True)
    be = OwnCloudBackend(
        env_id=ENV_ID, bucket=BUCKET, lock_table=LOCKS,
        sessions_table=SESSIONS, root_map=[(world_root, "world")],
        machine_id=machine_id, region=REGION,
        s3=cloud["s3"], ddb=cloud["ddb"])
    return be, world_root


def _s3_body(cloud, be, path) -> bytes:
    key = be._s3_key(path)
    return cloud["s3"].get_object(Bucket=BUCKET, Key=key)["Body"].read()


def _push(be, path) -> tuple[int, dict]:
    from owncloud_sync import sync_file
    stats: dict = {}
    rc = sync_file(be, path, dry_run=False, stats_out=stats)
    return rc, stats


def test_refused_merge_push_does_not_clobber_peer_edit(cloud, capsys):
    """THE ANSWER to 's open question. A pushes v1; B — holding a
    stale-base local — makes a DIVERGENT same-heading edit and pushes through
    the production sync_file lane. The handler refuses; the refusal must be a
    SKIP, not a fall-through whole-file PUT: S3 keeps A's bytes, B's local
    stays as B wrote it (frozen divergence), and the sweep counts an error."""
    be_a, root_a = _machine(cloud, "A")
    be_b, root_b = _machine(cloud, "B")
    node_a = root_a / NODE_REL
    node_b = root_b / NODE_REL
    node_a.parent.mkdir(parents=True, exist_ok=True)
    node_b.parent.mkdir(parents=True, exist_ok=True)

    # A authors v1 and pushes: S3 absent -> plain PUT lane.
    node_a.write_bytes(BASE)
    rc_a, stats_a = _push(be_a, node_a)
    assert rc_a == 0 and stats_a.get("pushed") == 1
    assert _s3_body(cloud, be_a, node_a) == BASE

    # B never pulled: stale base + divergent SAME-heading edit.
    b_bytes = b"# Node\n\n## Section\nB divergent line\n"
    node_b.write_bytes(b_bytes)
    rc_b, stats_b = _push(be_b, node_b)

    err = capsys.readouterr().err
    # The refusal is loud (union-merge push failed WARN), counted as an
    # error (rc=1), and NOTHING was written:
    assert rc_b == 1
    assert stats_b.get("errors", 0) >= 1
    assert stats_b.get("pushed", 0) == 0
    assert "union-merge push failed" in err
    assert "REFUSED" in err
    # S3 still holds A's edit — the peer's write SURVIVED the refused push.
    assert _s3_body(cloud, be_b, node_b) == BASE
    # B's local was not rewritten either — frozen divergence, both sides kept.
    assert node_b.read_bytes() == b_bytes


def test_mergeable_push_lands_union_positive_control(cloud):
    """POSITIVE CONTROL: the refusal above is same-heading divergence, not a
    dead merge lane. B holding A's content PLUS a new section pushes -> the
    union lands on S3 with both sides' content, via the same code path."""
    be_a, root_a = _machine(cloud, "A")
    be_b, root_b = _machine(cloud, "B")
    node_a = root_a / NODE_REL
    node_b = root_b / NODE_REL
    node_a.parent.mkdir(parents=True, exist_ok=True)
    node_b.parent.mkdir(parents=True, exist_ok=True)

    node_a.write_bytes(BASE)
    rc_a, _ = _push(be_a, node_a)
    assert rc_a == 0

    node_b.write_bytes(BASE + b"\n## B Extra\nB new section\n")
    rc_b, stats_b = _push(be_b, node_b)
    assert rc_b == 0
    assert stats_b.get("pushed_merged") == 1

    merged = _s3_body(cloud, be_b, node_b)
    assert b"A line v1" in merged
    assert b"## B Extra" in merged
