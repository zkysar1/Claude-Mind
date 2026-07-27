""" — structural prevention for aspirations.jsonl null-byte corruption.

Pins the three outcomes:
  1. save_history parse-validates JSONL sources; raises CorruptSourceError
     when source is non-empty but zero records parse (canonical OneDrive
     Files-On-Demand rehydration shape).
  2. _atomic_write_with_fallback parse-validates JSONL targets after write;
     restores from latest .history snapshot AND raises PostWriteValidationError
     on corruption.
  3. (iteration-close.sh canary tested separately — bash; not in this file.)

Run: py -3 core/scripts/tests/test_jsonl_corruption_prevention.py
"""
import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))


def with_sandbox(test_fn):
    """Spin up tmp WORLD_DIR + META_DIR sandboxes, reload _fileops against them."""
    def wrapped():
        sandbox = Path(tempfile.mkdtemp(prefix="jsonl_corruption_test_"))
        meta_sandbox = Path(tempfile.mkdtemp(prefix="jsonl_corruption_meta_"))
        prior_world = os.environ.get("MIND_WORLD")
        prior_meta = os.environ.get("MIND_META")
        try:
            os.environ["MIND_WORLD"] = str(sandbox)
            os.environ["MIND_META"] = str(meta_sandbox)
            for mod in list(sys.modules):
                if mod in ("_fileops", "_paths"):
                    del sys.modules[mod]
            import _fileops  # noqa: F401
            test_fn(sandbox, _fileops)
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)
            shutil.rmtree(meta_sandbox, ignore_errors=True)
            if prior_world is not None:
                os.environ["MIND_WORLD"] = prior_world
            else:
                os.environ.pop("MIND_WORLD", None)
            if prior_meta is not None:
                os.environ["MIND_META"] = prior_meta
            else:
                os.environ.pop("MIND_META", None)
    wrapped.__name__ = test_fn.__name__
    return wrapped


def assert_true(cond, msg=""):
    if not cond:
        raise AssertionError(f"FAIL: {msg}")


def assert_eq(actual, expected, msg=""):
    if actual != expected:
        raise AssertionError(f"{msg}: expected {expected!r}, got {actual!r}")


# ---------------------------------------------------------------------------
# Outcome 1 — save_history parse validation
# ---------------------------------------------------------------------------

@with_sandbox
def save_history_refuses_corrupt_jsonl_source(sandbox, _fileops):
    """save_history raises CorruptSourceError on a JSONL file of all null bytes."""
    target = sandbox / "aspirations.jsonl"
    # Canonical incident shape: 2952351 null bytes from OneDrive rehydration.
    target.write_bytes(b"\x00" * 1024)
    try:
        _fileops.save_history(str(target), str(sandbox), "test-agent",
                              summary="should refuse")
    except _fileops.CorruptSourceError as e:
        assert_true("0 records parsed" in str(e),
                    f"expected '0 records parsed' in error, got: {e}")
        return
    raise AssertionError("save_history should have raised CorruptSourceError")


def _cas_snapshot_records(sandbox, rel_name):
    """Return snapshot RECORD files for rel_name in the Stage-2 CAS-delta store.

    Stage 2 (2026-05-22, fix-ballooning-history) moved the authoritative
    snapshot store to <base_dir>/.history/{blobs,patches,snapshots}/ — one
    .yaml record per snapshot under snapshots/<rel-path>/, content addressed
    into blobs/. The legacy per-file gzip tree (.history/<rel-path>/<ts>.gz)
    is read-side fallback only and no longer written by default, so counting
    snapshots there reports 0 for writes that succeeded (g-115-2353 triage:
    the three allow-branch cases below failed on the legacy count while
    save_history demonstrably worked — snapshot present in snapshots/,
    blob in blobs/, legacy dir never created).
    """
    snap_dir = sandbox / ".history" / "snapshots" / rel_name
    if not snap_dir.exists():
        return []
    return [s for s in snap_dir.iterdir() if s.suffix == ".yaml"]


@with_sandbox
def save_history_allows_healthy_jsonl_source(sandbox, _fileops):
    """save_history snapshots a healthy JSONL file normally."""
    target = sandbox / "aspirations.jsonl"
    target.write_text(
        json.dumps({"id": "a", "v": 1}) + "\n" +
        json.dumps({"id": "b", "v": 2}) + "\n",
        encoding="utf-8",
    )
    _fileops.save_history(str(target), str(sandbox), "test-agent",
                          summary="healthy write")
    snaps = _cas_snapshot_records(sandbox, "aspirations.jsonl")
    assert_true(len(snaps) == 1,
                f"expected 1 snapshot, got {len(snaps)}: {snaps}")


@with_sandbox
def save_history_allows_legitimately_empty_jsonl(sandbox, _fileops):
    """save_history snapshots a 0-byte JSONL file normally (legitimate empty state)."""
    target = sandbox / "aspirations.jsonl"
    target.write_bytes(b"")
    _fileops.save_history(str(target), str(sandbox), "test-agent",
                          summary="empty write")
    snaps = _cas_snapshot_records(sandbox, "aspirations.jsonl")
    assert_true(len(snaps) == 1,
                f"expected 1 snapshot for empty file, got {len(snaps)}")


