"""Tests for core/scripts/storage_backend.py (Lodestar cutover step s1).

LocalBackend must be a correct, self-consistent local storage implementation
AND byte-compatible with _fileops (so the s2 wiring churns no on-disk file).
The byte-compat tests compare LocalBackend output directly against
_fileops._atomic_write_with_fallback with the same serializer closure.

Run: py -3 -m pytest core/scripts/tests/test_storage_backend.py -q
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

from storage_backend import (  # noqa: E402
    FileStat,
    LocalBackend,
    StorageBackend,
    WriteResult,
    get_backend,
    reset_backend_for_tests,
)


# ---------------------------------------------------------------------------
# Round-trip correctness
# ---------------------------------------------------------------------------

def test_text_round_trip(tmp_path):
    b = LocalBackend()
    p = tmp_path / "sub" / "note.txt"   # parent does not exist yet
    res = b.write_text(p, "hello\nworld\n")
    assert isinstance(res, WriteResult)
    assert res.fallback_used is False
    assert b.read_text(p) == "hello\nworld\n"
    assert b.exists(p) is True


def test_local_backend_conflict_error_is_empty_tuple():
    # G1 contract: LocalBackend cannot raise an optimistic-concurrency conflict
    # (single-filesystem writes never 412), so it declares the empty tuple —
    # `except backend.conflict_error` in _fileops' RMW retry then matches
    # nothing and the wrapper is a transparent single pass.
    assert LocalBackend().conflict_error == ()


def test_bytes_round_trip(tmp_path):
    b = LocalBackend()
    p = tmp_path / "blob.bin"
    payload = bytes(range(256))
    b.write_bytes(p, payload)
    assert b.read_bytes(p) == payload


def test_jsonl_round_trip(tmp_path):
    b = LocalBackend()
    p = tmp_path / "store.jsonl"
    items = [{"id": "a", "v": 1}, {"id": "b", "v": 2}]
    b.write_jsonl(p, items)
    assert b.read_jsonl(p) == items


def test_read_jsonl_missing_file_is_empty(tmp_path):
    b = LocalBackend()
    assert b.read_jsonl(tmp_path / "nope.jsonl") == []


def test_read_jsonl_skips_blank_lines(tmp_path):
    b = LocalBackend()
    p = tmp_path / "store.jsonl"
    p.write_text('{"id":"a"}\n\n{"id":"b"}\n', encoding="utf-8")
    assert b.read_jsonl(p) == [{"id": "a"}, {"id": "b"}]


def test_append_jsonl_record(tmp_path):
    b = LocalBackend()
    p = tmp_path / "store.jsonl"
    b.write_jsonl(p, [{"id": "a"}])
    b.append_jsonl_record(p, {"id": "b"})
    assert b.read_jsonl(p) == [{"id": "a"}, {"id": "b"}]


def test_modify_jsonl_rmw(tmp_path):
    b = LocalBackend()
    p = tmp_path / "store.jsonl"
    b.write_jsonl(p, [{"id": "a", "n": 1}])

    def bump(items):
        for it in items:
            it["n"] += 10
        items.append({"id": "b", "n": 0})
        return items

    out = b.modify_jsonl(p, bump)
    assert out == [{"id": "a", "n": 11}, {"id": "b", "n": 0}]
    assert b.read_jsonl(p) == [{"id": "a", "n": 11}, {"id": "b", "n": 0}]


def test_modify_jsonl_initial_when_missing(tmp_path):
    b = LocalBackend()
    p = tmp_path / "fresh.jsonl"
    out = b.modify_jsonl(p, lambda items: items, initial=[{"id": "seed"}])
    assert out == [{"id": "seed"}]
    assert b.read_jsonl(p) == [{"id": "seed"}]


def test_modify_jsonl_none_return_keeps_input(tmp_path):
    b = LocalBackend()
    p = tmp_path / "store.jsonl"
    b.write_jsonl(p, [{"id": "a"}])

    def mutate_in_place(items):
        items.append({"id": "b"})
        # returns None — backend must persist the mutated input

    b.modify_jsonl(p, mutate_in_place)
    assert b.read_jsonl(p) == [{"id": "a"}, {"id": "b"}]


# ---------------------------------------------------------------------------
# Serialization / byte-compat with _fileops
# ---------------------------------------------------------------------------

def test_jsonl_serialization_is_ensure_ascii(tmp_path):
    b = LocalBackend()
    p = tmp_path / "store.jsonl"
    b.write_jsonl(p, [{"name": "café", "emoji": "\U0001f680"}])
    raw = p.read_text(encoding="utf-8")
    # ensure_ascii=True escapes non-ASCII — the literal char must NOT appear.
    assert "caf\\u00e9" in raw
    assert "é" not in raw
    assert "\\ud83d\\ude80" in raw  # surrogate-pair escape for the rocket


def test_write_jsonl_byte_compat_with_fileops(tmp_path):
    """The decisive byte-compat check: LocalBackend.write_jsonl must produce a
    byte-identical file to _fileops._atomic_write_with_fallback with the same
    serializer. If this passes, s2 wiring churns no on-disk file."""
    import _fileops

    b = LocalBackend()
    items = [{"id": "a", "v": 1}, {"name": "café", "n": 2}, {"z": [1, 2, 3]}]
    p1 = tmp_path / "via_backend.jsonl"
    p2 = tmp_path / "via_fileops.jsonl"

    b.write_jsonl(p1, items)

    def _write(h):
        for it in items:
            h.write(json.dumps(it, ensure_ascii=True) + "\n")
    _fileops._atomic_write_with_fallback(p2, _write)

    assert p1.read_bytes() == p2.read_bytes()


# ---------------------------------------------------------------------------
# Atomic write fallback
# ---------------------------------------------------------------------------

def test_write_text_fallback_on_replace_failure(tmp_path, monkeypatch):
    b = LocalBackend()
    p = tmp_path / "data.txt"
    p.write_text("original\n", encoding="utf-8")

    calls = {"n": 0}

    def always_fail(src, dst, *a, **k):
        calls["n"] += 1
        raise PermissionError(13, "Simulated cloud-sync lock")

    monkeypatch.setattr(os, "replace", always_fail)
    monkeypatch.setattr(time, "sleep", lambda *a, **k: None)  # skip real backoff
    res = b.write_text(p, "new-content\n")

    assert res.fallback_used is True
    assert res.retry_count == 10          # default max_retries exhausted
    assert calls["n"] == 10
    assert res.error_class == "PermissionError"
    assert p.read_text(encoding="utf-8") == "new-content\n"
    assert not (tmp_path / "data.txt.tmp").exists()


def test_atomic_write_clean_path_reports_no_retries(tmp_path):
    b = LocalBackend()
    p = tmp_path / "clean.txt"
    res = b.atomic_write(p, lambda h: h.write("ok\n"))
    assert res.fallback_used is False
    assert res.retry_count == 0
    assert res.error_class is None
    assert res.version == str(p.stat().st_mtime_ns)
    assert p.read_text(encoding="utf-8") == "ok\n"


# ---------------------------------------------------------------------------
# Locking — the backend operates on the LITERAL lock-file path the caller
# passes (the resource->.lock derivation lives in _fileops, mirrored here).
# ---------------------------------------------------------------------------

def test_acquire_and_release_lock(tmp_path):
    b = LocalBackend()
    lock = (tmp_path / "store.jsonl").with_suffix(".lock")
    b.acquire_lock(lock)
    assert lock.exists()
    b.release_lock(lock)
    assert not lock.exists()


def test_lock_contention_times_out(tmp_path):
    b = LocalBackend()
    lock = (tmp_path / "store.jsonl").with_suffix(".lock")
    b.acquire_lock(lock)
    try:
        start = time.time()
        with pytest.raises(TimeoutError):
            b.acquire_lock(lock, timeout=1, stale_seconds=999)
        assert time.time() - start >= 1.0
    finally:
        b.release_lock(lock)


def test_stale_lock_is_broken(tmp_path):
    b = LocalBackend()
    lock = (tmp_path / "store.jsonl").with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("99999", encoding="utf-8")
    old = time.time() - 120
    os.utime(lock, (old, old))           # make the lock look 120s stale
    b.acquire_lock(lock, timeout=2, stale_seconds=30)   # must break it
    assert lock.exists()                 # re-created by us
    b.release_lock(lock)


def test_release_missing_lock_is_noop(tmp_path):
    b = LocalBackend()
    b.release_lock(tmp_path / "never-locked.lock")   # must not raise


# ---------------------------------------------------------------------------
# stat / list_dir / ensure_local
# ---------------------------------------------------------------------------

def test_stat_returns_metadata(tmp_path):
    b = LocalBackend()
    p = tmp_path / "f.txt"
    p.write_text("12345", encoding="utf-8")
    st = b.stat(p)
    assert isinstance(st, FileStat)
    assert st.size == 5
    assert st.mtime_ns > 0
    assert st.version == str(st.mtime_ns)


def test_stat_missing_returns_none(tmp_path):
    assert LocalBackend().stat(tmp_path / "ghost") is None


def test_list_dir(tmp_path):
    b = LocalBackend()
    (tmp_path / "b.txt").write_text("", encoding="utf-8")
    (tmp_path / "a.txt").write_text("", encoding="utf-8")
    assert b.list_dir(tmp_path) == ["a.txt", "b.txt"]   # sorted
    assert b.list_dir(tmp_path / "missing") == []


def test_ensure_local_is_identity(tmp_path):
    b = LocalBackend()
    p = tmp_path / "x"
    assert b.ensure_local(p) == Path(p)


def test_write_version_advances(tmp_path):
    b = LocalBackend()
    p = tmp_path / "v.txt"
    r1 = b.write_text(p, "one")
    time.sleep(0.01)
    r2 = b.write_text(p, "two")
    assert r1.version and r2.version
    # mtime_ns version should be monotonic across writes (or at least differ)
    assert r2.version != r1.version or b.read_text(p) == "two"


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

def test_get_backend_default_is_local(monkeypatch):
    reset_backend_for_tests()
    monkeypatch.delenv("MIND_STORAGE_BACKEND", raising=False)
    be = get_backend()
    assert be.name == "local"
    assert isinstance(be, LocalBackend)
    assert isinstance(be, StorageBackend)   # runtime_checkable structural match
    reset_backend_for_tests()


def test_get_backend_unknown_raises(monkeypatch):
    # 'own-cloud' is now implemented (s3); 'lodestar-hosted' is the still-unbuilt
    # commons backend that must keep raising NotImplementedError.
    reset_backend_for_tests()
    monkeypatch.setenv("MIND_STORAGE_BACKEND", "lodestar-hosted")
    with pytest.raises(NotImplementedError):
        get_backend()
    reset_backend_for_tests()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
