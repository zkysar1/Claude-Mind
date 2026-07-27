"""Tests for the archive-before-delete history-store vacuum (-b).

Pytest-collectable (def test_*(tmp_path)) so it runs in the daemon-safe full
suite (STORAGE_BACKEND=local python3 -m pytest core/scripts/tests). Covers the
correctness core where the single-copy-store data-loss risk lives:

- enumerate_vacuum_targets is read-only (no mutation).
- enumerate + delete frees the SAME set as vacuum(apply=True)  [orphan sweep].
- enumerate + delete matches vacuum(apply=True) with retention-drop.
- archive-before-delete ordering: orphan archived+verified BEFORE unlink;
  reachable blob SURVIVES (positive control); archived bytes == orphan bytes.
- archive failure  -> abort, NOTHING deleted.
- verify  failure  -> abort, NOTHING deleted (and no receipt).
- corrupt manifest -> abort, NOTHING deleted (Phase-2a invariant preserved).
- apply=false (SAFE landing) -> dry_run, no archive, no delete.
- receipt content: enumeration counts/bytes + the "do NOT restore into live
  .history" warning.
- tick cadence gate (fresh stamp no-ops) + per-box lock (concurrent no-op).
"""

import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import _history_store as hs  # noqa: E402
import history_vacuum_archive as hva  # noqa: E402

import pathlib
# guard-580: resolve bash explicitly — a bare 'bash' argv[0] hits System32 WSL on win32.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _bash_helpers import BASH  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _store(tmp_path, name="world"):
    base = tmp_path / name
    base.mkdir()
    return base


def _blobs(base):
    return sorted(p.name for p in (base / ".history" / "blobs").rglob("*.gz"))


def _ctx():
    return {"box_id": "testbox", "run_id": "run1",
            "created_at": "2026-07-20T00:00:00"}


def _orphan_one_blob(base):
    """Save two distinct contents, orphan the second by removing its manifest.
    Returns (kept_blob_names, orphan_blob_path, orphan_bytes)."""
    hs.save(base / "keep.txt", b"KEEP-reachable-content\n", base, agent="alpha")
    kept = _blobs(base)
    m2 = hs.save(base / "gone.txt", b"ORPHAN-unique-content\n", base, agent="alpha")
    Path(m2).unlink()  # manifest gone -> its blob is now unreachable
    t = hva.enumerate_vacuum_targets(base)
    op = Path(t["orphan_blobs"][0]["path"])
    return kept, op, op.read_bytes()


# --------------------------------------------------------------------------- #
# enumerate: read-only + finds the orphan, keeps the reachable
# --------------------------------------------------------------------------- #

def test_enumerate_is_read_only_and_finds_orphan(tmp_path):
    base = _store(tmp_path)
    kept, orphan_path, _ = _orphan_one_blob(base)
    before = _blobs(base)
    t = hva.enumerate_vacuum_targets(base)
    assert t["aborted"] is None
    # exactly one orphan; the other (reachable) blob is NOT enumerated
    assert len(t["orphan_blobs"]) == 1
    assert t["bytes_freed"] > 0
    assert len(kept) == 1 and len(before) == 2  # keep blob + orphan blob
    # the enumerated orphan path is the one whose manifest we removed
    assert Path(t["orphan_blobs"][0]["path"]) == orphan_path
    # enumerate mutated nothing (read-only) — both blobs still on disk
    assert _blobs(base) == before
    assert orphan_path.exists()


# --------------------------------------------------------------------------- #
# enumerate + delete == vacuum(apply=True)  (the consistency guarantee)
# --------------------------------------------------------------------------- #

def test_enumerate_delete_matches_vacuum_orphan_sweep(tmp_path):
    def build(base):
        hs.save(base / "a.txt", b"aaa-content\n", base, agent="alpha")
        m2 = hs.save(base / "b.txt", b"bbb-unique-content\n", base, agent="alpha")
        Path(m2).unlink()

    base1 = _store(tmp_path, "s1"); build(base1)
    base2 = _store(tmp_path, "s2"); build(base2)

    r_vac = hs.vacuum(base1, dry_run=False)
    t = hva.enumerate_vacuum_targets(base2)
    r_del = hs.delete_vacuum_targets(base2, t)

    assert r_vac["blobs_deleted"] == r_del["blobs_deleted"] == 1
    assert r_vac["patches_deleted"] == r_del["patches_deleted"]
    assert r_vac["bytes_freed"] == r_del["bytes_freed"]
    assert _blobs(base1) == _blobs(base2)


