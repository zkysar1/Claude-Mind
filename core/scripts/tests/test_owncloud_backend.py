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


# --- botocore IfMatch preflight (P0: zeta zakbox1 own-cloud write incident) ---
# Own-cloud writes use PutObject(IfMatch=<etag>) compare-and-swap (needs
# botocore>=1.35). Old botocore rejects IfMatch CLIENT-SIDE (ParamValidationError,
# no network call), so every write silently fails while reads look healthy. The
# init preflight turns that latent per-write crash into one clear startup error;
# the _put catch is the runtime backstop.
class _FakePutObjModel:
    """Minimal stand-in for botocore's s3 service model, exposing only the
    PutObject input-shape members the preflight introspects."""
    def __init__(self, members):
        self._members = members

    def operation_model(self, name):
        assert name == "PutObject"
        shape = type("_Shape", (), {"members": self._members})()
        return type("_Op", (), {"input_shape": shape})()


class _FakeBotoSession:
    def __init__(self, members):
        self._members = members

    def get_service_model(self, service):
        assert service == "s3"
        return _FakePutObjModel(self._members)


def test_ifmatch_preflight_raises_when_model_lacks_ifmatch(monkeypatch):
    """botocore<1.35 has no IfMatch on the PutObject model -> preflight fails
    LOUD at init with the actionable upgrade guidance (the 'never silently
    again' win)."""
    import botocore.session
    from owncloud_backend import _assert_ifmatch_supported
    monkeypatch.setattr(botocore.session, "get_session",
                        lambda: _FakeBotoSession(members={}))  # no IfMatch
    with pytest.raises(RuntimeError, match="botocore>=1.35"):
        _assert_ifmatch_supported()


def test_ifmatch_preflight_passes_when_supported(monkeypatch):
    """IfMatch present in the model -> preflight is a silent no-op."""
    import botocore.session
    from owncloud_backend import _assert_ifmatch_supported
    monkeypatch.setattr(botocore.session, "get_session",
                        lambda: _FakeBotoSession(members={"IfMatch": object()}))
    _assert_ifmatch_supported()  # must not raise


def test_ifmatch_preflight_fails_open_when_introspection_errors(monkeypatch):
    """If the botocore model can't be introspected at all (internals change), the
    preflight fails OPEN — the _put ParamValidationError catch is the runtime
    backstop; a working backend must not be bricked by a meta-check error."""
    import botocore.session
    from owncloud_backend import _assert_ifmatch_supported

    def _boom():
        raise RuntimeError("botocore internals moved")
    monkeypatch.setattr(botocore.session, "get_session", _boom)
    _assert_ifmatch_supported()  # must not raise


def test_put_paramvalidation_remapped_to_upgrade_message(cloud, monkeypatch):
    """_put runtime backstop: a client-side ParamValidationError on a fenced PUT
    (IfMatch in play) is remapped to the actionable upgrade RuntimeError, never
    silently dropped."""
    from botocore.exceptions import ParamValidationError
    b = _backend(cloud)
    p = cloud["root"] / "world" / "pv.txt"
    b.write_text(p, "v1")                 # establishes the IfMatch fence for p

    def _raise_pv(**kw):
        raise ParamValidationError(report='Unknown parameter in input: "IfMatch"')
    monkeypatch.setattr(b.s3, "put_object", _raise_pv)
    with pytest.raises(RuntimeError, match="botocore>=1.35"):
        b.write_text(p, "v2")             # fenced PUT -> ParamValidationError -> remap


# --- read-path freshness keystone (own-cloud read-path class fix 2026-07-02) -
def test_ensure_local_noop_for_out_of_root_path(cloud):
    """Keystone: ensure_local on a git-shipped path under NO synced root
    (e.g. core/config/*.yaml) is a no-op -- returns the path, never raises.
    This lets dual-use readers (one code path reading BOTH a synced world/meta
    file AND a git-shipped config) call ensure_local unconditionally."""
    b = _backend(cloud)
    outside = cloud["root"] / "core" / "config" / "tree.yaml"  # under no synced root
    # _rel(outside) raises ValueError; the keystone swallows it in _refresh.
    assert b.ensure_local(outside) == outside
    b.refresh(outside)                    # force_fresh path is likewise a no-op


def test_ensure_local_materializes_missing_file_from_s3(cloud):
    """Fresh-box regression: an object present in S3 but ABSENT from the local
    cache is materialized by ensure_local (head->get->write). This is the read
    path a fresh own-cloud box hits for _tree.yaml -- the class the report
    flagged. Reads that route through jsonl_cache/yaml_cache call ensure_local
    before the stat, so this proves the fresh-box materialization they rely on."""
    b = _backend(cloud)
    p = cloud["root"] / "world" / "knowledge" / "tree" / "_tree.yaml"
    b.write_text(p, "nodes: {}\n")        # lands in S3 AND local
    p.unlink()                            # simulate a fresh box: blank local cache
    b._etags.clear()                      # ...and no in-process etag memory
    b._cache_check.clear()
    assert not p.exists()
    b.ensure_local(p)                     # must re-materialize from S3
    assert p.exists()
    assert p.read_text(encoding="utf-8") == "nodes: {}\n"


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