@with_sandbox
def save_history_skips_validation_for_non_jsonl(sandbox, _fileops):
    """save_history does NOT validate non-JSONL files (YAML has its own parser)."""
    target = sandbox / "team-state.yaml"
    # Even garbage content should be snapshotted — JSONL validation must not fire on .yaml
    target.write_bytes(b"\x00" * 256)
    _fileops.save_history(str(target), str(sandbox), "test-agent",
                          summary="non-jsonl write")
    snaps = _cas_snapshot_records(sandbox, "team-state.yaml")
    assert_true(len(snaps) == 1,
                f"expected 1 snapshot for .yaml file, got {len(snaps)}")


# ---------------------------------------------------------------------------
# Outcome 2 — _atomic_write_with_fallback post-write validation
# ---------------------------------------------------------------------------

@with_sandbox
def atomic_write_validates_jsonl_after_write_happy_path(sandbox, _fileops):
    """Post-write validation is a no-op on healthy writes."""
    target = sandbox / "data.jsonl"
    items = [{"id": "a", "v": 1}, {"id": "b", "v": 2}]

    def _write(h):
        for it in items:
            h.write(json.dumps(it) + "\n")

    _fileops._atomic_write_with_fallback(target, _write,
                                         fallback_counter_key="test_happy")
    # No exception raised; file readable.
    content = target.read_text(encoding="utf-8")
    parsed = [json.loads(line) for line in content.strip().split("\n")]
    assert_eq(parsed, items, "healthy write should round-trip")


@with_sandbox
def atomic_write_restores_from_history_on_post_write_corruption(sandbox, _fileops):
    """Post-write validation restores from .history when write yields null-byte file.

    Simulates the OneDrive rehydration race: the write closure writes valid
    content, but a hypothetical second-write later corrupts the file. We
    simulate by writing null bytes directly via the write callable.
    """
    target = sandbox / "aspirations.jsonl"
    # Seed a valid snapshot via save_history.
    target.write_text(json.dumps({"id": "good", "v": 1}) + "\n",
                      encoding="utf-8")
    _fileops.save_history(str(target), str(sandbox), "test-agent",
                          summary="seed valid snapshot")

    # Now perform a write that produces null bytes (simulating the corruption).
    def _write_corrupt(h):
        # Write to the handle but bypass JSON encoding — produces 0-parseable file.
        h.write("\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00")

    try:
        _fileops._atomic_write_with_fallback(target, _write_corrupt,
                                             fallback_counter_key="test_corrupt")
    except _fileops.PostWriteValidationError as e:
        # File should be restored from snapshot.
        restored = target.read_text(encoding="utf-8").strip()
        rec = json.loads(restored)
        assert_eq(rec.get("id"), "good", "target should be restored from snapshot")
        # Corrupt sidecar should be preserved.
        corrupt_sidecar = Path(str(target) + ".post-write-corrupt")
        assert_true(corrupt_sidecar.exists(),
                    ".post-write-corrupt sidecar should exist for forensics")
        assert_true("Restored from" in str(e),
                    f"error should mention restore: {e}")
        return
    raise AssertionError("expected PostWriteValidationError on corrupt write")


@with_sandbox
def atomic_write_validates_only_jsonl_targets(sandbox, _fileops):
    """Post-write validation skips non-JSONL paths (YAML / JSON / Markdown unaffected)."""
    target = sandbox / "team-state.yaml"

    def _write(h):
        h.write("agent_status: {}\n")

    _fileops._atomic_write_with_fallback(target, _write,
                                         fallback_counter_key="test_yaml")
    # No exception; file written as-is.
    assert_eq(target.read_text(encoding="utf-8"), "agent_status: {}\n",
              "YAML write should pass through unvalidated")


@with_sandbox
def atomic_write_raises_when_no_history_to_restore_from(sandbox, _fileops):
    """PostWriteValidationError fires WITHOUT restore when no snapshot exists."""
    target = sandbox / "fresh-aspirations.jsonl"
    # No prior snapshot in .history/ for this path.

    def _write_corrupt(h):
        h.write("\x00\x00\x00\x00\x00")

    try:
        _fileops._atomic_write_with_fallback(target, _write_corrupt,
                                             fallback_counter_key="test_no_history")
    except _fileops.PostWriteValidationError as e:
        assert_true("no .history snapshot exists" in str(e),
                    f"error should mention missing history: {e}")
        return
    raise AssertionError("expected PostWriteValidationError when no snapshot exists")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    save_history_refuses_corrupt_jsonl_source,
    save_history_allows_healthy_jsonl_source,
    save_history_allows_legitimately_empty_jsonl,
    save_history_skips_validation_for_non_jsonl,
    atomic_write_validates_jsonl_after_write_happy_path,
    atomic_write_restores_from_history_on_post_write_corruption,
    atomic_write_validates_only_jsonl_targets,
    atomic_write_raises_when_no_history_to_restore_from,
]


def main():
    failures = 0
    for t in TESTS:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception:
            failures += 1
            print(f"FAIL  {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(TESTS) - failures}/{len(TESTS)} passed")
    sys.exit(0 if failures == 0 else 1)


if __name__ == "__main__":
    main()