def test_enumerate_delete_matches_vacuum_retention_drop(tmp_path):
    def build(base):
        m = hs.save(base / "old.txt", b"old-unique-content\n", base, agent="alpha")
        old = time.time() - 40 * 86400  # age the manifest 40 days
        os.utime(Path(m), (old, old))
        hs.save(base / "fresh.txt", b"fresh-content\n", base, agent="alpha")

    base1 = _store(tmp_path, "s1"); build(base1)
    base2 = _store(tmp_path, "s2"); build(base2)

    r_vac = hs.vacuum(base1, dry_run=False, metadata_only_after_days=30)
    t = hva.enumerate_vacuum_targets(base2, metadata_only_after_days=30)
    r_del = hs.delete_vacuum_targets(base2, t)

    assert r_vac["manifests_dropped"] == r_del["manifests_dropped"] == 1
    assert r_vac["blobs_deleted"] == r_del["blobs_deleted"] == 1
    assert r_vac["bytes_freed"] == r_del["bytes_freed"]
    assert _blobs(base1) == _blobs(base2)


# --------------------------------------------------------------------------- #
# Archive-before-delete ordering + positive control
# --------------------------------------------------------------------------- #

def test_archive_before_delete_positive_control(tmp_path):
    base = _store(tmp_path)
    kept, orphan_path, orphan_bytes = _orphan_one_blob(base)
    grave = tmp_path / "graveyard"

    res = hva.run(base, archiver=hva.LocalDirArchiver(grave), apply=True,
                  run_ctx=_ctx())

    assert res["status"] == "deleted"
    # orphan archived to the graveyard, bytes intact (restorable)
    archived = list(grave.rglob("blobs/**/*.gz"))
    assert len(archived) == 1
    assert archived[0].read_bytes() == orphan_bytes
    # orphan deleted locally; reachable blob SURVIVES (positive control)
    assert not orphan_path.exists()
    assert _blobs(base) == kept
    # receipt written with the archive
    assert (grave / "testbox" / "run1" / "receipt.json").exists()


def test_receipt_content(tmp_path):
    base = _store(tmp_path)
    _orphan_one_blob(base)
    grave = tmp_path / "graveyard"
    hva.run(base, archiver=hva.LocalDirArchiver(grave), apply=True, run_ctx=_ctx())

    import json
    receipt = json.loads((grave / "testbox" / "run1" / "receipt.json").read_text())
    enum = receipt["enumeration"]
    assert enum["total_objects"] == 1
    assert enum["total_bytes"] > 0
    assert len(enum["orphan_blobs"]) == 1
    assert "Do NOT restore into live" in receipt["restore_instructions"]


# --------------------------------------------------------------------------- #
# Fail-safe: archive/verify failure aborts WITHOUT deleting
# --------------------------------------------------------------------------- #

class _FailArchiver:
    def archive(self, items, ctx):
        return {"archived": [], "failed": [(items[0]["hash_id"], "boom")]}

    def verify(self, items, ctx):
        raise AssertionError("verify must not run after archive failed")

    def write_receipt(self, receipt, ctx):
        raise AssertionError("receipt must not be written after archive failed")


class _VerifyFailArchiver:
    def archive(self, items, ctx):
        return {"archived": [i["hash_id"] for i in items], "failed": []}

    def verify(self, items, ctx):
        return {"verified": [], "missing": [(items[0]["hash_id"], "gone")]}

    def write_receipt(self, receipt, ctx):
        raise AssertionError("receipt must not be written after verify failed")


def test_archive_fail_aborts_no_delete(tmp_path):
    base = _store(tmp_path)
    _, orphan_path, _ = _orphan_one_blob(base)
    before = _blobs(base)
    res = hva.run(base, archiver=_FailArchiver(), apply=True, run_ctx=_ctx())
    assert res["status"] == "aborted"
    assert res["reason"] == "archive_failed"
    assert _blobs(base) == before          # nothing deleted
    assert orphan_path.exists()


def test_verify_fail_aborts_no_delete(tmp_path):
    base = _store(tmp_path)
    _, orphan_path, _ = _orphan_one_blob(base)
    before = _blobs(base)
    res = hva.run(base, archiver=_VerifyFailArchiver(), apply=True, run_ctx=_ctx())
    assert res["status"] == "aborted"
    assert res["reason"] == "verify_failed"
    assert _blobs(base) == before          # nothing deleted
    assert orphan_path.exists()


# --------------------------------------------------------------------------- #
# Corrupt manifest -> abort (Phase-2a invariant preserved through the wrapper)
# --------------------------------------------------------------------------- #

