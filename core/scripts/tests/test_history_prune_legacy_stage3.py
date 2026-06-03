#!/usr/bin/env python3
"""Stage 3 (2026-05-22): history prune-legacy must safely delete per-file
legacy gz subtrees once Stage 2's new store has taken over.

Eligibility (BOTH gates):
  - AGE GATE: every snapshot in the subtree older than --min-age-days
  - COVERAGE GATE: matching .history/snapshots/<rel>/ dir exists AND
    contains at least one new-store manifest

Also pins the Stage-2-companion fix: `cmd_prune` (the per-snapshot
retention pruner) must NOT recurse into `.history/snapshots/` and delete
new-store manifests.

Standalone runner — not pytest-collected. Invoke via:
    py -3 core/scripts/tests/test_history_prune_legacy_stage3.py
"""

import argparse
import gzip
import io
import os
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = THIS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))


TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


@contextmanager
def sandbox():
    """Per-test sandbox with dual-set MIND_*/MIND_* env vars."""
    world = Path(tempfile.mkdtemp(prefix="stage3_world_"))
    meta = Path(tempfile.mkdtemp(prefix="stage3_meta_"))
    tracked = ("MIND_WORLD", "MIND_META", "MIND_WORLD", "MIND_META")
    prior = {k: os.environ.get(k) for k in tracked}
    os.environ["MIND_WORLD"] = str(world)
    os.environ["MIND_META"] = str(meta)
    os.environ["MIND_WORLD"] = str(world)
    os.environ["MIND_META"] = str(meta)
    for mod in list(sys.modules):
        if mod in ("_fileops", "_paths", "_history_store", "history"):
            del sys.modules[mod]
    try:
        yield world
    finally:
        shutil.rmtree(world, ignore_errors=True)
        shutil.rmtree(meta, ignore_errors=True)
        for var, val in prior.items():
            if val is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = val


def _plant_legacy_snap(world, rel, ts_str, agent, content_bytes,
                       summary=None, mtime_age_days=None):
    """Plant a legacy gz snapshot at <world>/.history/<rel>/<ts>_<agent>.<ext>.gz.

    Optionally backdate mtime to simulate aged snapshots (mtime is the
    age signal, not the embedded timestamp).
    """
    snap_dir = world / ".history" / rel
    snap_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(rel).suffix
    name = f"{ts_str}_{agent}{ext}.gz"
    path = snap_dir / name
    with gzip.open(path, "wb") as f:
        f.write(content_bytes)
    if summary:
        meta_path = path.with_suffix(path.suffix + ".meta")
        meta_path.write_text(summary, encoding="utf-8")
    if mtime_age_days is not None:
        target_mtime = time.time() - mtime_age_days * 86400
        os.utime(path, (target_mtime, target_mtime))
        if summary:
            os.utime(meta_path, (target_mtime, target_mtime))
    return path


def _plant_new_store(world, rel, agent, content_bytes):
    """Save via _history_store.save into the CAS-delta tree."""
    import _history_store
    full = world / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(content_bytes)
    return _history_store.save(full, content_bytes, str(world), agent)


@contextmanager
def _capture():
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        yield out, err


def _run(cmd_argv):
    import history
    saved = sys.argv
    try:
        sys.argv = ["history.py"] + cmd_argv
        history.main()
    finally:
        sys.argv = saved


# ---------------------------------------------------------------------------
# Eligibility tests
# ---------------------------------------------------------------------------

@test
def deletes_old_subtree_with_new_store_coverage():
    """All snapshots aged >30d AND new-store has manifest → deleted."""
    with sandbox() as world:
        leg_dir = world / ".history" / "data.txt"
        _plant_legacy_snap(world, "data.txt", "2026-04-01T09-00-00",
                           "alpha", b"old", mtime_age_days=45)
        _plant_legacy_snap(world, "data.txt", "2026-04-02T09-00-00",
                           "alpha", b"old2", mtime_age_days=44)
        _plant_new_store(world, "data.txt", "beta", b"new content")

        assert leg_dir.exists()
        with _capture() as (out, _):
            _run(["prune-legacy", "--apply"])
        assert not leg_dir.exists(), (
            f"legacy dir should be deleted but still exists: {leg_dir}"
        )
        assert "Deleted 1 legacy subtree" in out.getvalue(), out.getvalue()


@test
def skips_old_subtree_without_new_store_coverage():
    """Old snapshots but NO new-store manifest → skip (would orphan history)."""
    with sandbox() as world:
        leg_dir = world / ".history" / "data.txt"
        _plant_legacy_snap(world, "data.txt", "2026-04-01T09-00-00",
                           "alpha", b"old", mtime_age_days=45)
        # No new-store save → no coverage

        with _capture() as (out, _):
            _run(["prune-legacy", "--apply"])
        assert leg_dir.exists(), "no-coverage subtree must be preserved"
        assert "no-coverage" in out.getvalue(), out.getvalue()


