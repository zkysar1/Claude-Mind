"""moto-mocked unit tests for OwnCloudBackend (Lodestar own-cloud tier, s3).

100% local: moto intercepts every boto3 call with fake creds — NO real AWS, NO
network, NO cloud writes. Validates each concurrency mechanism from
mind_api/docs/lodestar-own-cloud-architecture.md:

  - DDB lock acquire / conditional release / stale-break via ttl<:now (fix #1)
  - read records ETag; force_fresh bypasses the local cache (fix #2)
  - If-Match fence: stale-token PUT -> ConflictError (fix #3 / A2)
  - dual-runner: conditional IDLE->RUNNING, second runner -> RunnerHeld (fix #4)
  - crashed-runner reclaim via stale heartbeat; fresh runner NOT reclaimed (B2)

File basename starts with ``test_`` so domain-leak-check.sh skips it (the boto3 /
S3 / DynamoDB tokens here are test infrastructure, not a domain leak).
"""
import sys
import time
from pathlib import Path

import pytest

# core/scripts on sys.path so `import owncloud_backend` resolves (mirrors the
# import convention used by the sibling test modules in this directory).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

moto = pytest.importorskip("moto")
boto3 = pytest.importorskip("boto3")
from botocore.exceptions import ClientError  # noqa: E402
from moto import mock_aws  # noqa: E402

BUCKET = "zds-data"
LOCKS = "zds-locks"
SESSIONS = "zds-sessions"
REGION = "us-east-2"
ENV_ID = "ayoai-mind"


@pytest.fixture(autouse=True)
def _default_machine_id(monkeypatch):
    """G5: from_env() fail-closes when MACHINE_ID is unset/'unknown' (two
    machines both 'unknown' can false-release each other's DDB locks). Default a
    valid id for every test here so the from_env-constructing tests pass the
    guard; the guard's own test unsets it explicitly. __init__-based construction
    (the _backend helper) passes machine_id directly and is unaffected."""
    monkeypatch.setenv("MACHINE_ID", "test-machine-ci")


@pytest.fixture
def aws_env(monkeypatch):
    # Fake creds so  is satisfied; moto never contacts AWS.
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


def _set_heartbeat(cloud, agent, epoch):
    cloud["ddb"].update_item(
        TableName=SESSIONS,
        Key={"session_key": {"S": f"{ENV_ID}/{agent}"}},
        UpdateExpression="SET heartbeat_at = :hb",
        ExpressionAttributeValues={":hb": {"N": str(int(epoch))}})


# --- basic S3 read/write ----------------------------------------------------
def test_write_then_read_round_trip(cloud):
    b = _backend(cloud)
    p = cloud["root"] / "world" / "x.txt"
    res = b.write_text(p, "hello")
    assert res.fallback_used is False and res.version
    assert b.read_text(p) == "hello"


def test_stat_and_exists(cloud):
    b = _backend(cloud)
    p = cloud["root"] / "world" / "f.txt"
    assert b.exists(p) is False
    assert b.stat(p) is None
    b.write_text(p, "hello")
    assert b.exists(p) is True
    st = b.stat(p)
    assert st.size == 5 and st.version


def test_list_dir_env_scoped(cloud):
    b = _backend(cloud)
    b.write_text(cloud["root"] / "world" / "a.txt", "1")
    b.write_text(cloud["root"] / "world" / "b.txt", "2")
    b.write_text(cloud["root"] / "world" / "sub" / "c.txt", "3")
    assert b.list_dir(cloud["root"] / "world") == ["a.txt", "b.txt", "sub"]


# --- caching + force_fresh (fix #2) -----------------------------------------
def test_read_caches_and_force_fresh_bypasses(cloud):
    b = _backend(cloud)
    p = cloud["root"] / "world" / "c.txt"
    key = b._s3_key(p)
    b.write_text(p, "v1")
    assert b.read_text(p) == "v1"
    # Mutate the object out-of-band (another machine). Cached read still sees v1.
    cloud["s3"].put_object(Bucket=BUCKET, Key=key, Body=b"v2")
    assert b.read_text(p) == "v1"          # within cache_ttl -> cached
    assert b.read_text(p, force_fresh=True) == "v2"  # bypasses cache (fix #2)