def test_corrupt_manifest_aborts_no_delete(tmp_path):
    base = _store(tmp_path)
    m = hs.save(base / "a.txt", b"aaa-content\n", base, agent="alpha")
    m2 = hs.save(base / "gone.txt", b"ORPHAN-unique\n", base, agent="alpha")
    Path(m2).unlink()
    Path(m).write_text("encoding: bogus\nhash: deadbeef\nsize_bytes: 3\n")
    before = _blobs(base)
    res = hva.run(base, archiver=hva.LocalDirArchiver(tmp_path / "g"),
                  apply=True, run_ctx=_ctx())
    assert res["status"] == "aborted"
    assert res["reason"] == "corrupt_manifests_detected"
    assert _blobs(base) == before          # nothing deleted


# --------------------------------------------------------------------------- #
# SAFE landing: apply=false is dry_run — no archive, no delete
# --------------------------------------------------------------------------- #

def test_dry_run_no_archive_no_delete(tmp_path):
    base = _store(tmp_path)
    _, orphan_path, _ = _orphan_one_blob(base)
    before = _blobs(base)
    grave = tmp_path / "graveyard"
    res = hva.run(base, archiver=hva.LocalDirArchiver(grave), apply=False,
                  run_ctx=_ctx())
    assert res["status"] == "dry_run"
    assert res["orphan_blobs"] == 1
    assert _blobs(base) == before          # nothing deleted
    assert orphan_path.exists()
    assert not grave.exists() or not list(grave.rglob("*.gz"))  # nothing archived


# --------------------------------------------------------------------------- #
# Tick: cadence gate + per-box lock (shell, via subprocess, SYNC mode)
# --------------------------------------------------------------------------- #

def _tick_env():
    env = dict(os.environ)
    env["STORAGE_BACKEND"] = "local"
    env["HISTORY_VACUUM_TICK_SYNC"] = "1"
    return env


def _fresh_hist(tmp_path):
    base = _store(tmp_path)
    hist = base / ".history"
    for d in ("snapshots", "blobs", "patches"):
        (hist / d).mkdir(parents=True)
    return base, hist


def test_tick_cadence_gate(tmp_path):
    base, hist = _fresh_hist(tmp_path)
    tick = str(SCRIPT_DIR / "history-vacuum-tick.sh")
    stamp = hist / ".vacuum-last-run"

    r1 = subprocess.run([BASH, tick, str(base)], env=_tick_env(),
                        capture_output=True, text=True, timeout=180)
    assert r1.returncode == 0, r1.stderr
    assert stamp.exists(), f"stamp not created; stderr={r1.stderr}"
    assert not (hist / ".vacuum.lock.d").exists()  # lock released
    first = stamp.stat().st_mtime

    # Second run immediately: fresh stamp within 24h -> gated no-op.
    r2 = subprocess.run([BASH, tick, str(base)], env=_tick_env(),
                        capture_output=True, text=True, timeout=180)
    assert r2.returncode == 0, r2.stderr
    assert stamp.stat().st_mtime == first  # stamp NOT advanced (gated)


