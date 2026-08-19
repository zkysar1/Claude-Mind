"""moto-mocked tests for the batch prefetch primitive ().

100% local: moto intercepts every boto3 call with fake creds — NO real AWS, NO
network, NO cloud writes.

What this pins, and why each test exists:

  - THE WIN: after `prefetch`, a walk of N files issues ZERO head_object calls.
    That is the entire point of the goal — the operator's mail measured a tree
    walk at 1368 HEAD / 78.3s, all of it per-file freshness probes that one
    bulk listing can answer at ~1 request per 1000 keys.
  - THE INVARIANT: prefetch may reduce requests, NEVER change what a read
    returns. The stale-local test is the load-bearing one: if prefetch ever
    warms a file whose local copy is behind S3, every later read silently
    serves stale bytes, and it would do so INVISIBLY. A cache that is merely
    fast is worthless next to one that is correct, so the mismatch path is
    tested harder than the hit path.
  - FAIL-OPEN: a broken listing must degrade to today's behavior, not raise.

File basename starts with ``test_`` so domain-leak-check.sh skips it (the boto3
/ S3 tokens here are test infrastructure, not a domain leak).
"""
import sys
from pathlib import Path

import pytest

# core/scripts on sys.path so `import owncloud_backend` resolves (mirrors the
# import convention used by the sibling test modules in this directory).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

moto = pytest.importorskip("moto")
boto3 = pytest.importorskip("boto3")
from moto import mock_aws  # noqa: E402

BUCKET = "zds-data"
LOCKS = "zds-locks"
SESSIONS = "zds-sessions"
REGION = "us-east-2"
ENV_ID = "ayoai-mind"


@pytest.fixture(autouse=True)
def _default_machine_id(monkeypatch):
    """from_env() fail-closes when MACHINE_ID is unset/'unknown' (G5)."""
    monkeypatch.setenv("MACHINE_ID", "test-machine-ci")


