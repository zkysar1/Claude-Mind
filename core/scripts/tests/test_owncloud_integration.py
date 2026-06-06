"""End-to-end integration test: _fileops public ops routed through OwnCloudBackend.

Flips STORAGE_BACKEND=own-cloud and drives the REAL _fileops public
functions so the whole seam is exercised — get_backend() ->
OwnCloudBackend.from_env() -> boto3 -> moto. Proves writes/appends land as S3
objects, locks land as DDB items, and in-lock reads are force-fresh from S3.
100% local: moto intercepts boto3 with fake creds; no real AWS, no network.

s3 step 3 completed the reroute. The following _fileops paths now route through
the active backend (append_jsonl_record for appends, refresh() before in-lock
reads): locked_append_jsonl, locked_append_jsonl_with_allocator,
locked_modify_jsonl (read+write), locked_modify_yaml (read+write).

DELIBERATELY STILL LOCAL (documented deferrals, NOT oversights):
  - append_changelog  — changelog.jsonl is 150K+ lines, appended every write;
    OwnCloudBackend.append_jsonl_record is O(N), so per-entry cloud routing is a
    perf cliff. Needs a bulk-sync/rotation design (lodestar changelog follow-up).
  - log_script_decision — uses json.dumps(default=str); the backend append has
    no default=str. Fire-and-forget local audit log; low-priority follow-up.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # core/scripts

moto = pytest.importorskip("moto")
boto3 = pytest.importorskip("boto3")
from moto import mock_aws  # noqa: E402

import _fileops  # noqa: E402
from storage_backend import reset_backend_for_tests  # noqa: E402

BUCKET = "zds-data"
LOCKS = "zds-locks"
SESSIONS = "zds-sessions"
REGION = "us-east-2"
ENV_ID = "ayoai-mind"


@pytest.fixture
def seam(monkeypatch, tmp_path):
    """Own-cloud seam wired end-to-end. Sequence matters (Landmine 6): set ALL
    env -> reset singleton -> enter mock_aws -> create bucket/tables -> yield.
    The test body runs INSIDE mock_aws, so the lazy boto3.client() calls in
    from_env() (triggered by the first _fileops op) are intercepted by moto."""
    world = tmp_path / "world"; world.mkdir()
    meta = tmp_path / "meta"; meta.mkdir()
    agents = tmp_path / "agents"; agents.mkdir()
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
              "AWS_SECURITY_TOKEN", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(k, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    # This seam exercises the _fileops reroute (lock + atomic_write through the
    # backend), not credential resolution. Opt into the default  chain
    # (moto's fake creds) so the fail-closed MIND_AWS_* guard in from_env does
    # not fire — the cred gate is covered by test_owncloud_backend.py.
    monkeypatch.setenv("MIND_AWS_ALLOW_DEFAULT_CHAIN", "1")
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    monkeypatch.setenv("STORAGE_S3_BUCKET", BUCKET)
    monkeypatch.setenv("STORAGE_DDB_LOCK_TABLE", LOCKS)
    monkeypatch.setenv("STORAGE_DDB_SESSIONS_TABLE", SESSIONS)
    monkeypatch.setenv("ENVIRONMENT_ID", ENV_ID)
    monkeypatch.setenv("MACHINE_ID", "test-machine-ci")  # G5: from_env fail-closes without it
    monkeypatch.setenv("MIND_WORLD", str(world))
    monkeypatch.setenv("MIND_META", str(meta))
    monkeypatch.setenv("AGENTS_ROOT", str(agents))
    # _fileops.WORLD_DIR is frozen at import (the real agent's world, or None);
    # tmp paths are under neither, so resolve_base_dir() -> None and the
    # raw-local save_history/append_changelog side paths stay dormant. That
    # isolates the assertions to the backend-routed lock + atomic_write seam.
    reset_backend_for_tests()
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        ddb = boto3.client("dynamodb", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET,
                         CreateBucketConfiguration={"LocationConstraint": REGION})
        for tbl, pk in ((LOCKS, "lock_key"), (SESSIONS, "session_key")):
            ddb.create_table(
                TableName=tbl,
                KeySchema=[{"AttributeName": pk, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": pk, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST")
        yield {"s3": s3, "ddb": ddb, "world": world, "meta": meta, "agents": agents}
    reset_backend_for_tests()


def _s3_lines(seam, key):
    body = seam["s3"].get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8")
    return [json.loads(ln) for ln in body.splitlines() if ln.strip()]


# --- write path routes through the cloud backend ----------------------------
def test_locked_write_jsonl_lands_in_s3(seam):
    p = seam["world"] / "store.jsonl"
    _fileops.locked_write_jsonl(p, [{"id": 1}, {"id": 2}])
    # Proof of routing: the object is in S3 at the env-scoped key.
    assert _s3_lines(seam, "ayoai-mind/world/store.jsonl") == [{"id": 1}, {"id": 2}]
    assert p.exists()  # local cache mirror also written (backend writes local-first)


def test_locked_modify_jsonl_write_lands_in_s3(seam):
    p = seam["world"] / "store.jsonl"
    _fileops.locked_write_jsonl(p, [{"id": 1}])
    _fileops.locked_modify_jsonl(p, lambda items: items + [{"id": 2}])
    assert _s3_lines(seam, "ayoai-mind/world/store.jsonl") == [{"id": 1}, {"id": 2}]


# --- lock path routes through the cloud backend (DDB, not a local .lock file)-
def test_acquire_release_lock_uses_ddb(seam):
    lock_path = seam["world"] / "r.lock"
    key = {"lock_key": {"S": "ayoai-mind/world/r.lock"}}
    _fileops.acquire_lock(lock_path)
    assert "Item" in seam["ddb"].get_item(TableName=LOCKS, Key=key)  # lock in DDB
    assert not lock_path.exists()                                    # NOT a local file
    _fileops.release_lock(lock_path)
    assert "Item" not in seam["ddb"].get_item(TableName=LOCKS, Key=key)


# --- append path now routes through the backend (s3 step 3) -----------------
def test_locked_append_jsonl_reaches_s3(seam):
    p = seam["world"] / "log.jsonl"
    _fileops.locked_write_jsonl(p, [{"a": 1}])
    _fileops.locked_append_jsonl(p, {"a": 2})     # routed -> append_jsonl_record -> S3
    assert _s3_lines(seam, "ayoai-mind/world/log.jsonl") == [{"a": 1}, {"a": 2}]


def test_allocator_append_reaches_s3(seam):
    p = seam["world"] / "alloc.jsonl"
    _fileops.locked_write_jsonl(p, [{"n": 1}])
    _fileops.locked_append_jsonl_with_allocator(p, lambda items: {"n": len(items) + 1})
    assert _s3_lines(seam, "ayoai-mind/world/alloc.jsonl") == [{"n": 1}, {"n": 2}]


# --- force-fresh in-lock read (fix #2 / Landmine 4f) ------------------------
def test_modify_reads_force_fresh_from_s3(seam):
    # Simulate a cross-machine race: our local cache is stale while S3 holds a
    # newer version (another machine wrote it). The in-lock refresh() must pull
    # the S3 version so the modifier operates on the latest state, not the
    # stale local cache — otherwise the out-of-band record is lost.
    p = seam["world"] / "store.jsonl"
    key = "ayoai-mind/world/store.jsonl"
    _fileops.locked_write_jsonl(p, [{"id": 1}])           # local + S3 in sync: [{1}]
    # Another machine appends {id:2} directly to S3; our local cache still shows [{1}].
    seam["s3"].put_object(Bucket=BUCKET, Key=key,
                          Body=b'{"id": 1}\n{"id": 2}\n')
    seen = {}
    def _mod(items):
        seen["items"] = list(items)                       # what the modifier observed
        return items + [{"id": 3}]
    _fileops.locked_modify_jsonl(p, _mod)
    # refresh() pulled the S3 version, so the modifier saw [{1},{2}] (not stale [{1}]),
    # and the final write is [{1},{2},{3}] — the out-of-band record survived.
    assert seen["items"] == [{"id": 1}, {"id": 2}]
    assert _s3_lines(seam, key) == [{"id": 1}, {"id": 2}, {"id": 3}]


def test_modify_yaml_write_reaches_s3(seam):
    import yaml
    p = seam["world"] / "state.yaml"
    _fileops.locked_modify_yaml(p, lambda d: {**(d or {}), "k": "v"})
    body = seam["s3"].get_object(Bucket=BUCKET, Key="ayoai-mind/world/state.yaml")["Body"].read()
    assert yaml.safe_load(body.decode("utf-8")) == {"k": "v"}
