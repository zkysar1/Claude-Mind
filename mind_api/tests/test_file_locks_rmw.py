"""file_locks.locked_rmw — the daemon's SSOT lock-held conflict-retry wrapper
(#38). A thin composition of `locked(path)` + `_rmw_with_conflict_retry`, so the
retry math is already covered by core/scripts/tests/test_fileops_conflict_retry.py.

These tests pin the COMPOSITION contract that the store/pipeline handlers rely on:
  - the file lock is acquired ONCE and held across ALL retries (the retry runs
    INSIDE the single `with locked()`), never re-acquired per attempt;
  - the lock is released even when the cycle ultimately raises;
  - conflict_error == () (LocalBackend) is a transparent single pass;
  - a non-conflict exception propagates without a retry.

Pure unit test — no daemon, no S3. A stub backend supplies conflict_error plus
no-op acquire/release that record calls, so the lock-lifecycle is observable.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]  # repo root
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Importing file_locks inserts core/scripts onto sys.path (its module header),
# which is what makes the bare `import _fileops` below resolve.
from mind_api.src import file_locks  # noqa: E402
import _fileops  # noqa: E402


class _Conflict(Exception):
    """Stand-in for the backend's optimistic-concurrency exception."""


class _StubBackend:
    def __init__(self, conflict_error):
        self.conflict_error = conflict_error
        self.acquired = []
        self.released = []

    def acquire_lock(self, lock_path, timeout=10, stale_seconds=30):
        self.acquired.append(str(lock_path))

    def release_lock(self, lock_path):
        self.released.append(str(lock_path))


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(_fileops, "_conflict_backoff", lambda *_: 0)


def _use_backend(monkeypatch, conflict_error):
    be = _StubBackend(conflict_error)
    # acquire_lock/release_lock AND _rmw_with_conflict_retry all resolve
    # get_backend() from the _fileops module namespace, so one patch covers
    # both the lock dance and the conflict check.
    monkeypatch.setattr(_fileops, "get_backend", lambda: be)
    return be


def test_single_pass_on_local_backend(monkeypatch, tmp_path):
    be = _use_backend(monkeypatch, ())  # LocalBackend conflict_error
    calls = []
    p = tmp_path / "x.jsonl"
    out = file_locks.locked_rmw(p, lambda: (calls.append(1), "ok")[1])
    assert out == "ok"
    assert len(calls) == 1
    # Lock taken once on the .lock SIBLING (suffix replaced, not appended).
    assert be.acquired == [str(p.with_suffix(".lock"))]
    assert be.released == [str(p.with_suffix(".lock"))]


def test_retries_then_succeeds_under_one_lock(monkeypatch, tmp_path):
    be = _use_backend(monkeypatch, _Conflict)
    calls = []
    p = tmp_path / "x.jsonl"

    def cycle():
        calls.append(1)
        if len(calls) == 1:           # fail once, succeed on re-run
            raise _Conflict("stale fence")
        return "ok"

    assert file_locks.locked_rmw(p, cycle) == "ok"
    assert len(calls) == 2
    # THE key property: the lock is held ONCE across both attempts — the retry
    # happens inside the single `with locked()`, not by re-locking each attempt.
    assert be.acquired == [str(p.with_suffix(".lock"))]
    assert be.released == [str(p.with_suffix(".lock"))]


def test_reraises_after_cap_and_releases(monkeypatch, tmp_path):
    be = _use_backend(monkeypatch, _Conflict)
    calls = []
    p = tmp_path / "x.jsonl"

    def cycle():
        calls.append(1)
        raise _Conflict("always stale")

    with pytest.raises(_Conflict):
        file_locks.locked_rmw(p, cycle)
    assert len(calls) == _fileops._CONFLICT_RETRY_CAP   # bounded
    # `locked`'s finally still releases on the propagating raise.
    assert be.acquired == [str(p.with_suffix(".lock"))]
    assert be.released == [str(p.with_suffix(".lock"))]


def test_non_conflict_propagates_without_retry(monkeypatch, tmp_path):
    be = _use_backend(monkeypatch, _Conflict)
    calls = []
    p = tmp_path / "x.jsonl"

    def cycle():
        calls.append(1)
        raise ValueError("unrelated")

    with pytest.raises(ValueError):
        file_locks.locked_rmw(p, cycle)
    assert len(calls) == 1                              # ValueError != _Conflict
    assert be.released == [str(p.with_suffix(".lock"))]


def test_returns_cycle_value_including_early_exit_objects(monkeypatch, tmp_path):
    # Handlers return a Response from inside the cycle for validation early-exits;
    # locked_rmw must pass ANY return value straight through (it only intercepts
    # conflict_error). Use a sentinel object to stand in for a Response.
    _use_backend(monkeypatch, _Conflict)
    sentinel = object()
    p = tmp_path / "x.jsonl"
    assert file_locks.locked_rmw(p, lambda: sentinel) is sentinel
