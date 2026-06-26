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
import hashlib
import json
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


@pytest.fixture(autouse=True)
def _isolate_sync_manifest(monkeypatch, tmp_path):
    """4: _refresh's no-clobber guard lazily reads owncloud_sync's
    persistent sync-manifest (located via RUNTIME_DIR). Point RUNTIME_DIR at a
    per-test tmp dir so these tests are hermetic — they never read the real
    mind_api/state/owncloud-sync-manifest.json (no dependency on machine state).
    Tests that exercise the baseline write their manifest into this dir via
    _write_sync_manifest()."""
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "_owncloud_rt"))


def _write_sync_manifest(tmp_path, entries):
    """Write owncloud_sync's persistent sync-manifest into the RUNTIME_DIR the
    _isolate_sync_manifest fixture points at. `entries` maps rel_key ->
    {"mtime": int, "md5": hexdigest}."""
    rt = tmp_path / "_owncloud_rt"
    rt.mkdir(parents=True, exist_ok=True)
    (rt / "owncloud-sync-manifest.json").write_text(
        json.dumps(entries), encoding="utf-8")


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


# --- clean release: RUNNING->IDLE iff token matches (7) -------------
def test_release_runner_clean(cloud):
    a = _backend(cloud, machine_id="A")
    assert a.acquire_runner("alpha", "tokA") is True
    assert a.release_runner("alpha", "tokA") is True        # we held it -> released
    assert a.get_runner_state("alpha")["agent_state"] == "IDLE"


def test_release_runner_idempotent_second_call(cloud):
    a = _backend(cloud, machine_id="A")
    a.acquire_runner("alpha", "tokA")
    assert a.release_runner("alpha", "tokA") is True
    assert a.release_runner("alpha", "tokA") is False       # already IDLE -> no-op
    assert a.get_runner_state("alpha")["agent_state"] == "IDLE"


def test_release_runner_wrong_token_does_not_release(cloud):
    # A peer (or a stale token) must NOT release a claim it does not hold: the
    # row stays RUNNING and the real owner's token is untouched.
    a = _backend(cloud, machine_id="A")
    other = _backend(cloud, machine_id="B")
    a.acquire_runner("alpha", "tokA")
    assert other.release_runner("alpha", "tokB") is False    # wrong token -> no-op
    assert a.get_runner_state("alpha")["agent_state"] == "RUNNING"
    assert a.get_runner_state("alpha")["runner_token"] == "tokA"


def test_release_runner_after_peer_reclaim_is_idempotent(cloud):
    # Crash path: A acquires, heartbeat goes stale, peer B reclaims -> IDLE. When
    # A finally reaches /stop and calls release_runner, the row is already IDLE;
    # release must report not-transitioned (False) and NEVER raise.
    a = _backend(cloud, machine_id="A")
    other = _backend(cloud, machine_id="B")
    a.acquire_runner("alpha", "tokA")
    _set_heartbeat(cloud, "alpha", time.time() - 10_000)
    assert other.reclaim_if_stale("alpha") is True
    assert a.release_runner("alpha", "tokA") is False        # already reclaimed
    assert a.get_runner_state("alpha")["agent_state"] == "IDLE"


def test_release_runner_missing_item_noop(cloud):
    # Releasing an agent that was never acquired (no row) is a no-op, not a raise.
    a = _backend(cloud, machine_id="A")
    assert a.release_runner("never-started", "tok") is False


# --- list_runner_claims: env-scoped enumeration for ownership (7) ---
def test_list_runner_claims_empty(cloud):
    a = _backend(cloud, machine_id="A")
    assert a.list_runner_claims() == []


def test_list_runner_claims_running_and_idle(cloud):
    a = _backend(cloud, machine_id="A")
    a.acquire_runner("alpha", "tokA")                        # RUNNING on A
    a.acquire_runner("bravo", "tokB")
    a.release_runner("bravo", "tokB")                        # IDLE
    claims = {c.agent: c for c in a.list_runner_claims()}
    assert set(claims) == {"alpha", "bravo"}
    assert claims["alpha"].agent_state == "RUNNING"
    assert claims["alpha"].machine_id == "A"
    assert claims["alpha"].heartbeat_at > 0                  # int epoch, fresh
    assert claims["bravo"].agent_state == "IDLE"


