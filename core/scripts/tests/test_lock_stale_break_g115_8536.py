"""Stale-break diagnostics + lost-update reproduction ().

The framework's mutual exclusion has a documented hole: LocalBackend.acquire_lock
breaks any lock file older than stale_seconds (file_locks.locked defaults to 10s),
and OwnCloudBackend's DDB conditional put steals on ttl < now. Both fired with
ZERO trace until the 2026-09-01 instrumentation. This file:

  1. pins the new ``[lock-stale-break]`` diagnostic on both backends
     (breaker side local, victim side own-cloud via moto), and
  2. REPRODUCES the lost update deterministically — step 2 of g-115-8536.
     The sequential interleaving IS the race schedule: backdating the lock's
     mtime/ttl stands in for "holder stalled inside the critical section",
     so there are no sleeps, no threads, no flakes.

ONE TEST STILL PINS CURRENT-DEFECT BEHAVIOR, marked ``PINS DEFECT``:
test_local_lost_update_reproduction asserts the final counter is 1 (one of two
increments LOST). When the g-115-8536 remedy lands, that assertion FLIPS —
that failure is the signal to rewrite it into the remedy's regression test,
not a regression itself.

The second pin already flipped (same day, 2026-09-01): LocalBackend.release_lock
gained the holder-checked release (pid:tid compare — the twin of own-cloud's
holder = :me), so test_local_release_is_holder_checked is now the REMEDY
REGRESSION test for the third-writer door. What remains open, deliberately:
the lost-update race itself (stale-seconds tuning / hold-refresh / write
fences), gated on measured [lock-stale-break] frequency — LocalBackend still
has conflict_error = () (no write-layer fence), so the breakable lock remains
the ONLY protection a local read-modify-write has.
"""
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage_backend import LocalBackend  # noqa: E402

TOKEN = "[lock-stale-break]"


# --- LocalBackend: breaker-side diagnostic ---------------------------------

def test_local_stale_break_prints_diagnostic_and_acquires(tmp_path, capsys):
    """POSITIVE CONTROL for the instrumentation: a stale lock is broken, the
    diagnostic names ages/pids, and acquisition then succeeds."""
    lock = tmp_path / "t.lock"
    lock.write_text("99999")                       # a dead "holder" pid
    old = time.time() - 60
    os.utime(lock, (old, old))

    LocalBackend().acquire_lock(lock, timeout=5, stale_seconds=1)

    err = capsys.readouterr().err
    assert TOKEN in err
    assert "holder_pid=99999" in err
    assert f"breaker_pid={os.getpid()}" in err
    assert lock.exists()                           # we hold it now


def test_local_fresh_lock_is_not_broken_and_stays_silent(tmp_path, capsys):
    """NEGATIVE CONTROL: a fresh lock is respected — no break, no diagnostic,
    TimeoutError as before. Proves the instrumentation is rare-branch-only."""
    lock = tmp_path / "t.lock"
    lock.write_text(str(os.getpid()))

    with pytest.raises(TimeoutError):
        LocalBackend().acquire_lock(lock, timeout=1, stale_seconds=60)
    assert TOKEN not in capsys.readouterr().err


# --- LocalBackend: the race itself ( step 2) ---------------------

def test_local_lost_update_reproduction(tmp_path, capsys):
    """PINS DEFECT — the lost update, reproduced deterministically.

    Two writers each increment a counter once under the lock. Writer A stalls
    inside the critical section past stale_seconds (simulated by backdating
    the lock mtime — same signal acquire_lock reads); writer B breaks the
    lock and completes a full RMW; A resumes and writes its stale result.
    Correct mutual exclusion gives 2. Today gives 1: B's increment vanishes
    with rc=0 everywhere. When the remedy lands this assertion flips."""
    backend = LocalBackend()
    counter = tmp_path / "counter.txt"
    counter.write_text("0")
    lock = tmp_path / "counter.lock"

    # A enters the critical section and reads.
    backend.acquire_lock(lock, timeout=2, stale_seconds=30)
    a_read = int(counter.read_text())
    # A stalls > stale_seconds (deterministic stand-in: backdate the mtime).
    old = time.time() - 60
    os.utime(lock, (old, old))

    # B arrives, sees a stale lock, BREAKS it, and does a complete RMW.
    backend.acquire_lock(lock, timeout=2, stale_seconds=10)
    counter.write_text(str(int(counter.read_text()) + 1))   # B: 0 -> 1
    backend.release_lock(lock)

    # A resumes, still believing it holds the lock, and writes its stale RMW.
    counter.write_text(str(a_read + 1))                     # A: 0 -> 1 (B lost)
    backend.release_lock(lock)

    assert int(counter.read_text()) == 1, (
        "two increments produced 2 — mutual exclusion held. The g-115-8536 "
        "remedy has landed: rewrite this pin into its regression test.")
    assert TOKEN in capsys.readouterr().err     # the break was observable


