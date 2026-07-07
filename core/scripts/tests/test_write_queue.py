"""test_write_queue.py —  per-path FIFO write queue.

Pins the BRD P1 contract: same-path writers inside one process wait in
ARRIVAL ORDER ahead of the lock/CAS layer; different paths stay parallel;
overload fails loud with the DISTINCT backpressure errors (never a raw
"Could not acquire lock"); contention is measured (conflict_rate).

White-box notes: tests reset the private registry between cases and poll
`waiting_now` via metrics_snapshot() to sequence arrivals deterministically
(an Event-free way to know a competing thread is actually IN LINE before
launching the next).
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

import _write_queue as wq  # noqa: E402
from _write_queue import (  # noqa: E402
    WriteQueueFullError,
    WriteQueueTimeoutError,
    acquire_turn,
    metrics_snapshot,
    release_turn,
)


def _reset():
    with wq._REGISTRY_LOCK:
        wq._REGISTRY.clear()


def _waiting_now(path) -> int:
    key = wq._normalize(path)
    for row in metrics_snapshot()["top_paths"]:
        if row["path"] == key:
            return row["waiting_now"]
    return 0


def _poll_until(fn, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(0.01)
    return False


# --- FIFO order ---------------------------------------------------------------

def test_fifo_grant_order_matches_arrival_order():
    _reset()
    path = "C:/tmp/wq-test/order.jsonl"
    grants = []
    acquire_turn(path)  # main thread holds the turn

    def waiter(i):
        acquire_turn(path)
        grants.append(i)
        release_turn(path)

    threads = []
    for i in range(4):
        t = threading.Thread(target=waiter, args=(i,), daemon=True)
        t.start()
        threads.append(t)
        # Don't launch the next arrival until this one is verifiably IN LINE —
        # that makes "arrival order" deterministic.
        assert _poll_until(lambda n=i: _waiting_now(path) == n + 1), \
            f"waiter {i} never enqueued"
    release_turn(path)  # open the gate
    for t in threads:
        t.join(timeout=10)
    assert grants == [0, 1, 2, 3], grants


def test_different_paths_run_in_parallel():
    _reset()
    a_held = threading.Event()
    proceed = threading.Event()

    def hold_a():
        acquire_turn("C:/tmp/wq-test/a.jsonl")
        a_held.set()
        proceed.wait(timeout=5)
        release_turn("C:/tmp/wq-test/a.jsonl")

    t = threading.Thread(target=hold_a, daemon=True)
    t.start()
    assert a_held.wait(timeout=5)
    started = time.monotonic()
    acquire_turn("C:/tmp/wq-test/b.jsonl")   # must NOT wait behind a.jsonl
    elapsed = time.monotonic() - started
    release_turn("C:/tmp/wq-test/b.jsonl")
    proceed.set()
    t.join(timeout=5)
    assert elapsed < 0.5, f"b.jsonl waited {elapsed}s behind a.jsonl"


# --- backpressure ---------------------------------------------------------------

def test_queue_full_raises_distinct_error_immediately():
    _reset()
    path = "C:/tmp/wq-test/full.jsonl"
    acquire_turn(path)  # holder

    blockers = []
    def parked():
        acquire_turn(path, wait_timeout=10)
        release_turn(path)
    for i in range(3):
        t = threading.Thread(target=parked, daemon=True)
        t.start()
        blockers.append(t)
        assert _poll_until(lambda n=i: _waiting_now(path) == n + 1)

    started = time.monotonic()
    try:
        acquire_turn(path, max_depth=3)
        raise AssertionError("expected WriteQueueFullError")
    except WriteQueueFullError as e:
        assert "write-queue full" in str(e)
        assert "Could not acquire lock" not in str(e)
    assert time.monotonic() - started < 1.0, "full-rejection must be immediate"
    release_turn(path)
    for t in blockers:
        t.join(timeout=10)


def test_wait_timeout_raises_distinct_error():
    _reset()
    path = "C:/tmp/wq-test/timeout.jsonl"
    acquire_turn(path)  # holder never releases within the waiter's budget
    try:
        # hold_stale must exceed the wait budget or the stale-escape would
        # steal the turn before the timeout fires.
        acquire_turn(path, wait_timeout=0.3, hold_stale=60)
        raise AssertionError("expected WriteQueueTimeoutError")
    except WriteQueueTimeoutError as e:
        assert "the write did NOT run" in str(e)
        assert "Could not acquire lock" not in str(e)
    finally:
        release_turn(path)
    snap = metrics_snapshot()
    assert snap["global"]["timeouts"] == 1


def test_stale_holder_turn_is_stolen():
    _reset()
    path = "C:/tmp/wq-test/stale.jsonl"
    acquire_turn(path)  # simulate a holder that died without release
    started = time.monotonic()
    acquire_turn(path, wait_timeout=10, hold_stale=0.3)  # steals after ~0.3s
    elapsed = time.monotonic() - started
    release_turn(path)
    assert elapsed < 5, f"stale escape took {elapsed}s"
    assert metrics_snapshot()["global"]["turns_stolen"] == 1


# --- _fileops integration --------------------------------------------------------

def test_backend_lock_failure_releases_turn():
    """If the cross-process lock times out (another PROCESS holds it), the
    queue turn must be released so the path doesn't wedge."""
    _reset()
    import _fileops
    with tempfile.TemporaryDirectory() as tmpd:
        lock_path = Path(tmpd) / "held.lock"
        lock_path.write_text("999999", encoding="utf-8")  # foreign fresh lock
        try:
            _fileops.acquire_lock(lock_path, timeout=1, stale_seconds=60)
            raise AssertionError("expected TimeoutError from backend")
        except TimeoutError as e:
            assert "Could not acquire lock" in str(e)  # genuine cross-process shape
        # Turn was released on failure: a fresh acquire waits in no line.
        started = time.monotonic()
        try:
            _fileops.acquire_lock(lock_path, timeout=1, stale_seconds=60)
            raise AssertionError("expected TimeoutError from backend")
        except TimeoutError:
            pass
        # Two full backend timeouts ≈ 2s; a wedged queue would add its own
        # wait on the second call.
        assert time.monotonic() - started < 3


