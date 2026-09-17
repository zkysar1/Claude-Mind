"""sync_file passes the manifest baseline, so a single-writer in-place edit
lands through the PostToolUse hook lane instead of being refused (g-115-8029,
2026-09-15).

Measured before the fix (bravo, cc-13, 2026-09-15): seven of eight Edit-tool
writes to one tree node were refused by the hook lane with `coordination merge
REFUSED ... same-heading divergence`, and 54 of 111 hook pushes on that box
since 09-04. Cause: `sync_file` called `_sync_one` with NO `baseline_md5`, so
`s3_at_baseline` was structurally False and the terminal push gate routed
EVERY push of an object S3 already held into the union-merge lane; the
tree-node handler merges with an EMPTY base, so any same-heading body change
is a conflict. The 120 s sweep, which does pass the baseline, landed each
edit two minutes later via the fenced mirror_put. The fix makes the hook lane
classify exactly as the sweep does.

These run the production hook shape end-to-end — sync_file -> _sync_one ->
fenced mirror_put / union merge — against moto's real S3 conditional-write
semantics (same harness as test_merge_refusal_no_clobber_g115_8412.py), with
ONE manifest per simulated machine (RUNTIME_DIR switched per machine) so each
baseline is the machine's own, as in production.
"""
import hashlib
import json
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

NODE_REL = "knowledge/tree/system/g8029-baseline-fast-path-node.md"
KEY = "world/" + NODE_REL
V1 = b"# Node\n\n## Section\nA line v1\n"
V2 = b"# Node\n\n## Section\nA line v2, edited in place under the same heading\n"
V3 = b"# Node\n\n## Section\nA line v3, a second in-place edit\n"
B_APPEND = V1 + b"\n## B Extra\nB new section\n"


def _md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


@pytest.fixture(autouse=True)
def _default_machine_id(monkeypatch):
    monkeypatch.setenv("MACHINE_ID", "test-machine-ci")


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
    """One simulated machine: its own world cache root + backend, sharing the
    mock S3/DDB with every other machine (root_map so both map to the SAME
    `<env>/world/...` key — the shape two writers actually collide on)."""
    from owncloud_backend import OwnCloudBackend
    world_root = cloud["root"] / machine_id / "world"
    world_root.mkdir(parents=True, exist_ok=True)
    be = OwnCloudBackend(
        env_id=ENV_ID, bucket=BUCKET, lock_table=LOCKS,
        sessions_table=SESSIONS, root_map=[(world_root, "world")],
        machine_id=machine_id, region=REGION,
        s3=cloud["s3"], ddb=cloud["ddb"])
    node = world_root / NODE_REL
    node.parent.mkdir(parents=True, exist_ok=True)
    return be, node


def _act_as(monkeypatch, cloud, machine_id):
    """Point the sync manifest at THIS machine's runtime dir. _runtime_dir()
    reads RUNTIME_DIR at call time, and the backend's post-put stamp goes
    through the same helper, so switching the env var IS switching machines."""
    monkeypatch.setenv("RUNTIME_DIR",
                       str(cloud["root"] / machine_id / "_owncloud_rt"))


def _manifest(cloud, machine_id) -> dict:
    p = cloud["root"] / machine_id / "_owncloud_rt" / "owncloud-sync-manifest.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _s3_body(cloud, be, path) -> bytes:
    key = be._s3_key(path)
    return cloud["s3"].get_object(Bucket=BUCKET, Key=key)["Body"].read()


def _push(be, path) -> tuple[int, dict]:
    from owncloud_sync import sync_file
    stats: dict = {}
    rc = sync_file(be, path, dry_run=False, stats_out=stats)
    return rc, stats


def test_in_place_edit_lands_through_the_hook_lane_when_remote_is_at_baseline(
        cloud, monkeypatch, capsys):
    """THE FIX. A pushes v1 (S3 absent -> plain PUT; the backend stamps A's
    manifest). A then edits the SAME heading in place and pushes through the
    production hook lane: with the baseline read back, remote == baseline and
    local changed -> fenced mirror_put -> lands NOW, no merge attempted, no
    refusal. Before the fix this exact push was refused (baseline None ->
    union lane -> same-heading divergence)."""
    be_a, node = _machine(cloud, "A")
    _act_as(monkeypatch, cloud, "A")

    node.write_bytes(V1)
    rc, st = _push(be_a, node)
    assert rc == 0 and st.get("pushed") == 1
    # The backend's post-put stamp wrote the key sync_file will look up —
    # this pins that the two key shapes agree (a mismatch would silently
    # disable the fast path and this test would fail at the next assert).
    assert _manifest(cloud, "A").get(KEY, {}).get("md5") == _md5(V1)

    node.write_bytes(V2)
    rc, st = _push(be_a, node)
    err = capsys.readouterr().err
    assert rc == 0, err
    assert st.get("pushed") == 1, st
    assert st.get("errors", 0) == 0 and not st.get("pushed_merged"), st
    assert "union-merge push failed" not in err and "REFUSED" not in err
    assert _s3_body(cloud, be_a, node) == V2
    # Self-sustaining: the fenced put stamped the NEW baseline, so the next
    # in-place edit takes the fast path too (rapid successive edits).
    assert _manifest(cloud, "A")[KEY]["md5"] == _md5(V2)
    node.write_bytes(V3)
    rc, st = _push(be_a, node)
    assert rc == 0 and st.get("pushed") == 1, st
    assert _s3_body(cloud, be_a, node) == V3