@test
def skips_subtree_with_recent_snapshot():
    """Any snapshot newer than min_age_days → skip the whole subtree."""
    with sandbox() as world:
        leg_dir = world / ".history" / "data.txt"
        _plant_legacy_snap(world, "data.txt", "2026-04-01T09-00-00",
                           "alpha", b"old", mtime_age_days=45)
        _plant_legacy_snap(world, "data.txt", "2026-05-21T09-00-00",
                           "alpha", b"recent", mtime_age_days=1)
        _plant_new_store(world, "data.txt", "beta", b"new content")

        with _capture() as (out, _):
            _run(["prune-legacy", "--apply"])
        assert leg_dir.exists(), "subtree with recent snapshot must be preserved"
        assert "Skipped 1 recent" in out.getvalue(), out.getvalue()


@test
def skips_when_new_store_dir_exists_but_empty():
    """Coverage gate requires AT LEAST ONE new-store manifest, not just dir."""
    with sandbox() as world:
        leg_dir = world / ".history" / "data.txt"
        _plant_legacy_snap(world, "data.txt", "2026-04-01T09-00-00",
                           "alpha", b"old", mtime_age_days=45)
        # Create empty new-store dir
        (world / ".history" / "snapshots" / "data.txt").mkdir(parents=True)

        with _capture() as (out, _):
            _run(["prune-legacy", "--apply"])
        assert leg_dir.exists(), "empty new-store dir is NOT coverage"
        assert "no-coverage" in out.getvalue()


@test
def custom_min_age_days_changes_eligibility():
    """--min-age-days=10 admits 15-day-old snapshots that 30 would skip."""
    with sandbox() as world:
        leg_dir = world / ".history" / "data.txt"
        _plant_legacy_snap(world, "data.txt", "2026-05-07T09-00-00",
                           "alpha", b"old", mtime_age_days=15)
        _plant_new_store(world, "data.txt", "beta", b"new content")

        # Default 30 days → skipped (15 < 30)
        with _capture() as (out, _):
            _run(["prune-legacy", "--apply"])
        assert leg_dir.exists(), "default 30-day threshold should preserve 15-day snap"

        # min-age-days=10 → 15 > 10 → deleted
        with _capture() as (out, _):
            _run(["prune-legacy", "--min-age-days", "10", "--apply"])
        assert not leg_dir.exists()


# ---------------------------------------------------------------------------
# Dry-run + side-effect tests
# ---------------------------------------------------------------------------

@test
def dry_run_does_not_delete():
    """Default mode is dry-run; --apply is required to actually delete."""
    with sandbox() as world:
        leg_dir = world / ".history" / "data.txt"
        _plant_legacy_snap(world, "data.txt", "2026-04-01T09-00-00",
                           "alpha", b"old", mtime_age_days=45)
        _plant_new_store(world, "data.txt", "beta", b"new")

        with _capture() as (out, _):
            _run(["prune-legacy"])  # no --apply
        assert leg_dir.exists(), "dry-run must not delete"
        assert "Would delete" in out.getvalue()


@test
def deletes_meta_sidecars_alongside():
    """rmtree deletes .meta sidecars too — pinned via byte accounting."""
    with sandbox() as world:
        leg_dir = world / ".history" / "data.txt"
        _plant_legacy_snap(world, "data.txt", "2026-04-01T09-00-00",
                           "alpha", b"old" * 100,
                           summary="written by alpha",
                           mtime_age_days=45)
        _plant_new_store(world, "data.txt", "beta", b"new")

        meta_path = next(leg_dir.glob("*.meta"))
        assert meta_path.exists()

        with _capture() as (out, _):
            _run(["prune-legacy", "--apply"])
        assert not meta_path.exists()
        assert not leg_dir.exists()


@test
def empty_husk_dir_cleaned_up():
    """An empty per-file dir (snapshots already removed by /prune) gets rmdir'd."""
    with sandbox() as world:
        husk = world / ".history" / "data.txt"
        husk.mkdir(parents=True)
        # No snapshots inside, no new-store either
        with _capture() as (out, _):
            _run(["prune-legacy", "--apply"])
        assert not husk.exists(), "empty husk should be rmdir'd"


# ---------------------------------------------------------------------------
# Safety: cmd_prune (Stage 2 companion fix) must skip new-store subtree
# ---------------------------------------------------------------------------