# --- If-Match fence (fix #3 / A2) -------------------------------------------
def test_fence_stale_etag_raises_conflict(cloud):
    b = _backend(cloud)
    p = cloud["root"] / "world" / "fence.txt"
    key = b._s3_key(p)
    b.write_text(p, "v1")                  # records etag E1
    cloud["s3"].put_object(Bucket=BUCKET, Key=key, Body=b"v2")  # remote -> E2
    from owncloud_backend import ConflictError
    with pytest.raises(ConflictError):
        b.write_text(p, "v3")              # If-Match E1 != E2 -> 412 -> ConflictError


def test_fence_allows_write_with_current_etag(cloud):
    b = _backend(cloud)
    p = cloud["root"] / "world" / "fence2.txt"
    b.write_text(p, "v1")                  # E1
    res = b.write_text(p, "v2")            # If-Match E1 == remote -> ok
    assert res.version and b.read_text(p, force_fresh=True) == "v2"


# --- G1: defer local write + conflict_error contract (machine-2 gate) -------
def test_put_defers_local_write_until_put_succeeds(cloud):
    """A 412 must NOT leave the local cache ahead of S3. _put writes the local
    cache only AFTER put_object succeeds, so a fenced PUT leaves the local file
    byte-identical to the last good version — no local-ahead divergence for the
    mirror sweep to later push, and nothing lost on the next restart."""
    b = _backend(cloud)
    p = cloud["root"] / "world" / "defer.txt"
    key = b._s3_key(p)
    b.write_text(p, "v1")                       # local + S3 = v1, fence = E1
    assert p.read_bytes() == b"v1"
    cloud["s3"].put_object(Bucket=BUCKET, Key=key, Body=b"v2")  # peer moves remote -> E2
    from owncloud_backend import ConflictError
    with pytest.raises(ConflictError):
        b.write_text(p, "v3")                   # If-Match E1 != E2 -> 412
    assert p.read_bytes() == b"v1"              # losing bytes NOT written locally


def test_conflict_error_attribute_is_conflict_error(cloud):
    from owncloud_backend import ConflictError
    assert _backend(cloud).conflict_error is ConflictError


# --- mirror_put (B15: local->S3 mirror sweep primitive) ---------------------
def test_mirror_put_new_object_unconditional(cloud):
    b = _backend(cloud)
    p = cloud["root"] / "world" / "knowledge" / "node.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"raw-written body")     # a RAW write that bypassed the backend
    assert b.stat(p) is None               # absent on S3 (the B15 gap)
    b.mirror_put(p, p.read_bytes(), expected_version=None)
    assert b.read_text(p, force_fresh=True) == "raw-written body"  # now on S3


def test_mirror_put_fenced_succeeds_with_current_etag(cloud):
    b = _backend(cloud)
    p = cloud["root"] / "world" / "node.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    cloud["s3"].put_object(Bucket=BUCKET, Key=b._s3_key(p), Body=b"old")
    etag = b.stat(p).version               # observe current ETag (what a sweep does)
    p.write_bytes(b"new local body")       # local diverged (raw write)
    b.mirror_put(p, p.read_bytes(), expected_version=etag)
    assert b.read_text(p, force_fresh=True) == "new local body"


def test_mirror_put_stale_etag_raises_conflict(cloud):
    b = _backend(cloud)
    p = cloud["root"] / "world" / "node.md"
    key = b._s3_key(p)
    cloud["s3"].put_object(Bucket=BUCKET, Key=key, Body=b"v1")
    stale = b.stat(p).version              # E1
    cloud["s3"].put_object(Bucket=BUCKET, Key=key, Body=b"v2")  # remote -> E2
    from owncloud_backend import ConflictError
    with pytest.raises(ConflictError):
        b.mirror_put(p, b"v3", expected_version=stale)  # If-Match E1 != E2