def test_list_runner_claims_is_env_scoped(cloud):
    # A claim under a DIFFERENT env-id must never leak into this env's owned-set.
    a = _backend(cloud, machine_id="A")
    a.acquire_runner("alpha", "tokA")
    cloud["ddb"].put_item(
        TableName=SESSIONS,
        Item={"session_key": {"S": "other-env/gamma"},
              "agent_state": {"S": "RUNNING"},
              "machine_id": {"S": "A"},
              "heartbeat_at": {"N": str(int(time.time()))}})
    agents = {c.agent for c in a.list_runner_claims()}
    assert agents == {"alpha"}                               # other-env/gamma excluded


def test_list_runner_claims_idle_row_defaults(cloud):
    # A bare create-only IDLE row (no machine_id, no heartbeat) projects to
    # machine_id=None and heartbeat_at=0 (treated as infinitely stale by §3).
    a = _backend(cloud, machine_id="A")
    cloud["ddb"].put_item(
        TableName=SESSIONS,
        Item={"session_key": {"S": f"{ENV_ID}/delta"},
              "agent_state": {"S": "IDLE"}})
    (claim,) = a.list_runner_claims()
    assert claim.agent == "delta"
    assert claim.machine_id is None
    assert claim.heartbeat_at == 0
    assert claim.agent_state == "IDLE"


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


# --- _refresh no-clobber guard (3 root cause / 4 fix) ------
# The in-process self._etags cache is empty after a daemon restart, so the
# first force_fresh refresh must NOT download stale S3 over a local file holding
# unpushed writes. The guard gates the overwrite on the persistent sync-manifest
# baseline, symmetric to owncloud_sync._pull_one. These four tests pin all four
# _overwrite_decision branches: no_clobber, download(peer), identical, download(no-baseline).
def test_refresh_no_clobber_unpushed_local_after_restart(cloud, tmp_path):
    """3 regression: a restart empties _etags; the first force_fresh
    refresh of a file whose local copy has unpushed writes (local != baseline)
    must KEEP local, never clobber it with stale S3."""
    b = _backend(cloud)
    p = cloud["root"] / "world" / "reasoning-bank.jsonl"
    key = b._s3_key(p)
    stale = b'{"id":"rb-1","valid_from":0}\n'            # last-synced version on S3
    fresh_local = b'{"id":"rb-1","valid_from":2020}\n'   # unpushed local backfill
    cloud["s3"].put_object(Bucket=BUCKET, Key=key, Body=stale)   # S3 = stale
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(fresh_local)                            # non-backend raw local write
    # baseline == last reconciled (stale) content; local diverged -> unpushed.
    _write_sync_manifest(tmp_path, {b._rel(p): {
        "mtime": 1, "md5": hashlib.md5(stale).hexdigest()}})
    b2 = _backend(cloud)                                  # restart: empty _etags
    assert b2._etags == {}
    got = b2.read_text(p, force_fresh=True)               # the _fileops in-lock RMW path
    assert got == fresh_local.decode()                   # unpushed local survives
    assert p.read_bytes() == fresh_local                 # on-disk file untouched


def test_refresh_pulls_when_local_at_baseline_and_s3_moved(cloud, tmp_path):
    """local == baseline and S3 != baseline -> a peer/other machine wrote; this
    is NOT an unpushed-local case, so the guard MUST still pull (no over-block)."""
    b = _backend(cloud)
    p = cloud["root"] / "world" / "peer.jsonl"
    key = b._s3_key(p)
    baseline = b'{"v":1}\n'
    peer_new = b'{"v":2}\n'
    cloud["s3"].put_object(Bucket=BUCKET, Key=key, Body=peer_new)  # peer wrote v2
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(baseline)                              # local still at baseline v1
    _write_sync_manifest(tmp_path, {b._rel(p): {
        "mtime": 1, "md5": hashlib.md5(baseline).hexdigest()}})
    b2 = _backend(cloud)
    got = b2.read_text(p, force_fresh=True)
    assert got == peer_new.decode()                     # pulled the peer's newer copy
    assert p.read_bytes() == peer_new


def test_refresh_identical_local_post_restart_adopts_fence(cloud, tmp_path):
    """Post-restart with local already byte-identical to S3: short-circuit (no
    re-download) and adopt the S3 ETag as the If-Match fence token."""
    b = _backend(cloud)
    p = cloud["root"] / "world" / "same.jsonl"
    key = b._s3_key(p)
    body = b'{"v":1}\n'
    head = cloud["s3"].put_object(Bucket=BUCKET, Key=key, Body=body)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)                                  # local == S3
    b2 = _backend(cloud)                                 # empty _etags
    assert b2._etags == {}
    got = b2.read_text(p, force_fresh=True)
    assert got == body.decode()
    assert b2._etags.get(key) == head["ETag"]           # fence token adopted