def test_local_release_is_holder_checked(tmp_path, capsys):
    """REMEDY REGRESSION (flipped 2026-09-01 from the PINS-DEFECT clobber
    test): release only unlinks a lock whose content is OUR pid:tid. A lock
    now held by someone else SURVIVES our release and the victim-side
    diagnostic fires — the LocalBackend twin of own-cloud's holder = :me.
    The foreign holder is simulated by rewriting the lock content (a real
    thief is another process/thread, which a single-thread test cannot be —
    same recipe as the moto test's machine_id A/B)."""
    backend = LocalBackend()
    lock = tmp_path / "t.lock"

    backend.acquire_lock(lock, timeout=2, stale_seconds=30)     # we hold
    lock.write_text("424242:424242")    # a thief stale-broke us + now holds

    backend.release_lock(lock)          # victim's release must NOT clobber
    assert lock.exists(), "victim's release deleted the thief's lock"
    err = capsys.readouterr().err
    assert TOKEN in err
    assert "victim=self" in err
    assert "424242:424242" in err       # diagnostic names the actual holder

    # A normal self-release still removes our own lock.
    lock.unlink()
    backend.acquire_lock(lock, timeout=2, stale_seconds=30)
    backend.release_lock(lock)
    assert not lock.exists()

    # Empty content (holder crashed between create and write) is
    # unattributable and keeps the legacy cleanup behavior.
    lock.write_text("")
    backend.release_lock(lock)
    assert not lock.exists()


# --- OwnCloudBackend: victim-side diagnostic (moto — no real AWS) ----------

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
        ddb.create_table(
            TableName=LOCKS,
            KeySchema=[{"AttributeName": "lock_key", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "lock_key", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST")
        ddb.create_table(
            TableName=SESSIONS,
            KeySchema=[{"AttributeName": "session_key", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "session_key", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST")
        yield {"s3": s3, "ddb": ddb, "root": tmp_path}


def _backend(cloud, machine_id="m1"):
    from owncloud_backend import OwnCloudBackend
    return OwnCloudBackend(
        env_id=ENV_ID, bucket=BUCKET, lock_table=LOCKS,
        sessions_table=SESSIONS, cache_root=cloud["root"],
        machine_id=machine_id, region=REGION,
        s3=cloud["s3"], ddb=cloud["ddb"])


def test_owncloud_victim_line_fires_on_stale_broken_release(cloud, capsys):
    """The full steal sequence against moto's real ConditionExpression
    semantics: A acquires, its ttl expires, B steals, A releases -> the
    conditional delete fails (holder is now B) and the VICTIM diagnostic
    fires. Also proves B's lock SURVIVES A's release — the conditional
    release is exactly the guard LocalBackend.release_lock lacks."""
    a = _backend(cloud, machine_id="A")
    other = _backend(cloud, machine_id="B")
    lp = cloud["root"] / "world" / "r.lock"

    a.acquire_lock(lp)
    # Expire the lock's ttl into the past (no sleeping) — same recipe as
    # test_owncloud_backend.py::test_stale_lock_is_breakable.
    cloud["ddb"].update_item(
        TableName=LOCKS, Key={"lock_key": {"S": a._lock_key(lp)}},
        UpdateExpression="SET #t = :old",
        ExpressionAttributeNames={"#t": "ttl"},
        ExpressionAttributeValues={":old": {"N": str(int(time.time()) - 100)}})
    other.acquire_lock(lp, timeout=1)          # B steals (breaker side)

    a.release_lock(lp)                          # A is no longer the holder

    err = capsys.readouterr().err
    assert TOKEN in err
    assert "victim=self" in err
    assert "g-115-8536" in err
    # The thief's lock survived the victim's release (conditional delete).
    item = cloud["ddb"].get_item(
        TableName=LOCKS, Key={"lock_key": {"S": a._lock_key(lp)}})["Item"]
    assert item["holder"]["S"].startswith("B:")


def test_owncloud_holder_release_is_silent(cloud, capsys):
    """NEGATIVE CONTROL: a normal acquire/release round-trip emits nothing."""
    a = _backend(cloud, machine_id="A")
    lp = cloud["root"] / "world" / "r.lock"
    a.acquire_lock(lp)
    a.release_lock(lp)
    assert TOKEN not in capsys.readouterr().err