def test_tick_lock_blocks_concurrent(tmp_path):
    base, hist = _fresh_hist(tmp_path)
    tick = str(SCRIPT_DIR / "history-vacuum-tick.sh")
    lock = hist / ".vacuum.lock.d"
    lock.mkdir()  # simulate a vacuum already running

    r = subprocess.run([BASH, tick, str(base)], env=_tick_env(),
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr
    # Locked out -> no run, no stamp; the (fresh) lock is untouched.
    assert not (hist / ".vacuum-last-run").exists()
    assert lock.exists()


# --------------------------------------------------------------------------- #
# Legacy per-file-dir drain () — a SEPARATE store from the CAS vacuum.
# The frozen pre-2026-07-16 legacy per-file dirs have no manifest/blob/patch
# model, so they are enumerated + archived PATH-PRESERVINGLY, gated OFF by
# default (drain_legacy_dirs). These tests pin the single-copy-store safety
# invariants + the CAS-untouched guarantee.
# --------------------------------------------------------------------------- #

def _build_legacy(tmp_path):
    """A .history/ with BOTH the CAS store (snapshots/blobs/patches + a dotfile +
    a CAS payload) and frozen legacy per-file dirs. Returns (base, hist)."""
    base, hist = _fresh_hist(tmp_path)
    (hist / ".vacuum-last-run").write_text("2026-07-24")          # operational dotfile
    (hist / "blobs" / "ab").mkdir(parents=True)
    (hist / "blobs" / "ab" / "cas.gz").write_bytes(b"cas")        # CAS payload (must survive)
    (hist / "aspirations.jsonl").mkdir()
    (hist / "aspirations.jsonl" / "2026-07-10.snap").write_text("v1")
    (hist / "aspirations.jsonl" / "2026-07-11.snap").write_text("v2-longer")
    (hist / "knowledge" / "system").mkdir(parents=True)
    (hist / "knowledge" / "system" / "node.snap").write_text("k")
    return base, hist


def test_enumerate_legacy_read_only_skips_cas(tmp_path):
    base, hist = _build_legacy(tmp_path)
    tg = hs.enumerate_legacy_dir_targets(base)
    assert sorted(tg["entries"]) == ["aspirations.jsonl", "knowledge"]
    rels = sorted(x["rel_path"] for x in tg["legacy_files"])
    assert rels == ["aspirations.jsonl/2026-07-10.snap",
                    "aspirations.jsonl/2026-07-11.snap",
                    "knowledge/system/node.snap"]
    # No CAS payload / snapshots / patches / dotfile enumerated.
    assert not any("blobs" in r or "snapshots" in r or "patches" in r
                   or "vacuum" in r for r in rels)
    # Read-only: everything still present.
    assert (hist / "aspirations.jsonl" / "2026-07-10.snap").exists()
    assert (hist / "blobs" / "ab" / "cas.gz").exists()


def test_legacy_gkey_path_preserving(tmp_path):
    base, _ = _build_legacy(tmp_path)
    items = hva.normalize_legacy_items(hs.enumerate_legacy_dir_targets(base))
    assert sorted(i["gkey"] for i in items) == [
        "legacy/aspirations.jsonl/2026-07-10.snap",
        "legacy/aspirations.jsonl/2026-07-11.snap",
        "legacy/knowledge/system/node.snap"]
    assert all(i["kind"] == "legacy" for i in items)


def test_legacy_dry_run_no_delete(tmp_path):
    base, hist = _build_legacy(tmp_path)
    res = hva.run_legacy_drain(base, archiver=None, apply=False)
    assert res["status"] == "dry_run"
    assert res["legacy_files"] == 3
    assert (hist / "aspirations.jsonl" / "2026-07-10.snap").exists()  # nothing deleted


def test_legacy_positive_control(tmp_path):
    base, hist = _build_legacy(tmp_path)
    grave = tmp_path / "graveyard"
    res = hva.run_legacy_drain(base, archiver=hva.LocalDirArchiver(grave),
                               apply=True, run_ctx=_ctx())
    assert res["status"] == "deleted"
    assert res["deleted"]["files_deleted"] == 3
    # legacy gone + empty dirs pruned
    assert not (hist / "aspirations.jsonl").exists()
    assert not (hist / "knowledge").exists()
    # CAS store + dotfile UNTOUCHED
    assert (hist / "blobs" / "ab" / "cas.gz").exists()
    assert (hist / "snapshots").exists()
    assert (hist / ".vacuum-last-run").exists()
    # archived copy (path-preserving) + receipt present under the run_id
    arch_copy = (grave / "testbox" / "run1" / "legacy"
                 / "aspirations.jsonl" / "2026-07-10.snap")
    assert arch_copy.exists() and arch_copy.read_text() == "v1"
    assert (grave / "testbox" / "run1" / "receipt.json").exists()


def test_legacy_archive_fail_aborts_no_delete(tmp_path):
    base, hist = _build_legacy(tmp_path)
    res = hva.run_legacy_drain(base, archiver=_FailArchiver(), apply=True,
                               run_ctx=_ctx())
    assert res["status"] == "aborted" and res["reason"] == "archive_failed"
    assert (hist / "aspirations.jsonl" / "2026-07-10.snap").exists()  # single-copy safety
    assert (hist / "knowledge" / "system" / "node.snap").exists()


def test_legacy_verify_fail_aborts_no_delete(tmp_path):
    base, hist = _build_legacy(tmp_path)
    res = hva.run_legacy_drain(base, archiver=_VerifyFailArchiver(), apply=True,
                               run_ctx=_ctx())
    assert res["status"] == "aborted" and res["reason"] == "verify_failed"
    assert (hist / "aspirations.jsonl" / "2026-07-10.snap").exists()  # nothing deleted
    assert (hist / "knowledge" / "system" / "node.snap").exists()


def test_legacy_clean_when_no_legacy(tmp_path):
    base, _ = _fresh_hist(tmp_path)  # CAS dirs only, no legacy
    res = hva.run_legacy_drain(base, archiver=hva.LocalDirArchiver(tmp_path / "g"),
                               apply=True, run_ctx=_ctx())
    assert res["status"] == "clean"


def test_legacy_gate_key_present_in_config():
    # load_config surfaces the drain_legacy_dirs gate as a bool (schema presence).
    # Value is NOT asserted (it is env-specific + the deliberate flip changes it);
    # the OFF-by-default safety lives in the config file + the hv.get(...,False)
    # code default, and run_legacy_drain's dry_run/apply/abort tests above.
    cfg = hva.load_config()
    assert "drain_legacy_dirs" in cfg
    assert isinstance(cfg["drain_legacy_dirs"], bool)
