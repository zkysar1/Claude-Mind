"""mind_api.src.history.snapshot — CAS-delta delegation contract (0/7).

The daemon snapshot writer previously carried its own plain shutil.copy2
body claiming byte-parity with _fileops.save_history; it drifted from the
CLI's 2026-05-22 CAS cutover and kept writing uncompressed, uncapped full
copies (13.9G in 4 days on cc-04; filled cc-05's root fs 2026-07-16).
These tests pin the fix (independently landed by alpha as g-115-2410 and
echo as g-115-2407, reconciled onto alpha's version): snapshot() delegates
to _fileops.save_history, so daemon and CLI share ONE writer and cannot
drift again.

Contract pinned here:
  1. A snapshot lands as a CAS manifest + gzip blob — NO legacy
     .history/<rel>/ copy dir.
  2. Identical content dedups: two manifests, one content-addressed blob.
  3. A manifest restores byte-exact content by NAME (the
     meta_backpressure._evolution_rollback contract: restore resolves a
     snapshot's basename).
  4. guard-600 / g-115-1043: a non-empty JSONL source with zero parseable
     records raises CorruptSourceError and writes nothing.
  5. Missing source (new file) is a no-op: returns None, writes nothing.

Direct module calls on a tmp tree — no daemon needed. The caller-holds-lock
contract is about concurrent writers, absent here.
"""
from __future__ import annotations

import gzip
from pathlib import Path

import pytest

# Importing the module also puts core/scripts on sys.path (its module top
# does the insert), so the _fileops/_history_store imports work afterwards.
from mind_api.src import history
import _fileops  # noqa: E402
import _history_store  # noqa: E402


def _mk_base(tmp_path: Path) -> Path:
    base = tmp_path / "world"
    base.mkdir()
    return base


def _manifests(base: Path, rel: str):
    d = base / ".history" / "snapshots" / rel
    return sorted(p for p in d.iterdir() if p.suffix == ".yaml") if d.exists() else []


def test_snapshot_writes_cas_manifest_and_blob_not_legacy(tmp_path):
    base = _mk_base(tmp_path)
    f = base / "aspirations.jsonl"
    f.write_text('{"id": "asp-001"}\n', encoding="utf-8")

    history.snapshot(f, base, "echo", summary="test write")

    manifests = _manifests(base, "aspirations.jsonl")
    assert len(manifests) == 1
    assert manifests[0].name.endswith("_echo.yaml")
    # The gzip blob holds the exact content:
    blobs = list((base / ".history" / "blobs").rglob("*.gz"))
    assert len(blobs) == 1
    assert gzip.decompress(blobs[0].read_bytes()) == f.read_bytes()
    # Legacy uncompressed copy dir must NOT be created (cutover pin):
    assert not (base / ".history" / "aspirations.jsonl").exists()


def test_identical_content_dedups_to_one_blob(tmp_path):
    base = _mk_base(tmp_path)
    f = base / "store.jsonl"
    f.write_text('{"id": 1}\n', encoding="utf-8")

    history.snapshot(f, base, "echo")
    history.snapshot(f, base, "echo")

    assert len(_manifests(base, "store.jsonl")) == 2
    blobs = list((base / ".history" / "blobs").rglob("*.gz"))
    assert len(blobs) == 1  # content-addressed: identical bytes, one blob


def test_manifest_restores_byte_exact(tmp_path):
    base = _mk_base(tmp_path)
    f = base / "store.jsonl"
    v1 = '{"id": 1, "v": "one"}\n'
    f.write_text(v1, encoding="utf-8")

    history.snapshot(f, base, "echo", summary="before v2")
    manifest = _manifests(base, "store.jsonl")[0]
    f.write_text('{"id": 1, "v": "two"}\n', encoding="utf-8")

    restored = _history_store.restore(f, manifest.name, base)
    assert restored == v1.encode("utf-8")


def test_corrupt_jsonl_raises_and_writes_nothing(tmp_path):
    base = _mk_base(tmp_path)
    f = base / "store.jsonl"
    f.write_bytes(b"\x00\x00 not json \x00\n")  # non-empty, zero parseable

    with pytest.raises(_fileops.CorruptSourceError):
        history.snapshot(f, base, "echo")

    assert not (base / ".history" / "snapshots").exists()


def test_missing_source_is_noop(tmp_path):
    base = _mk_base(tmp_path)
    result = history.snapshot(base / "new-file.jsonl", base, "echo")
    assert result is None
    assert not (base / ".history").exists()