def test_mirror_put_does_not_clobber_local_with_older_remote(cloud):
    """The anti-clobber property: a locally-NEWER file must be PUSHED to S3, never
    overwritten by the older remote copy. read_bytes(force_fresh) would download
    and destroy the local change; mirror_put must not."""
    b = _backend(cloud)
    p = cloud["root"] / "world" / "self.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    cloud["s3"].put_object(Bucket=BUCKET, Key=b._s3_key(p), Body=b"OLD remote")
    p.write_bytes(b"NEW local (unsynced raw write)")
    etag = b.stat(p).version
    b.mirror_put(p, p.read_bytes(), expected_version=etag)
    # local preserved AND pushed; S3 now carries the local-authoritative bytes.
    assert p.read_bytes() == b"NEW local (unsynced raw write)"
    assert b.read_text(p, force_fresh=True) == "NEW local (unsynced raw write)"


# --- JSONL read-modify-write ------------------------------------------------
def test_modify_jsonl_rmw(cloud):
    b = _backend(cloud)
    p = cloud["root"] / "world" / "store.jsonl"
    b.write_jsonl(p, [{"id": 1}])
    b.modify_jsonl(p, lambda items: items + [{"id": 2}])
    assert b.read_jsonl(p) == [{"id": 1}, {"id": 2}]


def test_append_jsonl_record(cloud):
    b = _backend(cloud)
    p = cloud["root"] / "world" / "log.jsonl"
    b.write_jsonl(p, [{"a": 1}])
    b.append_jsonl_record(p, {"a": 2})
    assert b.read_jsonl(p) == [{"a": 1}, {"a": 2}]


# --- DDB locking (fix #1) ---------------------------------------------------
def test_acquire_lock_when_absent(cloud):
    b = _backend(cloud)
    lp = cloud["root"] / "world" / "r.lock"
    b.acquire_lock(lp)
    item = cloud["ddb"].get_item(
        TableName=LOCKS, Key={"lock_key": {"S": b._lock_key(lp)}})["Item"]
    assert item["holder"]["S"].startswith("m1:")


def test_held_fresh_lock_blocks_other_holder(cloud):
    a = _backend(cloud, machine_id="A")
    other = _backend(cloud, machine_id="B")
    lp = cloud["root"] / "world" / "r.lock"
    a.acquire_lock(lp)
    with pytest.raises(TimeoutError):
        other.acquire_lock(lp, timeout=1)


def test_stale_lock_is_breakable(cloud):
    a = _backend(cloud, machine_id="A")
    other = _backend(cloud, machine_id="B")
    lp = cloud["root"] / "world" / "r.lock"
    a.acquire_lock(lp)
    # Expire the lock's ttl into the past (no sleeping). Liveness is the
    # app-level ttl<:now condition, NOT  TTL deletion (fix #1).
    cloud["ddb"].update_item(
        TableName=LOCKS, Key={"lock_key": {"S": b_lock_key(a, lp)}},
        UpdateExpression="SET #t = :old",
        ExpressionAttributeNames={"#t": "ttl"},
        ExpressionAttributeValues={":old": {"N": str(int(time.time()) - 100)}})
    other.acquire_lock(lp, timeout=1)      # ttl < now -> acquirable
    item = cloud["ddb"].get_item(
        TableName=LOCKS, Key={"lock_key": {"S": b_lock_key(a, lp)}})["Item"]
    assert item["holder"]["S"].startswith("B:")


def test_release_only_by_holder(cloud):
    a = _backend(cloud, machine_id="A")
    other = _backend(cloud, machine_id="B")
    lp = cloud["root"] / "world" / "r.lock"
    a.acquire_lock(lp)
    other.release_lock(lp)                 # not holder -> conditional no-op
    assert "Item" in cloud["ddb"].get_item(
        TableName=LOCKS, Key={"lock_key": {"S": b_lock_key(a, lp)}})
    a.release_lock(lp)                      # holder -> deletes
    assert "Item" not in cloud["ddb"].get_item(
        TableName=LOCKS, Key={"lock_key": {"S": b_lock_key(a, lp)}})


def b_lock_key(backend, lp):
    return backend._lock_key(lp)


