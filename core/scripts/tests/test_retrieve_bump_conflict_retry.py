""": _locked_bump_jsonl conflict-retry + graceful degrade.

The retrieval counter bump was a SINGLE-SHOT fenced write — on own-cloud, a
conflict on a hot shared store (world/reasoning-bank.jsonl) failed the ENTIRE
retrieval with a 409. The fix wraps the in-lock cycle in
_fileops._rmw_with_conflict_retry and, on retry exhaustion, returns the
last-read records un-bumped instead of raising (telemetry is subordinate to
retrieval availability).

Stub-backend pattern mirrors test_fileops_conflict_retry.py: the retry
primitive only touches get_backend().conflict_error, so a stub exercises the
retry/degrade branches without S3.

MODULE-IDENTITY NOTE: several sibling suites (test_fileops_snapshot_blacklist
_and_gzip.py, test_fileops_finding_fixes.py, test_atomic_write_fallback.py)
sandbox-reload _fileops via `del sys.modules["_fileops"]` + re-import. A
module-level `import _fileops` here would bind the COLLECTION-time object,
while retrieve._locked_bump_jsonl's function-level `from _fileops import ...`
resolves the CURRENT sys.modules entry at call time — so patches applied to a
stale reference are invisible to the code under test (observed: the two
patch-dependent tests below failed in the full suite, passed in isolation).
Resolve _fileops dynamically at patch time instead.
"""
import importlib
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]  # core/scripts
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import retrieve  # noqa: E402


def _fileops_mod():
    """The CURRENT _fileops module object (post any sibling-suite reload)."""
    return importlib.import_module("_fileops")


class _Conflict(Exception):
    """Stand-in for the backend's optimistic-concurrency exception."""


class _StubBackend:
    """Minimal backend for _locked_bump_jsonl: identity ensure_local/refresh,
    with an optional per-call refresh hook so a test can inject conflicts."""

    def __init__(self, conflict_error=_Conflict):
        self.conflict_error = conflict_error
        self.refresh_calls = 0
        self._held = set()

    def ensure_local(self, p):  # noqa: D401 — identity
        return p

    def refresh(self, p):
        self.refresh_calls += 1
        return p

    # _fileops.acquire_lock/release_lock route through the lock backend —
    # in-memory no-op locks suffice for a single-threaded unit test.
    def acquire_lock(self, lock_path, timeout=10, stale_seconds=30):
        self._held.add(str(lock_path))

    def release_lock(self, lock_path):
        self._held.discard(str(lock_path))

    def atomic_write(self, target, write_to_handle, *, max_retries=10):
        # Minimal WriteResult-compatible write: full-content text write.
        class _Result:
            fallback_used = False
            retry_count = 0
            error_msg = ""
        with open(target, "w", encoding="utf-8") as h:
            write_to_handle(h)
        return _Result()


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(_fileops_mod(), "_conflict_backoff", lambda *_: 0)


@pytest.fixture()
def store(tmp_path):
    p = tmp_path / "reasoning-bank.jsonl"
    rows = [
        {"id": "rb-1", "category": "infrastructure",
         "utilization": {"retrieval_count": 0}},
        {"id": "rb-2", "category": "other",
         "utilization": {"retrieval_count": 5}},
    ]
    p.write_text("".join(json.dumps(r) + "\n" for r in rows),
                 encoding="utf-8")
    return p


def _use_backend(monkeypatch, be):
    # _locked_bump_jsonl reads get_backend from retrieve's module namespace;
    # _rmw_with_conflict_retry reads it from _fileops. Patch both — resolving
    # _fileops fresh so the patch lands on the object retrieve will import.
    monkeypatch.setattr(retrieve, "get_backend", lambda: be)
    monkeypatch.setattr(_fileops_mod(), "get_backend", lambda: be)


def test_bump_succeeds_first_try(monkeypatch, store):
    be = _StubBackend()
    _use_backend(monkeypatch, be)
    out = retrieve._locked_bump_jsonl(
        store, lambda rec: rec["id"] == "rb-1")
    assert [r["id"] for r in out] == ["rb-1", "rb-2"]
    on_disk = [json.loads(l) for l in store.read_text().splitlines() if l]
    assert on_disk[0]["utilization"]["retrieval_count"] == 1
    assert on_disk[1]["utilization"]["retrieval_count"] == 5


def test_bump_retries_then_succeeds(monkeypatch, store):
    """A transient conflict (hot-store contention) is absorbed by the retry:
    the write lands on attempt 2 and the caller sees bumped records."""
    be = _StubBackend()
    _use_backend(monkeypatch, be)
    calls = {"n": 0}
    fo = _fileops_mod()
    real_write = fo._atomic_write_with_fallback

    def flaky_write(path, write_fn, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _Conflict("stale fence")
        return real_write(path, write_fn, **kw)

    # _locked_bump_jsonl binds the helper at call time via its function-level
    # import from _fileops — patch the CURRENT _fileops module's attribute.
    monkeypatch.setattr(fo, "_atomic_write_with_fallback", flaky_write)
    out = retrieve._locked_bump_jsonl(
        store, lambda rec: rec["id"] == "rb-1")
    assert calls["n"] == 2
    on_disk = [json.loads(l) for l in store.read_text().splitlines() if l]
    assert on_disk[0]["utilization"]["retrieval_count"] == 1
    # The retry re-read fresh, so the returned records reflect one bump only
    # (no double-apply — a rejected fenced PUT lands nothing).
    assert out[0]["utilization"]["retrieval_count"] == 1


def test_bump_degrades_on_exhaustion(monkeypatch, store, capsys):
    """A persistent conflict (per-object fence wedge, rb-2639/rb-3080 class)
    must NOT raise out of _locked_bump_jsonl — the retrieval proceeds with
    un-bumped records and the on-disk store is untouched."""
    be = _StubBackend()
    _use_backend(monkeypatch, be)

    def always_conflict(path, write_fn, **kw):
        raise _Conflict("wedged fence")

    monkeypatch.setattr(_fileops_mod(), "_atomic_write_with_fallback",
                        always_conflict)
    out = retrieve._locked_bump_jsonl(
        store, lambda rec: rec["id"] == "rb-1")
    # Records returned (retrieval availability preserved) …
    assert [r["id"] for r in out] == ["rb-1", "rb-2"]
    # … the store is untouched on disk …
    on_disk = [json.loads(l) for l in store.read_text().splitlines() if l]
    assert on_disk[0]["utilization"]["retrieval_count"] == 0
    # … and the degrade is announced on stderr, not silent.
    assert "counter-bump" in capsys.readouterr().err


def test_no_match_no_write(monkeypatch, store):
    be = _StubBackend()
    _use_backend(monkeypatch, be)
    before = store.read_text()
    out = retrieve._locked_bump_jsonl(store, lambda rec: False)
    assert len(out) == 2
    assert store.read_text() == before