def test_live_long_turn_peer_not_reclaimable_at_incident_age(cloud):
    """Regression for the 2026-07-07 bravo dual-runner incident. A LIVE peer
    22 minutes into ONE max-effort LLM turn has a 1320s-old heartbeat (the DDB
    heartbeat only advances once per loop iteration via heartbeat-tick.sh) —
    stale under the original 900s design placeholder, which is exactly how a
    /start on cc-04 stale-broke cc-05's live claim and started a second bravo.
    Under the calibrated default (DEFAULT_RUNNER_STALE_SECONDS = 3900, i.e.
    the local runner_heartbeat.stale_minutes contract + margin) that claim is
    FRESH: reclaim refuses and the second acquire raises RunnerHeld, so
    /start answers held=true (ACQUIRE_RC=4) and refuses the second runner.
    A genuinely crashed peer (past the lease) must STILL be recoverable."""
    from owncloud_backend import DEFAULT_RUNNER_STALE_SECONDS, RunnerHeld
    live = _backend(cloud, machine_id="cc-05")
    second = _backend(cloud, machine_id="cc-04")
    assert live.acquire_runner("bravo", "tokLive") is True
    _set_heartbeat(cloud, "bravo", time.time() - 22 * 60)  # the incident gap
    assert second.reclaim_if_stale("bravo") is False, (
        "a 22-min-old heartbeat is a BUSY runner, not a crashed one — "
        "reclaim must refuse")
    with pytest.raises(RunnerHeld):
        second.acquire_runner("bravo", "tokSecond")
    # Crash recovery still works: age the heartbeat past the calibrated lease.
    _set_heartbeat(cloud, "bravo",
                   time.time() - (DEFAULT_RUNNER_STALE_SECONDS + 60))
    assert second.reclaim_if_stale("bravo") is True
    assert second.acquire_runner("bravo", "tokSecond") is True