def test_concurrent_writers_all_land_zero_raw_lock_failures():
    """ verification criterion: parallel writes to ONE path all land,
    in arrival order, with zero raw lock-acquisition failures surfaced."""
    _reset()
    import _fileops
    with tempfile.TemporaryDirectory() as tmpd:
        target = Path(tmpd) / "one-file.jsonl"
        errors = []
        order = []

        def writer(i):
            try:
                _fileops.locked_append_jsonl(target, {"writer": i})
                order.append(i)
            except Exception as e:  # noqa: BLE001 — the assertion surface
                errors.append((i, repr(e)))

        threads = []
        for i in range(8):
            t = threading.Thread(target=writer, args=(i,), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=30)

        assert not errors, errors
        import json
        lines = [json.loads(x) for x in
                 target.read_text(encoding="utf-8").splitlines()]
        assert sorted(r["writer"] for r in lines) == list(range(8))
        assert len(lines) == 8, "every parallel write landed exactly once"


# --- metrics ---------------------------------------------------------------------

def test_metrics_conflict_rate_and_shape():
    _reset()
    path = "C:/tmp/wq-test/metrics.jsonl"
    acquire_turn(path)
    release_turn(path)          # uncontended
    acquire_turn(path)

    done = threading.Event()
    def contender():
        acquire_turn(path)      # contended
        release_turn(path)
        done.set()
    t = threading.Thread(target=contender, daemon=True)
    t.start()
    assert _poll_until(lambda: _waiting_now(path) == 1)
    release_turn(path)
    assert done.wait(timeout=5)

    snap = metrics_snapshot()
    g = snap["global"]
    assert g["enqueued"] == 3 and g["contended"] == 1
    assert g["conflict_rate"] == round(1 / 3, 4)
    assert snap["bounds"]["max_queue_depth"] >= 1
    row = snap["top_paths"][0]
    assert row["path"] == wq._normalize(path)
    assert row["avg_wait_ms"] >= 0


def test_release_unknown_path_is_noop():
    _reset()
    release_turn("C:/tmp/wq-test/never-acquired.jsonl")  # must not raise


def test_path_normalization_unifies_spellings():
    _reset()
    with tempfile.TemporaryDirectory() as tmpd:
        p1 = Path(tmpd) / "Case.jsonl"
        p2 = str(p1).upper() if sys.platform == "win32" else str(p1)
        acquire_turn(p1)
        if sys.platform == "win32":
            # Same file, different case → same queue → distinct error fast.
            try:
                acquire_turn(p2, wait_timeout=0.2, hold_stale=60)
                raise AssertionError("expected same-queue timeout")
            except WriteQueueTimeoutError:
                pass
        release_turn(p1)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("ALL OK")