def test_refresh_no_baseline_pulls_s3_authoritative(cloud, tmp_path):
    """No manifest baseline + local differs from S3: cannot prove local
    authority -> S3 is authoritative -> pull (matches _pull_one's no-baseline
    branch; preserves the pre-fix force_fresh semantics)."""
    b = _backend(cloud)
    p = cloud["root"] / "world" / "nobaseline.jsonl"
    key = b._s3_key(p)
    s3_body = b'{"v":2}\n'
    local_body = b'{"v":1}\n'
    cloud["s3"].put_object(Bucket=BUCKET, Key=key, Body=s3_body)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(local_body)
    # No manifest written -> baseline None.
    b2 = _backend(cloud)
    got = b2.read_text(p, force_fresh=True)
    assert got == s3_body.decode()                      # pulled (S3-authoritative)


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


# --- multi-tenant customer dimension (T-b, 1) ----------------------
# Brief mind_api/docs/lodestar-tenant-isolation-rearch.md sections 3/6/8. The
# back-compat invariant (default customer => byte-identical legacy keys) is the
# regression that protects the live ayoai-mind/* data; the customer-set cases
# prove the new isolation; the concurrency case proves the contextvars seam has
# no key bleed between simultaneous distinct-customer requests (risk #3).
import owncloud_backend as _ocb  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_customer_ctx():
    """contextvars persist across tests in one process — reset to the
    single-tenant baseline after each test so a customer-set test cannot bleed
    into the next."""
    yield
    _ocb.set_customer("default")


def test_default_customer_keys_are_byte_identical_legacy(cloud):
    """REGRESSION (back-compat invariant): with no customer set (the 'default'
    baseline this deployment runs today), every key builder emits the legacy
    env-id-only key — NO customer segment — so the live ayoai-mind/* data is
    untouched. Pins the exact bytes BEFORE trusting the customer dimension."""
    b = _backend(cloud)
    assert _ocb.current_customer() == "default"
    assert b._s3_key(cloud["root"] / "world" / "reasoning-bank.jsonl") == \
        f"{ENV_ID}/world/reasoning-bank.jsonl"
    assert b._lock_key(cloud["root"] / "world" / "aspirations.lock") == \
        f"{ENV_ID}/world/aspirations.lock"
    assert b._session_key("alpha") == f"{ENV_ID}/alpha"


def test_customer_set_prepends_customer_segment(cloud):
    """GIVEN X-Mind-Tenant: pearl (set_customer), WHEN keys are built, THEN the
    customer segment leads: <customer>/<env-id>/<path> (brief section 8)."""
    b = _backend(cloud)
    tok = _ocb.set_customer("pearl")
    try:
        assert b._s3_key(cloud["root"] / "world" / "x.jsonl") == \
            f"pearl/{ENV_ID}/world/x.jsonl"
        assert b._lock_key(cloud["root"] / "world" / "x.lock") == \
            f"pearl/{ENV_ID}/world/x.lock"
        assert b._session_key("alpha") == f"pearl/{ENV_ID}/alpha"
    finally:
        _ocb.reset_customer(tok)
    # after reset, back to the legacy baseline (byte-identical)
    assert b._s3_key(cloud["root"] / "world" / "x.jsonl") == \
        f"{ENV_ID}/world/x.jsonl"


def test_set_customer_normalizes_and_rejects_slash(cloud):
    """Surrounding slashes are stripped (header hygiene); an internal '/' is
    rejected — it would corrupt prefix segmentation / escape the IAM-conditioned
    customer prefix; blank/None => the default baseline."""
    b = _backend(cloud)
    tok = _ocb.set_customer("/pearl/")
    try:
        assert b._s3_key(cloud["root"] / "a.txt") == f"pearl/{ENV_ID}/a.txt"
    finally:
        _ocb.reset_customer(tok)
    tok = _ocb.set_customer("   ")
    try:
        assert _ocb.current_customer() == "default"  # blank => baseline
    finally:
        _ocb.reset_customer(tok)
    with pytest.raises(ValueError, match="must not contain"):
        _ocb.set_customer("acme/evil")