def test_from_env_honors_ownership_stale_seconds(cloud, tmp_path, monkeypatch):
    """OWNERSHIP_STALE_SECONDS must reach the LOCK-BREAK, not just the sync
    filter. Before 2026-07-07 from_env never passed runner_stale_seconds, so
    the documented calibration knob silently governed only _owned_agents while
    reclaim_if_stale kept its hardcoded default — two consumers, two answers."""
    from owncloud_backend import DEFAULT_RUNNER_STALE_SECONDS, OwnCloudBackend
    world, meta, agents = tmp_path / "w", tmp_path / "m", tmp_path / "agents"
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(k, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("MIND_AWS_ALLOW_DEFAULT_CHAIN", "1")
    monkeypatch.setenv("STORAGE_S3_BUCKET", BUCKET)
    monkeypatch.setenv("STORAGE_DDB_LOCK_TABLE", LOCKS)
    monkeypatch.setenv("STORAGE_DDB_SESSIONS_TABLE", SESSIONS)
    monkeypatch.setenv("ENVIRONMENT_ID", ENV_ID)
    monkeypatch.setenv("MIND_WORLD", str(world))
    monkeypatch.setenv("MIND_META", str(meta))
    monkeypatch.setenv("AGENTS_ROOT", str(agents))
    monkeypatch.setenv("OWNERSHIP_STALE_SECONDS", "1234")
    assert OwnCloudBackend.from_env().runner_stale_seconds == 1234
    # Unset / garbage -> the calibrated default, never a crash.
    monkeypatch.delenv("OWNERSHIP_STALE_SECONDS", raising=False)
    assert (OwnCloudBackend.from_env().runner_stale_seconds
            == DEFAULT_RUNNER_STALE_SECONDS)
    monkeypatch.setenv("OWNERSHIP_STALE_SECONDS", "not-a-number")
    assert (OwnCloudBackend.from_env().runner_stale_seconds
            == DEFAULT_RUNNER_STALE_SECONDS)


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


def test_refresh_multipart_etag_downloads_not_no_clobber(cloud, tmp_path):
    """2026-07-02 fleet-wide-freeze regression: a multipart S3 ETag ('<hex>-N')
    with local == baseline (no unpushed writes; the L499 gate did NOT fire) must
    classify as "download" (S3-authoritative), NOT "no_clobber". The prior
    "no_clobber" never refreshed the in-process fence (self._etags), so _put kept
    sending IfMatch(stale) and every write to a multipart-stored file (the ~8MB
    world/aspirations.jsonl) 412'd DETERMINISTICALLY -- a fleet-wide write freeze.
    "download" pulls S3 and adopts the current ETag as the fence, curing it, while
    the L499 baseline gate still protects genuine unpushed local writes (rb-2096)."""
    b = _backend(cloud)
    p = cloud["root"] / "world" / "big.jsonl"
    local_body = b'{"v":1}\n'
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(local_body)
    # local == baseline -> the unpushed-writes gate (L499) does NOT fire.
    _write_sync_manifest(tmp_path, {b._rel(p): {
        "mtime": 1, "md5": hashlib.md5(local_body).hexdigest()}})
    multipart_etag = '"' + ("a" * 32) + '-3"'           # '<hex>-N' == multipart
    assert b._overwrite_decision(p, p, multipart_etag) == "download"
    # And a genuine unpushed local write under a multipart ETag is STILL protected
    # by the baseline gate (local != baseline -> no_clobber, fence stays stale ->
    # freeze-on-conflict, never a silent lost update).
    p.write_bytes(b'{"v":2,"unpushed":true}\n')          # local now diverges from baseline
    assert b._overwrite_decision(p, p, multipart_etag) == "no_clobber"


def test_refresh_nonmultipart_local_equals_baseline_downloads(cloud, tmp_path):
    """7 BRD sub-case (a) PINNING: NON-multipart S3 ETag + local ==
    baseline (no unpushed writes) + S3 moved (a peer wrote) -> "download"
    (adopt S3 + refresh the fence). This is the final fall-through in
    _overwrite_decision and already behaved correctly before g-115-1787 —
    pinned here so a future edit cannot regress the fence-refresh path that
    self-heals the sweep-lag false-positive freeze (the read-path mirror of
    the multipart pin above, minus the multipart trigger)."""
    b = _backend(cloud)
    p = cloud["root"] / "world" / "small.jsonl"
    local_body = b'{"v":1}\n'
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(local_body)
    # local == baseline -> the unpushed-writes gate does NOT fire.
    _write_sync_manifest(tmp_path, {b._rel(p): {
        "mtime": 1, "md5": hashlib.md5(local_body).hexdigest()}})
    # Non-multipart ETag of DIFFERENT content (a peer moved S3).
    peer_etag = '"' + hashlib.md5(b'{"v":2}\n').hexdigest() + '"'
    assert b._overwrite_decision(p, p, peer_etag) == "download"


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


# --- gap #5: both-diverged coordination-store merge () --------------
# When local holds unpushed writes AND S3 moved (both-diverged), a REGISTERED
# coordination store (reasoning-bank.jsonl, team-state.yaml) MERGES local+remote
# instead of freezing (stale fence -> perpetual 412) or clobbering the peer
# (empty post-restart fence -> unconditional PUT). See coordination_merge.py +
# owncloud_backend._merge_reconcile_put.
def _rb_blob(*recs):
    return ("".join(json.dumps(r, ensure_ascii=True) + "\n" for r in recs)).encode()


def _setup_both_diverged_rb(cloud, tmp_path, b):
    """Drive `b` into the both-diverged state for reasoning-bank.jsonl: local
    holds an unpushed machineA record (differs from the manifest baseline) and
    S3 was moved out-of-band by a peer (machineB). Returns (path, key)."""
    p = cloud["root"] / "world" / "reasoning-bank.jsonl"
    key = b._s3_key(p)
    base = _rb_blob({"id": "rb-1", "created": "2026-07-02T09:00:00", "title": "base"})
    b.write_text(p, base.decode())              # S3 == local == base; fence recorded
    _write_sync_manifest(tmp_path, {b._rel(p): {
        "mtime": 0, "md5": hashlib.md5(base).hexdigest()}})
    localA = base + _rb_blob({"id": "rb-2", "created": "2026-07-02T10:00:00",
                              "title": "machineA"})
    b._local(p).write_bytes(localA)             # machineA unpushed local write
    remoteB = base + _rb_blob({"id": "rb-2", "created": "2026-07-02T11:00:00",
                               "title": "machineB"})
    cloud["s3"].put_object(Bucket=BUCKET, Key=key, Body=remoteB)  # peer moved S3
    return p, key, localA


def test_both_diverged_registered_store_merges_not_freezes(cloud, tmp_path):
    """Stale-fence both-diverged write to reasoning-bank.jsonl MERGES: neither
    machine's record is lost, the id collision is re-id'd, divergence clears."""
    b = _backend(cloud)
    p, key, localA = _setup_both_diverged_rb(cloud, tmp_path, b)
    b.refresh(p)                                # RMW step 1: no_clobber -> flags key
    assert key in b._diverged_keys
    b.write_text(p, localA.decode())            # terminal write -> merge-reconcile
    merged = b.read_text(p, force_fresh=True)
    ids = {json.loads(l)["id"] for l in merged.splitlines()}
    titles = {json.loads(l).get("title") for l in merged.splitlines()}
    assert {"machineA", "machineB"} <= titles   # zero data loss (peer survives)
    assert ids == {"rb-1", "rb-2", "rb-3"}      # collision re-id'd rbB -> rb-3
    assert key not in b._diverged_keys          # divergence resolved


def test_both_diverged_empty_fence_merges_not_clobbers(cloud, tmp_path):
    """Post-restart (empty fence) both-diverged write MERGES instead of doing an
    unconditional PUT that would silently clobber the peer's S3 write."""
    b = _backend(cloud)
    p, key, localA = _setup_both_diverged_rb(cloud, tmp_path, b)
    b2 = _backend(cloud)                         # fresh backend == empty _etags fence
    assert key not in b2._etags
    b2.refresh(p)
    assert key in b2._diverged_keys
    b2.write_text(p, localA.decode())           # must merge, not unconditional clobber
    titles = {json.loads(l).get("title")
              for l in b2.read_text(p, force_fresh=True).splitlines()}
    assert "machineB" in titles and "machineA" in titles  # peer NOT clobbered


def test_both_diverged_unregistered_file_preserves_safe_freeze(cloud, tmp_path):
    """An unregistered store (no merge handler) keeps the safe-freeze: a
    stale-fence both-diverged write raises ConflictError, never clobbers.

    (g-115-1787 hygiene: this test previously used aspirations.jsonl labeled
    "NOT merge-registered" — stale since 74d227cd registered it; it only kept
    passing because the non-JSON body made the HANDLER raise. Re-pointed at a
    genuinely unregistered basename so it pins the intended path — the
    handler-is-None freeze — not the malformed-blob path, which now has its
    own pin below.)"""
    from owncloud_backend import ConflictError
    b = _backend(cloud)
    p = cloud["root"] / "world" / "unregistered-store.jsonl"  # no merge handler
    from coordination_merge import merge_handler_for
    assert merge_handler_for(p) is None                 # guard the premise
    assert b._machine_local(p) is False
    key = b._s3_key(p)
    base = b"line1\n"
    b.write_text(p, base.decode())              # S3 == local == base; fence E0
    _write_sync_manifest(tmp_path, {b._rel(p): {
        "mtime": 0, "md5": hashlib.md5(base).hexdigest()}})
    b._local(p).write_bytes(base + b"localA\n")             # unpushed local write
    cloud["s3"].put_object(Bucket=BUCKET, Key=key, Body=base + b"remoteB\n")  # peer
    b.refresh(p)
    assert key in b._diverged_keys
    with pytest.raises(ConflictError):
        b.write_text(p, (base + b"localA\n").decode())      # stale fence -> 412
    assert b._get_remote_raw(key)[0] == base + b"remoteB\n"  # peer intact, not clobbered


def _hyp_line(rec_id, **kw):
    """One pipeline.jsonl record line (writer byte format: ensure_ascii=True)."""
    rec = {"id": rec_id, "title": f"hyp {rec_id}", "stage": "active",
           "horizon": "short", "type": "calibration", "confidence": 0.5,
           "position": "YES — a multi-word testable claim",
           "formed_date": rec_id[:10], "category": "framework-architecture",
           "outcome": None, "reflected": False, "surprise": None}
    rec.update(kw)
    return (json.dumps(rec, ensure_ascii=True) + "\n").encode("utf-8")


def test_both_diverged_pipeline_store_merges_not_freezes(cloud, tmp_path):
    """7 END-TO-END (BRD P0 sub-case (b)): the EXACT cc-04 freeze
    shape — NON-multipart pipeline.jsonl, genuine both-diverged (real unpushed
    local records AND S3 moved) — must union-merge with ZERO data loss
    (local-only AND S3-only hypothesis records BOTH survive), refresh the
    fence, and leave the NEXT write clean. Pre-fix this raised ConflictError
    deterministically on every retry (fail-fast, cleared only by daemon
    restart — bravo's direct probe), freezing every hypothesis
    add/resolve/reflected fleet-wide. Mirror of the reasoning-bank e2e above;
    the write-path sibling of bdab36ab's multipart read-path fix."""
    b = _backend(cloud)
    p = cloud["root"] / "world" / "pipeline.jsonl"           # merge-REGISTERED now
    key = b._s3_key(p)
    base = _hyp_line("2026-07-01_base")
    b.write_text(p, base.decode())                            # S3 == local; fence E0
    _write_sync_manifest(tmp_path, {b._rel(p): {
        "mtime": 0, "md5": hashlib.md5(base).hexdigest()}})
    # Genuine both-diverged: local holds an unpushed record AND a peer moved S3.
    local_a = base + _hyp_line("2026-07-05_machine-a")
    b._local(p).write_bytes(local_a)
    cloud["s3"].put_object(Bucket=BUCKET, Key=key,
                           Body=base + _hyp_line("2026-07-06_machine-b"))
    b.refresh(p)                                              # no_clobber -> flags key
    assert key in b._diverged_keys
    b.write_text(p, local_a.decode())                         # must MERGE, not raise
    merged = b.read_text(p, force_fresh=True)
    ids = {json.loads(l)["id"] for l in merged.splitlines() if l.strip()}
    assert {"2026-07-01_base", "2026-07-05_machine-a",
            "2026-07-06_machine-b"} == ids                    # ZERO data loss
    assert key not in b._diverged_keys                        # divergence resolved
    # Fence refreshed: the very next fenced write lands clean (the freeze is
    # cured end-to-end, not just for one merge).
    b.write_text(p, merged + _hyp_line("2026-07-07_next").decode())
    after = {json.loads(l)["id"]
             for l in b.read_text(p, force_fresh=True).splitlines() if l.strip()}
    assert "2026-07-07_next" in after and ids <= after


def test_both_diverged_spark_store_merges_not_freezes(cloud, tmp_path):
    """rb-2849's second named victim: meta/spark-questions.jsonl. Genuine
    both-diverged counter bumps merge (per-counter MAX + derived yield_rate
    recompute) and a peer's new candidate survives — no freeze, no clobber."""
    b = _backend(cloud)
    p = cloud["root"] / "meta" / "spark-questions.jsonl"      # merge-REGISTERED now
    key = b._s3_key(p)

    def sq(asked):
        return (json.dumps(
            {"id": "sq-001", "text": "Q1?", "times_asked": asked,
             "sparks_generated": 3, "yield_rate": round(3 / max(asked, 1), 4),
             "status": "active", "category": "discovery", "type": "question"},
            ensure_ascii=True) + "\n").encode("utf-8")

    cand = (json.dumps(
        {"id": "sq-c01", "text": "C1?", "category": "discovery",
         "type": "candidate", "proposed_session": 7},
        ensure_ascii=True) + "\n").encode("utf-8")
    base = sq(10)
    b.write_text(p, base.decode())                            # S3 == local; fence E0
    _write_sync_manifest(tmp_path, {b._rel(p): {
        "mtime": 0, "md5": hashlib.md5(base).hexdigest()}})
    b._local(p).write_bytes(sq(11))                           # local: counter bump
    cloud["s3"].put_object(Bucket=BUCKET, Key=key, Body=sq(12) + cand)  # peer: bump + new candidate
    b.refresh(p)
    assert key in b._diverged_keys
    b.write_text(p, sq(11).decode())                          # must MERGE, not raise
    merged = [json.loads(l) for l in
              b.read_text(p, force_fresh=True).splitlines() if l.strip()]
    by_id = {r["id"]: r for r in merged}
    assert set(by_id) == {"sq-001", "sq-c01"}                 # peer's candidate survives
    assert by_id["sq-001"]["times_asked"] == 12               # counter MAX, no regress
    assert by_id["sq-001"]["yield_rate"] == round(3 / 12, 4)  # derived recomputed
    assert key not in b._diverged_keys


def test_merge_failure_on_registered_store_raises_conflict_not_clobbers(cloud, tmp_path):
    """A REGISTERED store whose remote blob is unparseable must surface
    ConflictError (the _merge_reconcile_put handler-exception wrap) and leave
    S3 intact — never clobber, never wedge silently. (Pins the malformed-blob
    safety path that the pre-g-115-1787 'unregistered aspirations.jsonl' test
    exercised by accident.)"""
    from owncloud_backend import ConflictError
    b = _backend(cloud)
    p = cloud["root"] / "world" / "pipeline.jsonl"            # merge-REGISTERED
    key = b._s3_key(p)
    base = _hyp_line("2026-07-01_base")
    b.write_text(p, base.decode())
    _write_sync_manifest(tmp_path, {b._rel(p): {
        "mtime": 0, "md5": hashlib.md5(base).hexdigest()}})
    local_a = base + _hyp_line("2026-07-05_machine-a")
    b._local(p).write_bytes(local_a)                          # unpushed local write
    garbage = b"this is not json\n"
    cloud["s3"].put_object(Bucket=BUCKET, Key=key, Body=garbage)  # corrupt remote
    b.refresh(p)
    assert key in b._diverged_keys
    with pytest.raises(ConflictError):
        b.write_text(p, local_a.decode())                     # merge fails LOUD
    assert b._get_remote_raw(key)[0] == garbage               # remote NOT clobbered


def test_hot_coordination_store_412_merges_with_empty_diverged_flag(cloud, tmp_path):
    """1: a HOT coordination store (team-state.yaml, written every
    iteration) whose in-process fence went stale but whose key was NEVER added
    to _diverged_keys -- because _refresh's warm-cache early-return skips the
    no_clobber divergence detection for an always-warm cache -- MERGE-RECONCILES
    on the _put 412 instead of dead-looping ConflictError. The merge PRE-check at
    _put L663 MISSES here (empty _diverged_keys); the fix's 412-handler POST-check
    routes to _merge_reconcile_put, which re-GETs the FRESH remote ETag. This is
    the write-path twin of test_refresh_multipart_etag_downloads_not_no_clobber
    (bdab36a's read-path fix) and the deterministic >22min single-writer deadlock
    zeta observed on cc-02. Contrast the unregistered-store test above: an
    UNregistered store still raises ConflictError here -- the fix is scoped to
    stores with a commutative merge handler (rb-2096 freeze-safety preserved)."""
    b = _backend(cloud)
    p = cloud["root"] / "world" / "team-state.yaml"          # merge-REGISTERED
    key = b._s3_key(p)
    base = (b"last_updated: '2026-07-02T09:00:00'\n"
            b"agent_status:\n  alpha:\n    last_active: '2026-07-02T09:00:00'\n")
    b.write_text(p, base.decode())                           # S3 == local; fence E0 recorded
    # Peer moves S3 out-of-band -> the in-process fence (E0) is now stale.
    # Crucially: NO refresh() is called, so _diverged_keys stays EMPTY -- exactly
    # the always-warm-cache production case the L663 pre-check cannot catch.
    peer = (b"last_updated: '2026-07-02T11:00:00'\n"
            b"agent_status:\n  alpha:\n    last_active: '2026-07-02T09:00:00'\n"
            b"  bravo:\n    last_active: '2026-07-02T11:00:00'\n")
    cloud["s3"].put_object(Bucket=BUCKET, Key=key, Body=peer)
    assert key not in b._diverged_keys                       # the bug's precondition: NOT flagged
    # This stale-fence write WOULD 412. PRE-fix: ConflictError -> _fileops RMW
    # re-hits the same warm-cache-stale-fence 412 -> deterministic deadlock.
    # POST-fix: the 412 handler routes to merge-reconcile (no exception).
    localA = (b"last_updated: '2026-07-02T12:00:00'\n"
              b"agent_status:\n  alpha:\n    last_active: '2026-07-02T12:00:00'\n")
    b.write_text(p, localA.decode())                         # must merge-reconcile, NOT raise
    merged = b.read_text(p, force_fresh=True).encode()
    assert b"alpha" in merged                                # local write applied
    assert b"bravo" in merged                                # peer's write PRESERVED (merged, not clobbered)
    assert key not in b._diverged_keys                       # resolved (merge-reconcile discards on success)


# --- : CAS retry / backoff / jitter + 409-rate metric ----------------
def test_cas_metrics_start_at_zero(cloud):
    """A fresh backend reports an all-zero CAS metric snapshot — the "measurable"
    409-rate surface the goal names exists and reads 0.0 before any fenced write."""
    b = _backend(cloud)
    assert b.cas_metrics() == {
        "writes": 0, "conflicts": 0, "resolved": 0, "conflict_rate": 0.0}


def test_conflict_backoff_is_jittered_and_bounded():
    """: _conflict_backoff now draws FULL jitter over [0, capped-exp]
    instead of the old deterministic min(0.05*2**n, 1.0). Assert (a) jitter is
    present (repeated draws are NOT all identical — the pre-g-328-21 code returned
    a constant), (b) every draw stays within [0, cap], (c) attempt 0 window is
    <=0.05, (d) the 1.0s cap holds at a large attempt."""
    from owncloud_backend import _conflict_backoff
    a0 = [_conflict_backoff(0) for _ in range(200)]
    assert all(0.0 <= v <= 0.05 for v in a0)           # attempt 0 window
    assert len(set(a0)) > 1                             # jitter present (not constant)
    a10 = [_conflict_backoff(10) for _ in range(200)]  # 0.05*2**10 = 51.2 -> capped 1.0
    assert all(0.0 <= v <= 1.0 for v in a10)           # cap holds
    assert len(set(a10)) > 1                            # jitter present at the cap too


def test_cas_412_then_success_completes_via_retry(cloud, monkeypatch):
    """The goal's named check: a 409 (412 PreconditionFailed) followed by success
    COMPLETES via the bounded merge-reconcile retry loop rather than propagating a
    hard failure. Drives TWO 412s — the _put stale-fence 412 (routes into
    merge-reconcile) AND a synthetic 412 on the merge-reconcile's first PUT (a peer
    moving S3 mid-merge) — so the jittered-backoff retry path (attempt>0) is
    exercised before the retry succeeds. Asserts the write lands (no raise), the
    peer write is preserved (merge, not clobber), and the 409-rate metric counted
    both conflicts and the resolution."""
    # No real sleep in the retry loop — patch the module-global backoff to 0
    # (mirrors test_fileops_conflict_retry). Jitter itself is covered above.
    monkeypatch.setattr("owncloud_backend._conflict_backoff", lambda *_: 0.0)

    b = _backend(cloud)
    p = cloud["root"] / "world" / "team-state.yaml"          # merge-REGISTERED
    key = b._s3_key(p)
    base = (b"last_updated: '2026-07-02T09:00:00'\n"
            b"agent_status:\n  alpha:\n    last_active: '2026-07-02T09:00:00'\n")
    b.write_text(p, base.decode())                           # S3 == local; fence E0
    # Peer moves S3 out-of-band -> the in-process fence (E0) is now stale (a real
    # 412 on the next fenced _put). Done through the real client BEFORE the flaky
    # wrapper is installed, so this peer write is not intercepted.
    peer = (b"last_updated: '2026-07-02T11:00:00'\n"
            b"agent_status:\n  alpha:\n    last_active: '2026-07-02T09:00:00'\n"
            b"  bravo:\n    last_active: '2026-07-02T11:00:00'\n")
    cloud["s3"].put_object(Bucket=BUCKET, Key=key, Body=peer)

    # One-shot-412 wrapper: the 2nd put_object from here (the merge-reconcile's
    # FIRST PUT) raises PreconditionFailed synthetically, so the bounded retry loop
    # must back off and re-GET/re-merge before succeeding. Call #1 = the _put fenced
    # PUT (moto 412s it naturally on the stale E0 fence).
    real_put = b.s3.put_object
    n = {"c": 0}

    def flaky_put(**kw):
        n["c"] += 1
        if n["c"] == 2:
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed",
                           "Message": "peer moved S3 mid-merge"}}, "PutObject")
        return real_put(**kw)

    monkeypatch.setattr(b.s3, "put_object", flaky_put)

    b.write_text(p, (b"last_updated: '2026-07-02T12:00:00'\n"
                     b"agent_status:\n  alpha:\n"
                     b"    last_active: '2026-07-02T12:00:00'\n").decode())

    merged = b.read_text(p, force_fresh=True).encode()
    assert b"alpha" in merged and b"bravo" in merged   # local applied, peer preserved
    assert key not in b._diverged_keys                 # divergence resolved
    m = b.cas_metrics()
    assert m["conflicts"] == 2                          # _put 412 + merge-loop 412
    assert m["resolved"] == 1                           # the terminal write recovered
    assert m["writes"] >= 2 and m["conflict_rate"] > 0.0  # rate is measurable