@pytest.fixture(autouse=True)
def _isolate_sync_manifest(monkeypatch, tmp_path):
    """_refresh's no-clobber guard lazily reads owncloud_sync's persistent
    sync-manifest via RUNTIME_DIR (g-115-1574). Point it at a per-test tmp dir
    so these tests never read real machine state."""
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
        for table, key in ((LOCKS, "lock_key"), (SESSIONS, "session_key")):
            ddb.create_table(
                TableName=table,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key,
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


def _go_cold(b):
    """Drop the in-process caches, reproducing a fresh process / daemon
    restart — the state in which a tree walk pays one HEAD per file."""
    b._etags.clear()
    b._cache_check.clear()


def _count_heads(b, monkeypatch):
    """Record every head_object Key the backend issues. Returns the live list."""
    seen = []
    orig = b.s3.head_object

    def _counting(**kw):
        seen.append(kw.get("Key"))
        return orig(**kw)

    monkeypatch.setattr(b.s3, "head_object", _counting)
    return seen


# --- the win ----------------------------------------------------------------
def test_prefetch_eliminates_the_per_read_head(cloud, monkeypatch):
    """One bulk listing replaces N per-file HEADs.

    Cold-reads N files and counts the HEADs (the status quo the mail
    measured), then repeats from an equally cold cache WITH a prefetch in
    front and asserts the count falls to zero while every byte returned is
    unchanged. The before-count is asserted too: if reads stopped issuing
    HEADs for some unrelated reason, a bare `== 0` after would pass while
    measuring nothing."""
    b = _backend(cloud)
    names = [f"n{i}.md" for i in range(5)]
    for i, n in enumerate(names):
        b.write_text(cloud["root"] / "world" / n, f"body{i}")

    # BEFORE: cold walk pays one HEAD per file.
    _go_cold(b)
    heads = _count_heads(b, monkeypatch)
    for i, n in enumerate(names):
        assert b.read_text(cloud["root"] / "world" / n) == f"body{i}"
    assert len(heads) == len(names), (
        f"expected one HEAD per cold read, got {len(heads)}")

    # AFTER: one listing warms them all; the same walk pays zero HEADs.
    _go_cold(b)
    del heads[:]
    stats = b.prefetch(cloud["root"] / "world")
    assert stats["listed"] == len(names)
    assert stats["warmed"] == len(names)
    assert stats["errors"] == 0
    for i, n in enumerate(names):
        assert b.read_text(cloud["root"] / "world" / n) == f"body{i}"
    assert heads == [], f"prefetch should have removed every HEAD, got {heads}"


# --- the invariant ----------------------------------------------------------
def test_prefetch_refuses_to_warm_a_stale_local_copy(cloud, monkeypatch):
    """THE load-bearing test: a local copy behind S3 must NOT be warmed.

    Another box advances the object; this box's mirror still holds the old
    bytes. The listing's ETag no longer matches the local md5, so prefetch
    must skip it and let the read take its normal HEAD+GET path. A warm here
    would serve stale bytes silently forever after."""
    b = _backend(cloud)
    p = cloud["root"] / "world" / "s.md"
    b.write_text(p, "v1")

    # A peer advances S3 directly; the local mirror still says "v1".
    cloud["s3"].put_object(Bucket=BUCKET, Key=f"{ENV_ID}/world/s.md",
                           Body=b"v2-from-a-peer")
    assert p.read_bytes() == b"v1"

    _go_cold(b)
    stats = b.prefetch(cloud["root"] / "world")
    assert stats["warmed"] == 0, "a stale local copy must never be warmed"
    assert stats["skipped_mismatch"] == 1

    # The read still reaches S3 and returns the CURRENT bytes.
    heads = _count_heads(b, monkeypatch)
    assert b.read_text(p) == "v2-from-a-peer"
    assert len(heads) == 1, "an unwarmed file must still take its HEAD"


def test_prefetch_skips_a_key_with_no_local_copy(cloud):
    """A remote-only key has nothing to prove current — the read must GET it
    anyway, so warming it would be both pointless and wrong."""
    b = _backend(cloud)
    cloud["s3"].put_object(Bucket=BUCKET, Key=f"{ENV_ID}/world/remote.md",
                           Body=b"remote-only")
    _go_cold(b)
    stats = b.prefetch(cloud["root"] / "world")
    assert stats["listed"] == 1
    assert stats["warmed"] == 0
    assert stats["skipped_no_local"] == 1
    # Still readable through the normal materializing path.
    assert b.read_text(cloud["root"] / "world" / "remote.md") == "remote-only"


def test_prefetch_counts_every_skip_rather_than_dropping_it(cloud):
    """listed must reconcile against warmed + the skip counters.

    An optimization that reports only its hits cannot be measured — a caller
    could never tell a fully-warmed tree from one where nothing matched."""
    b = _backend(cloud)
    b.write_text(cloud["root"] / "world" / "hit.md", "x")
    cloud["s3"].put_object(Bucket=BUCKET, Key=f"{ENV_ID}/world/miss.md",
                           Body=b"no local copy")
    _go_cold(b)
    s = b.prefetch(cloud["root"] / "world")
    accounted = (s["warmed"] + s["skipped_no_local"] + s["skipped_multipart"]
                 + s["skipped_mismatch"] + s["skipped_machine_local"]
                 + s["errors"])
    assert accounted == s["listed"] == 2


# --- fail-open --------------------------------------------------------------
def test_prefetch_is_fail_open_when_the_listing_fails(cloud, monkeypatch):
    """A broken listing degrades to today's behavior — it never raises and it
    never warms. An optimization that can take down the read path is worse
    than no optimization."""
    b = _backend(cloud)
    b.write_text(cloud["root"] / "world" / "a.md", "a")

    def _boom(*a, **kw):
        raise RuntimeError("listing exploded")

    monkeypatch.setattr(b, "list_objects", _boom)
    _go_cold(b)
    stats = b.prefetch(cloud["root"] / "world")
    assert stats["errors"] == 1
    assert stats["warmed"] == 0
    assert "listing exploded" in stats["error"]
    # Reads still work, exactly as before.
    assert b.read_text(cloud["root"] / "world" / "a.md") == "a"


def test_prefetch_ttl_is_the_existing_cache_ttl_not_a_second_concept(cloud):
    """Batch validity inherits cache_ttl rather than inventing a new expiry.

    The reported window must be the one the consuming code path actually
    applies, or the number is decorative."""
    b = _backend(cloud, cache_ttl=7)
    b.write_text(cloud["root"] / "world" / "t.md", "t")
    _go_cold(b)
    assert b.prefetch(cloud["root"] / "world")["ttl_seconds"] == 7 == b.cache_ttl


# --- local backend ----------------------------------------------------------
def test_local_backend_prefetch_is_a_no_op(tmp_path):
    """LocalBackend must walk NOTHING: a local read issues no remote probe, so
    a stat sweep here would add real I/O to the default path to save a cost
    that does not exist."""
    from storage_backend import LocalBackend
    (tmp_path / "world").mkdir()
    (tmp_path / "world" / "a.md").write_text("a", encoding="utf-8")
    stats = LocalBackend().prefetch(tmp_path / "world")
    assert stats["backend"] == "local"
    assert stats["listed"] == 0 and stats["warmed"] == 0
    assert "no per-file remote cost" in stats["reason"]


def test_prefetch_is_on_the_storage_backend_protocol():
    """The goal's first outcome: the primitive is on the PROTOCOL, not just on
    one implementation — otherwise no caller can use it polymorphically."""
    import storage_backend
    assert hasattr(storage_backend.StorageBackend, "prefetch")
    assert hasattr(storage_backend.LocalBackend, "prefetch")
    from owncloud_backend import OwnCloudBackend
    assert hasattr(OwnCloudBackend, "prefetch")