def test_list_dir_scoped_to_active_customer(cloud):
    """list_dir's IAM-prefix assertion tracks the active customer: under pearl it
    scopes to pearl/<env>/, and a pearl write is isolated from the default
    tenant's namespace (no cross-customer read)."""
    b = _backend(cloud)
    b.write_text(cloud["root"] / "world" / "legacy.txt", "L")  # default tenant
    tok = _ocb.set_customer("pearl")
    try:
        b.write_text(cloud["root"] / "world" / "pearl-only.txt", "P")
        assert b.list_dir(cloud["root"] / "world") == ["pearl-only.txt"]
    finally:
        _ocb.reset_customer(tok)
    # default context sees only the legacy object — no bleed either direction
    assert b.list_dir(cloud["root"] / "world") == ["legacy.txt"]


def test_no_key_bleed_across_concurrent_contexts(cloud):
    """T-c concurrency (risk #3): the process-singleton backend serves two
    simultaneous distinct-customer 'requests' with ZERO key bleed because the
    customer lives in a contextvars.ContextVar (per-thread context), not
    singleton state. The barrier forces both customers to be set before either
    builds its key, maximizing the interleaving a singleton-attr design would
    fail under."""
    import threading
    b = _backend(cloud)
    results: dict = {}
    barrier = threading.Barrier(2)

    def worker(customer, name):
        _ocb.set_customer(customer)
        barrier.wait()  # both set simultaneously before either builds
        results[name] = b._s3_key(cloud["root"] / "world" / "k.jsonl")

    t1 = threading.Thread(target=worker, args=("pearl", "t1"))
    t2 = threading.Thread(target=worker, args=("vinheim", "t2"))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert results["t1"] == f"pearl/{ENV_ID}/world/k.jsonl"
    assert results["t2"] == f"vinheim/{ENV_ID}/world/k.jsonl"


# --- machine-local exclusion at the per-op backend (4 / rb-2396) ----
# The exclusion policy (_EXCLUDE_DIRS dir-prune + _is_machine_local basenames)
# lived only in owncloud_sync's periodic walk; the per-op backend was blind, so
# jsonl_hygiene truncating world/presence/<agent>.jsonl via get_backend()._put
# leaked it to S3. These pin the chokepoint guard added to _put / _refresh.
def test_put_machine_local_path_writes_local_not_s3(cloud):
    """world/presence/<agent>.jsonl is under _EXCLUDE_DIRS: _put writes the local
    file but does NOT push to S3 (mirrors the sync-walk prune)."""
    b = _backend(cloud)
    p = cloud["root"] / "world" / "presence" / "bravo.jsonl"
    res = b.write_text(p, "tick\n")
    assert p.read_text() == "tick\n"            # local written
    assert res.fallback_used is False and res.version
    key = b._s3_key(p)
    with pytest.raises(ClientError):            # NO S3 object created
        cloud["s3"].head_object(Bucket=BUCKET, Key=key)


def test_put_normal_store_still_pushes_to_s3(cloud):
    """A NORMAL governed store (world/reasoning-bank.jsonl) is NOT machine-local
    and MUST still push to S3 -- guards the false-positive failure mode where an
    over-broad exclusion silently stops a real store syncing to the commons."""
    b = _backend(cloud)
    p = cloud["root"] / "world" / "reasoning-bank.jsonl"
    b.write_text(p, "real\n")
    key = b._s3_key(p)
    head = cloud["s3"].head_object(Bucket=BUCKET, Key=key)  # raises if absent
    assert head["ContentLength"] == len("real\n")


def test_refresh_machine_local_skips_s3_head(cloud, monkeypatch):
    """_refresh on a machine-local path must not touch S3 (no HEAD/GET) -- the
    local file is the source of truth, mirroring LocalBackend.refresh's no-op."""
    b = _backend(cloud)
    p = cloud["root"] / "world" / "presence" / "bravo.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("local-only\n")
    def _boom(*a, **k):
        raise AssertionError("head_object called for a machine-local path")
    monkeypatch.setattr(b.s3, "head_object", _boom)
    assert b.read_text(p, force_fresh=True) == "local-only\n"


def test_machine_local_classification(cloud):
    """_machine_local matches the sync-walk policy: _EXCLUDE_DIRS directories +
    _is_machine_local basenames are local; ordinary world stores still sync."""
    b = _backend(cloud)
    R = cloud["root"]
    assert b._machine_local(R / "world" / "presence" / "a.jsonl") is True   # _EXCLUDE_DIRS
    assert b._machine_local(R / "world" / ".history" / "f.txt") is True     # _EXCLUDE_DIRS
    assert b._machine_local(R / "world" / "changelog.jsonl") is True        # _EXCLUDE_NAMES
    assert b._machine_local(R / "world" / "reasoning-bank.jsonl") is False  # syncs
    assert b._machine_local(R / "world" / "knowledge" / "tree" / "x.md") is False  # syncs