# --- list_dir on governed root (cold-bootstrap fix, 2) ---------------

def _backend_root_map(cloud, root_map, machine_id="m1"):
    """Build an OwnCloudBackend with an explicit root_map (not cache_root).
    This exercises the _s3_key -> _rel path that produced the governed-root
    prefix bug (g-115-1752): _rel maps a root path to '<prefix>/.' instead
    of '<prefix>', so list_dir searched 'env-id/prefix/./' — matching nothing."""
    from owncloud_backend import OwnCloudBackend
    return OwnCloudBackend(
        env_id=ENV_ID, bucket=BUCKET, lock_table=LOCKS,
        sessions_table=SESSIONS, root_map=root_map,
        machine_id=machine_id, region=REGION,
        s3=cloud["s3"], ddb=cloud["ddb"])


def test_list_dir_governed_root_returns_children(cloud):
    """list_dir on a governed ROOT path (path == root in root_map) must list
    S3 children under 'env-id/prefix/', NOT under 'env-id/prefix/./' which
    matches nothing.  Regression test for the cold-bootstrap NO-OP
    (g-115-1752): pull_bootstrap -> _materialize_tree calls
    list_dir(root_path) as its first S3-walk step; a wrong prefix returns []
    and the entire tree is skipped — 'scanned 0, pulled 0'."""
    world = cloud["root"] / "world_governed"
    world.mkdir()
    b = _backend_root_map(cloud, [(str(world), "world")])
    s3 = cloud["s3"]
    s3.put_object(Bucket=BUCKET, Key=f"{ENV_ID}/world/.initialized", Body=b"")
    s3.put_object(Bucket=BUCKET,
                  Key=f"{ENV_ID}/world/knowledge/tree/_tree.yaml",
                  Body=b"nodes: {}\n")
    s3.put_object(Bucket=BUCKET,
                  Key=f"{ENV_ID}/world/program.md",
                  Body=b"# The Program\n")

    children = b.list_dir(world)
    assert ".initialized" in children
    assert "knowledge" in children
    assert "program.md" in children
    assert len(children) == 3