# --- dual-runner prevention (fix #4) ----------------------------------------
def test_dual_runner_second_acquire_raises(cloud):
    from owncloud_backend import RunnerHeld
    a = _backend(cloud, machine_id="A")
    other = _backend(cloud, machine_id="B")
    assert a.acquire_runner("alpha", "tokA") is True
    assert a.get_runner_state("alpha")["agent_state"] == "RUNNING"
    with pytest.raises(RunnerHeld):
        other.acquire_runner("alpha", "tokB")


# --- crashed-runner reclaim (fix B2) ----------------------------------------
def test_reclaim_stale_runner(cloud):
    a = _backend(cloud, machine_id="A")
    other = _backend(cloud, machine_id="B")
    assert a.acquire_runner("alpha", "tokA") is True
    _set_heartbeat(cloud, "alpha", time.time() - 10_000)   # crash: heartbeat stale
    assert other.reclaim_if_stale("alpha") is True
    assert a.get_runner_state("alpha")["agent_state"] == "IDLE"
    assert other.acquire_runner("alpha", "tokB") is True   # reclaimed -> acquirable


def test_fresh_runner_not_reclaimed(cloud):
    from owncloud_backend import RunnerHeld
    a = _backend(cloud, machine_id="A")
    other = _backend(cloud, machine_id="B")
    a.acquire_runner("alpha", "tokA")
    assert other.reclaim_if_stale("alpha") is False        # heartbeat fresh
    with pytest.raises(RunnerHeld):
        other.acquire_runner("alpha", "tokB")


def test_heartbeat_refresh_blocks_reclaim(cloud):
    a = _backend(cloud, machine_id="A")
    other = _backend(cloud, machine_id="B")
    a.acquire_runner("alpha", "tokA")
    _set_heartbeat(cloud, "alpha", time.time() - 10_000)
    a.heartbeat("alpha", "tokA")                            # owner refreshes
    assert other.reclaim_if_stale("alpha") is False


def test_heartbeat_wrong_token_rejected(cloud):
    a = _backend(cloud, machine_id="A")
    a.acquire_runner("alpha", "tokA")
    with pytest.raises(ClientError):
        a.heartbeat("alpha", "WRONG-TOKEN")                 # not the runner_token


# --- multi-root key mapping (from_env wiring) -------------------------------
def _multiroot(cloud, world, meta, agents):
    from owncloud_backend import OwnCloudBackend
    return OwnCloudBackend(
        env_id=ENV_ID, bucket=BUCKET, lock_table=LOCKS,
        sessions_table=SESSIONS, region=REGION, s3=cloud["s3"], ddb=cloud["ddb"],
        root_map=[(world, "world"), (meta, "meta"), (agents, "agents")])


def test_multi_root_maps_each_root_to_its_prefix(cloud, tmp_path):
    world, meta, agents = tmp_path / "w", tmp_path / "m", tmp_path / "a"
    b = _multiroot(cloud, world, meta, agents)
    assert b._s3_key(world / "reasoning-bank.jsonl") == "ayoai-mind/world/reasoning-bank.jsonl"
    assert b._s3_key(meta / "spark-questions.jsonl") == "ayoai-mind/meta/spark-questions.jsonl"
    assert b._s3_key(agents / "alpha" / "aspirations.jsonl") == "ayoai-mind/agents/alpha/aspirations.jsonl"


def test_rel_raises_on_unmapped_path(cloud, tmp_path):
    b = _multiroot(cloud, tmp_path / "w", tmp_path / "m", tmp_path / "a")
    with pytest.raises(ValueError):
        b._s3_key(tmp_path / "nowhere" / "x.jsonl")         # under no root -> raise, not p.name


def test_same_filename_different_roots_get_distinct_lock_keys(cloud, tmp_path):
    # Landmine 3 regression: world/aspirations.lock and agents/alpha/aspirations.lock
    # must NOT collapse to the same DDB key (the old p.name fallback did exactly that).
    world, meta, agents = tmp_path / "w", tmp_path / "m", tmp_path / "a"
    b = _multiroot(cloud, world, meta, agents)
    k1 = b._lock_key(world / "aspirations.lock")
    k2 = b._lock_key(agents / "alpha" / "aspirations.lock")
    assert k1 == "ayoai-mind/world/aspirations.lock"
    assert k2 == "ayoai-mind/agents/alpha/aspirations.lock"
    assert k1 != k2