@test
def cmd_prune_does_not_touch_new_store():
    """Regression: pre-fix cmd_prune would rglob into snapshots/ and
    delete new-store .yaml manifests as if they were old gz snapshots."""
    with sandbox() as world:
        # Plant a new-store snapshot that's "old enough" to be tempting
        # to the existing weekly-retention pruner if it weren't skipped.
        manifest = _plant_new_store(world, "data.txt", "alpha", b"x")
        # Backdate the manifest mtime so the date-based prune logic would
        # have classified it as weekly-retention candidate.
        old_t = time.time() - 100 * 86400
        os.utime(manifest, (old_t, old_t))

        assert manifest.exists()
        with _capture() as (out, _):
            _run(["prune"])  # default dry-run inside the existing impl
        # Even if cmd_prune ran in apply mode, the skip should hold —
        # but it's safer to also assert it's NOT in the dry-run output.
        assert str(manifest) not in out.getvalue(), (
            f"cmd_prune leaked new-store manifest into output:\n{out.getvalue()}"
        )
        assert manifest.exists(), "new-store manifest must not be considered"


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------

@test
def idempotent_second_run_no_change():
    """After first run deletes eligible subtree, second run finds nothing to do."""
    with sandbox() as world:
        leg_dir = world / ".history" / "data.txt"
        _plant_legacy_snap(world, "data.txt", "2026-04-01T09-00-00",
                           "alpha", b"old", mtime_age_days=45)
        _plant_new_store(world, "data.txt", "beta", b"new")

        with _capture() as (out, _):
            _run(["prune-legacy", "--apply"])
        assert not leg_dir.exists()

        # Second run: nothing to delete
        with _capture() as (out, _):
            _run(["prune-legacy", "--apply"])
        text = out.getvalue()
        assert "Deleted 0 legacy subtrees" in text, text


# ---------------------------------------------------------------------------
# Multi-file
# ---------------------------------------------------------------------------

@test
def ignores_tmp_orphan_when_evaluating_age():
    """Fresh-eyes finding: an interrupted save leaves a .tmp file. Without
    filtering, its fresh mtime would falsely block subtree deletion forever."""
    with sandbox() as world:
        leg_dir = world / ".history" / "data.txt"
        _plant_legacy_snap(world, "data.txt", "2026-04-01T09-00-00",
                           "alpha", b"old", mtime_age_days=45)
        # Plant a fresh .tmp orphan in the same dir
        (leg_dir / "abandoned-write.txt.gz.tmp").write_bytes(b"junk")
        _plant_new_store(world, "data.txt", "beta", b"new")

        with _capture() as (out, _):
            _run(["prune-legacy", "--apply"])
        assert not leg_dir.exists(), (
            "subtree with only old gz + tmp orphan should be deleted, "
            f"got: {out.getvalue()}"
        )


@test
def deletes_only_eligible_subtrees_in_mixed_set():
    """Three files: one eligible, one recent-blocked, one no-coverage."""
    with sandbox() as world:
        # File A: eligible (old + new-store)
        _plant_legacy_snap(world, "a.txt", "2026-04-01T09-00-00",
                           "alpha", b"x", mtime_age_days=45)
        _plant_new_store(world, "a.txt", "beta", b"new-a")

        # File B: blocked by recent legacy snapshot
        _plant_legacy_snap(world, "b.txt", "2026-04-01T09-00-00",
                           "alpha", b"x", mtime_age_days=45)
        _plant_legacy_snap(world, "b.txt", "2026-05-21T09-00-00",
                           "alpha", b"y", mtime_age_days=1)
        _plant_new_store(world, "b.txt", "beta", b"new-b")

        # File C: old but no new-store coverage
        _plant_legacy_snap(world, "c.txt", "2026-04-01T09-00-00",
                           "alpha", b"x", mtime_age_days=45)

        with _capture() as (out, _):
            _run(["prune-legacy", "--apply"])
        assert not (world / ".history" / "a.txt").exists(), "A should be deleted"
        assert (world / ".history" / "b.txt").exists(), "B should remain (recent)"
        assert (world / ".history" / "c.txt").exists(), "C should remain (no coverage)"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="Run only tests matching substring")
    args = parser.parse_args()

    selected = [t for t in TESTS if not args.only or args.only in t.__name__]
    passes = 0
    fails = []
    for fn in selected:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
            passes += 1
        except Exception as e:
            print(f"FAIL  {fn.__name__}: {type(e).__name__}: {e}")
            fails.append(fn.__name__)
    print()
    print(f"{passes}/{len(selected)} passed")
    if fails:
        print(f"FAILURES: {fails}")
        sys.exit(1)


if __name__ == "__main__":
    main()