def test_list_dir_governed_root_subdir_still_works(cloud):
    """list_dir on a subdirectory UNDER a governed root must keep working
    (the fix must not break the non-root case)."""
    world = cloud["root"] / "world_governed"
    world.mkdir()
    b = _backend_root_map(cloud, [(str(world), "world")])
    s3 = cloud["s3"]
    s3.put_object(Bucket=BUCKET,
                  Key=f"{ENV_ID}/world/knowledge/tree/_tree.yaml",
                  Body=b"nodes: {}\n")
    s3.put_object(Bucket=BUCKET,
                  Key=f"{ENV_ID}/world/knowledge/tree/system/node.md",
                  Body=b"system node\n")

    children = b.list_dir(world / "knowledge")
    assert children == ["tree"]
    children2 = b.list_dir(world / "knowledge" / "tree")
    assert "_tree.yaml" in children2
    assert "system" in children2


def test_pull_bootstrap_real_backend_materializes_tree(cloud, monkeypatch):
    """End-to-end pull_bootstrap with a REAL OwnCloudBackend + moto S3 on an
    EMPTY local dir.  The existing pull_bootstrap tests in test_owncloud_sync.py
    use a FakeBackend whose list_dir works on local paths — they do NOT exercise
    the real _s3_key -> list_objects_v2 prefix mapping, so they stay green while
    the real backend returns 'scanned 0, pulled 0'.  This test catches that gap
    (g-115-1752)."""
    import owncloud_sync as _sync

    world = cloud["root"] / "world_governed"
    meta = cloud["root"] / "meta_governed"
    world.mkdir()
    meta.mkdir()
    b = _backend_root_map(
        cloud, [(str(world), "world"), (str(meta), "meta")])

    # Seed S3 under the ENVIRONMENT_ID prefix with a minimal world tree.
    s3 = cloud["s3"]
    s3.put_object(Bucket=BUCKET,
                  Key=f"{ENV_ID}/world/.initialized", Body=b"")
    s3.put_object(Bucket=BUCKET,
                  Key=f"{ENV_ID}/world/knowledge/tree/_tree.yaml",
                  Body=b"nodes: {}\n")
    s3.put_object(Bucket=BUCKET,
                  Key=f"{ENV_ID}/world/knowledge/tree/system/node.md",
                  Body=b"---\ntitle: system\n---\nSystem node\n")
    s3.put_object(Bucket=BUCKET,
                  Key=f"{ENV_ID}/world/program.md",
                  Body=b"# The Program\n")

    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    monkeypatch.setenv("RUNTIME_DIR", str(cloud["root"] / "rt"))

    # Local is EMPTY — the fresh-box case.
    assert not (world / ".initialized").exists()

    stats = _sync.pull_bootstrap(b, only_root="world")

    assert stats["skipped"] is None
    assert stats["pulled"] >= 4, (
        f"expected >= 4 pulled files, got {stats['pulled']}; "
        f"scanned={stats['scanned']} — list_dir may still be broken")
    assert (world / ".initialized").exists()
    assert (world / "knowledge" / "tree" / "_tree.yaml").exists()
    assert (world / "knowledge" / "tree" / "_tree.yaml").read_bytes() == b"nodes: {}\n"
    assert (world / "knowledge" / "tree" / "system" / "node.md").exists()
    assert (world / "program.md").read_bytes() == b"# The Program\n"