def test_from_env_builds_three_root_map(cloud, tmp_path, monkeypatch):
    from owncloud_backend import OwnCloudBackend
    world, meta, agents = tmp_path / "w", tmp_path / "m", tmp_path / "agents"
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(k, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    # This test exercises the root-map wiring, not credential resolution. Opt
    # into the default  chain so the fail-closed MIND_AWS_* guard (which
    # would otherwise raise before _resolve_root_map runs) does not fire.
    monkeypatch.setenv("MIND_AWS_ALLOW_DEFAULT_CHAIN", "1")
    monkeypatch.setenv("STORAGE_S3_BUCKET", BUCKET)
    monkeypatch.setenv("STORAGE_DDB_LOCK_TABLE", LOCKS)
    monkeypatch.setenv("STORAGE_DDB_SESSIONS_TABLE", SESSIONS)
    monkeypatch.setenv("ENVIRONMENT_ID", ENV_ID)
    monkeypatch.setenv("MIND_WORLD", str(world))
    monkeypatch.setenv("MIND_META", str(meta))
    monkeypatch.setenv("AGENTS_ROOT", str(agents))
    b = OwnCloudBackend.from_env()                           # builds its own  clients (moto-mocked)
    prefixes = {prefix: root for root, prefix in b._roots}
    assert prefixes["world"] == world and prefixes["meta"] == meta and prefixes["agents"] == agents
    assert b._s3_key(world / "x.jsonl") == "ayoai-mind/world/x.jsonl"


def test_from_env_routes_scoped_creds_to_session(monkeypatch, tmp_path):
    # When MIND_AWS_* are set, from_env must build the  clients from a
    # Session carrying THOSE creds (the scoped Zak_first_test user) — never the
    # process-wide root AWS_* keys. Monkeypatch Session to capture the creds;
    # no network, no moto needed.
    import owncloud_backend as ocb
    captured = {}

    class _FakeClient:
        pass

    class _FakeSession:
        def __init__(self, **kw):
            captured.update(kw)

        def client(self, *a, **k):
            return _FakeClient()

    monkeypatch.setattr(ocb.boto3, "Session", _FakeSession)
    monkeypatch.setenv("STORAGE_S3_BUCKET", BUCKET)
    monkeypatch.setenv("STORAGE_DDB_LOCK_TABLE", LOCKS)
    monkeypatch.setenv("STORAGE_DDB_SESSIONS_TABLE", SESSIONS)
    monkeypatch.setenv("MIND_WORLD", str(tmp_path / "w"))
    monkeypatch.setenv("MIND_AWS_ACCESS_KEY_ID", "SCOPED_AKID")
    monkeypatch.setenv("MIND_AWS_SECRET_ACCESS_KEY", "scoped_secret")
    b = ocb.OwnCloudBackend.from_env()
    assert captured.get("aws_access_key_id") == "SCOPED_AKID"
    assert captured.get("aws_secret_access_key") == "scoped_secret"
    assert isinstance(b.s3, _FakeClient) and isinstance(b.ddb, _FakeClient)


def test_from_env_requires_a_world_or_meta_root(monkeypatch):
    from owncloud_backend import OwnCloudBackend
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "testing")
    # Opt into the default chain so this test reaches the world/meta-root check
    # it is actually exercising (not the fail-closed cred gate, which would
    # otherwise raise first and pass this test for the wrong reason).
    monkeypatch.setenv("MIND_AWS_ALLOW_DEFAULT_CHAIN", "1")
    monkeypatch.setenv("STORAGE_S3_BUCKET", BUCKET)
    monkeypatch.setenv("STORAGE_DDB_LOCK_TABLE", LOCKS)
    monkeypatch.setenv("STORAGE_DDB_SESSIONS_TABLE", SESSIONS)
    monkeypatch.delenv("MIND_WORLD", raising=False)
    monkeypatch.delenv("WORLD_PATH", raising=False)
    monkeypatch.delenv("MIND_META", raising=False)
    monkeypatch.delenv("META_PATH", raising=False)
    with pytest.raises(RuntimeError, match="root"):
        OwnCloudBackend.from_env()