def test_both_moved_since_baseline_still_unions_and_refuses_same_heading(
        cloud, monkeypatch, capsys):
    """SAFETY: the fast path never clobbers a peer. A pushes v1 (baseline).
    B appends a new section (its union lands). A then edits the same heading
    in place: local != baseline AND remote != baseline -> both moved -> the
    union runs exactly as before the fix, hits the same-heading divergence,
    and REFUSES with no write attempted: S3 keeps B's section, A's local
    keeps A's edit (frozen divergence for reader reconciliation)."""
    be_a, node_a = _machine(cloud, "A")
    be_b, node_b = _machine(cloud, "B")

    _act_as(monkeypatch, cloud, "A")
    node_a.write_bytes(V1)
    rc, st = _push(be_a, node_a)
    assert rc == 0 and st.get("pushed") == 1

    _act_as(monkeypatch, cloud, "B")
    node_b.write_bytes(B_APPEND)
    rc, st = _push(be_b, node_b)
    assert rc == 0 and st.get("pushed_merged") == 1, st
    assert b"## B Extra" in _s3_body(cloud, be_b, node_b)

    _act_as(monkeypatch, cloud, "A")
    capsys.readouterr()
    node_a.write_bytes(V2)
    rc, st = _push(be_a, node_a)
    err = capsys.readouterr().err
    assert rc == 1
    assert st.get("errors", 0) >= 1 and st.get("pushed", 0) == 0, st
    assert "union-merge push failed" in err and "REFUSED" in err
    remote = _s3_body(cloud, be_a, node_a)
    assert b"## B Extra" in remote and b"A line v1" in remote
    assert b"v2" not in remote
    assert node_a.read_bytes() == V2


def test_local_at_baseline_with_remote_moved_is_a_stale_skip_not_a_push(
        cloud, monkeypatch, capsys):
    """A's local is untouched since its last sync (local == baseline) while a
    peer moved the remote: the hook lane must NOT push the stale local over
    the peer's bytes. With the baseline it takes the sweep's clobber-safe
    stale skip (the periodic sweep, which runs with own-cloud authority,
    heals the stale cache by pulling). Before the fix this case ran a union
    that happened to be harmless; the skip is the deliberate shape."""
    be_a, node_a = _machine(cloud, "A")
    be_b, node_b = _machine(cloud, "B")

    _act_as(monkeypatch, cloud, "A")
    node_a.write_bytes(V1)
    rc, st = _push(be_a, node_a)
    assert rc == 0 and st.get("pushed") == 1

    _act_as(monkeypatch, cloud, "B")
    node_b.write_bytes(B_APPEND)
    rc, st = _push(be_b, node_b)
    assert rc == 0 and st.get("pushed_merged") == 1, st

    _act_as(monkeypatch, cloud, "A")
    capsys.readouterr()
    rc, st = _push(be_a, node_a)          # local still V1 == baseline
    err = capsys.readouterr().err
    assert rc == 0, err
    assert st.get("stale_skipped") == 1 and st.get("pushed", 0) == 0, st
    assert "peer wrote" in err
    assert b"## B Extra" in _s3_body(cloud, be_a, node_a)
    assert node_a.read_bytes() == V1


def test_no_manifest_entry_degrades_to_the_pre_fix_behaviour(
        cloud, monkeypatch, capsys):
    """DEGRADE PATH: a machine with no manifest entry for the key (never
    synced it) still passes baseline None, so a same-heading divergent push
    from a stale base is refused exactly as before the fix — the fix adds a
    fast path only where the baseline PROVES the remote did not move."""
    be_a, node_a = _machine(cloud, "A")
    be_b, node_b = _machine(cloud, "B")

    _act_as(monkeypatch, cloud, "A")
    node_a.write_bytes(V1)
    rc, st = _push(be_a, node_a)
    assert rc == 0 and st.get("pushed") == 1

    _act_as(monkeypatch, cloud, "B")
    assert KEY not in _manifest(cloud, "B")
    capsys.readouterr()
    node_b.write_bytes(V2)
    rc, st = _push(be_b, node_b)
    err = capsys.readouterr().err
    assert rc == 1 and st.get("pushed", 0) == 0, st
    assert "REFUSED" in err
    assert _s3_body(cloud, be_b, node_b) == V1
    assert node_b.read_bytes() == V2