# --- : fail-loud on IAM/permission gap (AccessDenied) ----------------
# The 2026-07-04 fleet-wedge () root cause: a missing dynamodb:Scan grant
# let list_runner_claims' Scan silently degrade to "owns no agent dirs" for days.
# These tests pin the fix — a governed DDB/S3 op that hits AccessDenied surfaces a
# diagnosable OwnCloudPermissionError, NOT an empty/no-op result. The injected
#  client method is monkeypatched to raise, so the path under test is the
# REAL production except-branch, not a fake backend walking local paths (guard-919).
def _access_denied_error(code="AccessDeniedException", op="Scan", msg="not authorized"):
    return ClientError({"Error": {"Code": code, "Message": msg}}, op)


def _raiser(exc):
    def _f(*a, **k):
        raise exc
    return _f


def test_list_runner_claims_access_denied_fails_loud(cloud, monkeypatch):
    from owncloud_backend import OwnCloudPermissionError
    b = _backend(cloud)
    monkeypatch.setattr(b.ddb, "scan", _raiser(_access_denied_error(
        msg="User is not authorized to perform: dynamodb:Scan on resource: zds-sessions")))
    with pytest.raises(OwnCloudPermissionError) as ei:
        b.list_runner_claims()
    msg = str(ei.value)
    assert "list_runner_claims" in msg             # names the governed op
    assert "IAM" in msg or "dynamodb:Scan" in msg  # points at the grant to check