def test_from_env_fails_closed_when_scoped_creds_unset(monkeypatch, tmp_path):
    # Security: with STORAGE_BACKEND=own-cloud but MIND_AWS_* UNSET and no
    # explicit opt-in, from_env MUST refuse rather than silently fall back to the
    # default  chain (which on this deployment resolves the root AWS_* lambda
    # keys). Root-style AWS_* present, MIND_AWS_* absent -> RuntimeError.
    from owncloud_backend import OwnCloudBackend
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "root-key")           # the over-privileged keys
    monkeypatch.delenv("MIND_AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("MIND_AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("MIND_AWS_ALLOW_DEFAULT_CHAIN", raising=False)
    monkeypatch.setenv("STORAGE_S3_BUCKET", BUCKET)
    monkeypatch.setenv("STORAGE_DDB_LOCK_TABLE", LOCKS)
    monkeypatch.setenv("STORAGE_DDB_SESSIONS_TABLE", SESSIONS)
    monkeypatch.setenv("MIND_WORLD", str(tmp_path / "w"))
    with pytest.raises(RuntimeError, match="MIND_AWS"):
        OwnCloudBackend.from_env()


def test_from_env_allows_default_chain_when_opted_in(cloud, monkeypatch, tmp_path):
    # The escape hatch: MIND_AWS_ALLOW_DEFAULT_CHAIN=1 lets a deployment with no
    # static MIND_AWS_* (instance-role / ECS task-role) build successfully via
    # the default chain. The fixture's moto creds satisfy .
    from owncloud_backend import OwnCloudBackend
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(k, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.delenv("MIND_AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("MIND_AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.setenv("MIND_AWS_ALLOW_DEFAULT_CHAIN", "1")
    monkeypatch.setenv("STORAGE_S3_BUCKET", BUCKET)
    monkeypatch.setenv("STORAGE_DDB_LOCK_TABLE", LOCKS)
    monkeypatch.setenv("STORAGE_DDB_SESSIONS_TABLE", SESSIONS)
    monkeypatch.setenv("MIND_WORLD", str(tmp_path / "w"))
    b = OwnCloudBackend.from_env()
    assert b.name == "own-cloud"


def test_from_env_fails_closed_when_machine_id_unset(monkeypatch, tmp_path):
    # G5: a UNIQUE per-machine id is required so two machines cannot false-release
    # each other's DDB locks (holder = machine_id:pid:tid; release_lock matches on
    # holder). Unset OR the legacy 'unknown' default -> from_env MUST refuse.
    from owncloud_backend import OwnCloudBackend
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "testing")
    monkeypatch.setenv("MIND_AWS_ALLOW_DEFAULT_CHAIN", "1")
    monkeypatch.setenv("STORAGE_S3_BUCKET", BUCKET)
    monkeypatch.setenv("STORAGE_DDB_LOCK_TABLE", LOCKS)
    monkeypatch.setenv("STORAGE_DDB_SESSIONS_TABLE", SESSIONS)
    monkeypatch.setenv("MIND_WORLD", str(tmp_path / "w"))
    monkeypatch.delenv("MACHINE_ID", raising=False)        # the condition under test
    with pytest.raises(RuntimeError, match="MACHINE_ID"):
        OwnCloudBackend.from_env()
    # the old silent default value is refused too (not just absence)
    monkeypatch.setenv("MACHINE_ID", "unknown")
    with pytest.raises(RuntimeError, match="MACHINE_ID"):
        OwnCloudBackend.from_env()
    # a real id satisfies the guard
    monkeypatch.setenv("MACHINE_ID", "machine-1")
    assert OwnCloudBackend.from_env().machine_id == "machine-1"
