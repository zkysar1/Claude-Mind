"""G1 (machine-2 gate): _rmw_with_conflict_retry — the optimistic-concurrency
retry that wraps _fileops' locked RMW helpers.

Two layers:
  - Primitive unit tests (no S3/moto, instant): the primitive only touches
    get_backend().conflict_error, so a stub backend exercises the
    success / retry-then-succeed / cap-exhaustion / local-passthrough /
    non-conflict-propagation branches.
  - One end-to-end test (moto): locked_modify_jsonl driven against a real
    OwnCloudBackend whose atomic_write raises ConflictError ONCE, proving the
    wrapped cycle re-reads fresh, re-applies the modifier, and converges with
    NO duplicate record (a 412 is an atomic reject — nothing landed).
"""
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]  # core/scripts
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _fileops  # noqa: E402


class _Conflict(Exception):
    """Stand-in for the backend's optimistic-concurrency exception."""


class _StubBackend:
    def __init__(self, conflict_error):
        self.conflict_error = conflict_error


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    # Null the inter-retry backoff so the unit tests are instant.
    monkeypatch.setattr(_fileops, "_conflict_backoff", lambda *_: 0)


def _use_backend(monkeypatch, conflict_error):
    monkeypatch.setattr(_fileops, "get_backend",
                        lambda: _StubBackend(conflict_error))


# --- primitive branches -----------------------------------------------------
def test_succeeds_first_try_no_retry(monkeypatch):
    _use_backend(monkeypatch, _Conflict)
    calls = []
    assert _fileops._rmw_with_conflict_retry(
        Path("x"), lambda: (calls.append(1), "ok")[1]) == "ok"
    assert len(calls) == 1


def test_retries_then_succeeds(monkeypatch):
    _use_backend(monkeypatch, _Conflict)
    calls = []

    def cycle():
        calls.append(1)
        if len(calls) == 1:            # fail once, then succeed on re-run
            raise _Conflict("stale fence")
        return "ok"

    assert _fileops._rmw_with_conflict_retry(Path("x"), cycle) == "ok"
    assert len(calls) == 2


def test_reraises_after_cap(monkeypatch):
    _use_backend(monkeypatch, _Conflict)
    calls = []

    def cycle():
        calls.append(1)
        raise _Conflict("always stale")

    with pytest.raises(_Conflict):
        _fileops._rmw_with_conflict_retry(Path("x"), cycle)
    # Bounded — exactly _CONFLICT_RETRY_CAP attempts, not an infinite loop.
    assert len(calls) == _fileops._CONFLICT_RETRY_CAP


def test_local_backend_empty_tuple_is_single_pass(monkeypatch):
    # conflict_error == () -> `except ()` matches nothing -> transparent
    # single pass (the default LocalBackend path adds zero retries).
    _use_backend(monkeypatch, ())
    calls = []
    assert _fileops._rmw_with_conflict_retry(
        Path("x"), lambda: (calls.append(1), "ok")[1]) == "ok"
    assert len(calls) == 1


def test_non_conflict_exception_not_retried(monkeypatch):
    _use_backend(monkeypatch, _Conflict)
    calls = []

    def cycle():
        calls.append(1)
        raise ValueError("unrelated")

    with pytest.raises(ValueError):
        _fileops._rmw_with_conflict_retry(Path("x"), cycle)
    assert len(calls) == 1             # ValueError != _Conflict -> no retry


# --- end-to-end: locked_modify_jsonl retries a real injected conflict -------
def test_locked_modify_jsonl_retries_real_conflict(monkeypatch, tmp_path):
    moto = pytest.importorskip("moto")
    boto3 = pytest.importorskip("boto3")
    from moto import mock_aws
    import owncloud_backend as ocb

    region = "us-east-2"
    with mock_aws():
        s3 = boto3.client("s3", region_name=region)
        ddb = boto3.client("dynamodb", region_name=region)
        s3.create_bucket(
            Bucket="lodestar-rmw-test",
            CreateBucketConfiguration={"LocationConstraint": region})
        for tbl, key in (("locks", "lock_key"), ("sessions", "session_key")):
            ddb.create_table(
                TableName=tbl,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST")
        be = ocb.OwnCloudBackend(
            env_id="e", bucket="lodestar-rmw-test", lock_table="locks",
            sessions_table="sessions", cache_root=tmp_path,
            machine_id="m1", region=region, s3=s3, ddb=ddb)

        # Flaky wrapper: the FIRST atomic_write raises ConflictError (simulating
        # a stale-lock-break 412), every later call delegates to the real one.
        real_atomic = be.atomic_write
        state = {"n": 0}

        def flaky_atomic(target, write_to_handle, *, max_retries=10):
            state["n"] += 1
            if state["n"] == 1:
                raise ocb.ConflictError("injected stale-fence once")
            return real_atomic(target, write_to_handle, max_retries=max_retries)

        monkeypatch.setattr(be, "atomic_write", flaky_atomic)
        monkeypatch.setattr(_fileops, "get_backend", lambda: be)
        monkeypatch.setattr(_fileops, "_conflict_backoff", lambda *_: 0)

        p = tmp_path / "store.jsonl"
        be.write_jsonl(p, [{"id": 1}])

        result = _fileops.locked_modify_jsonl(
            p, lambda items: items + [{"id": 2}])

        # Converged: the modifier applied exactly once despite the injected 412.
        assert result == [{"id": 1}, {"id": 2}]
        assert state["n"] == 2                       # one fail + one success
        assert be._read_jsonl_fresh(p) == [{"id": 1}, {"id": 2}]  # S3 truth
        assert (tmp_path / "store.jsonl").exists()


def test_write_all_raises_conflict_not_silently_retried(monkeypatch, tmp_path):
    """Guard: locked_write_jsonl is WRITE-ALL — under own-cloud a stale cached
    If-Match fence must surface as a loud conflict_error (nothing landed), NOT be
    silently conflict-retried into a clobber of the peer's write. Locks in the
    documented constraint so a future change can't 'helpfully' wrap it."""
    pytest.importorskip("moto")
    boto3 = pytest.importorskip("boto3")
    from moto import mock_aws
    import owncloud_backend as ocb

    region = "us-east-2"
    bucket = "lodestar-rmw-test"
    with mock_aws():
        s3 = boto3.client("s3", region_name=region)
        ddb = boto3.client("dynamodb", region_name=region)
        s3.create_bucket(
            Bucket=bucket,
            CreateBucketConfiguration={"LocationConstraint": region})
        for tbl, key in (("locks", "lock_key"), ("sessions", "session_key")):
            ddb.create_table(
                TableName=tbl,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST")
        be = ocb.OwnCloudBackend(
            env_id="e", bucket=bucket, lock_table="locks",
            sessions_table="sessions", cache_root=tmp_path,
            machine_id="m1", region=region, s3=s3, ddb=ddb)
        monkeypatch.setattr(_fileops, "get_backend", lambda: be)

        p = tmp_path / "store.jsonl"
        _fileops.locked_write_jsonl(p, [{"id": 1}])      # caches fence E1
        s3.put_object(Bucket=bucket, Key=be._s3_key(p),
                      Body=b'{"id": 99}\n')               # peer moves remote -> E2
        with pytest.raises(ocb.ConflictError):
            # If-Match E1 != E2 -> 412 surfaces (NOT auto-retried into a clobber).
            _fileops.locked_write_jsonl(p, [{"id": 2}])