def test_list_dir_access_denied_fails_loud(cloud, monkeypatch):
    from owncloud_backend import OwnCloudPermissionError
    b = _backend(cloud)
    monkeypatch.setattr(b.s3, "list_objects_v2", _raiser(_access_denied_error(
        code="AccessDenied", op="ListObjectsV2", msg="not authorized: s3:ListBucket")))
    with pytest.raises(OwnCloudPermissionError) as ei:
        b.list_dir(cloud["root"] / "world")
    assert "list_dir" in str(ei.value)


def test_health_check_ok_when_permissions_present(cloud):
    # moto grants everything and the fixture creates the sessions table + bucket.
    result = _backend(cloud).health_check()
    assert result["ok"] is True
    assert set(result["checked"]) == {"ddb:Scan", "s3:ListBucket"}


def test_health_check_fails_loud_on_ddb_access_denied(cloud, monkeypatch):
    from owncloud_backend import OwnCloudPermissionError
    b = _backend(cloud)
    monkeypatch.setattr(b.ddb, "scan", _raiser(_access_denied_error(msg="no dynamodb:Scan")))
    with pytest.raises(OwnCloudPermissionError):
        b.health_check()


def test_health_check_fails_loud_on_s3_access_denied(cloud, monkeypatch):
    # DDB scan succeeds (moto), S3 list is denied — the probe still fails loud.
    from owncloud_backend import OwnCloudPermissionError
    b = _backend(cloud)
    monkeypatch.setattr(b.s3, "list_objects_v2", _raiser(_access_denied_error(
        code="AccessDenied", op="ListObjectsV2", msg="no s3:ListBucket")))
    with pytest.raises(OwnCloudPermissionError):
        b.health_check()


def test_non_permission_client_error_not_masked(cloud, monkeypatch):
    # A NON-permission ClientError (throttling) must NOT be reclassified as
    # OwnCloudPermissionError — the helper fires only for _ACCESS_DENIED codes, so
    # the original ClientError propagates unchanged (no over-broad masking).
    b = _backend(cloud)
    monkeypatch.setattr(b.ddb, "scan", _raiser(_access_denied_error(
        code="ThrottlingException", op="Scan", msg="rate exceeded")))
    with pytest.raises(ClientError) as ei:
        b.list_runner_claims()
    assert ei.value.response["Error"]["Code"] == "ThrottlingException"
