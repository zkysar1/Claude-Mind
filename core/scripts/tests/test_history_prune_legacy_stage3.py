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


# `test` is a case-registration decorator, not a pytest test. Without this guard,
# pytest's `test*` name rule collects it and errors on the missing `fn` fixture —
# and unlike conftest `collect_ignore`, __test__=False also holds under explicit-
# path invocation (`pytest <this-file>`), which overrides collect_ignore.
test.__test__ = False


@contextmanager
def sandbox():
    """Per-test sandbox setting MIND_WORLD/MIND_META."""
    world = Path(tempfile.mkdtemp(prefix="stage3_world_"))
    meta = Path(tempfile.mkdtemp(prefix="stage3_meta_"))
    tracked = ("MIND_WORLD", "MIND_META")
    prior = {k: os.environ.get(k) for k in tracked}
    try:
        os.environ["MIND_WORLD"] = str(world)
        os.environ["MIND_META"] = str(meta)
        for mod in list(sys.modules):
            if mod in ("_fileops", "_paths", "_history_store", "history"):
                del sys.modules[mod]
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
    """All snapshots aged >30d AND new-store coverage is complete → deleted.

    FIXTURE CORRECTED (g-115-5513): this test previously planted TWO legacy
    snapshots against ONE manifest and asserted deletion — i.e. it PINNED the
    count-blind coverage gate as correct behavior, which is the exact defect
    the goal was filed against. The intent ("aged out + covered ⇒ delete") is
    unchanged and still the thing under test; only the fixture moved, so that
    "covered" now means what the word claims. A second save supplies the
    second manifest.
    """
    with sandbox() as world:
        leg_dir = world / ".history" / "data.txt"
        _plant_legacy_snap(world, "data.txt", "2026-04-01T09-00-00",
                           "alpha", b"old", mtime_age_days=45)
        _plant_legacy_snap(world, "data.txt", "2026-04-02T09-00-00",
                           "alpha", b"old2", mtime_age_days=44)
        _plant_new_store(world, "data.txt", "beta", b"new content")
        _plant_new_store(world, "data.txt", "beta", b"new content 2")

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


# ---------------------------------------------------------------------------
# Shortfall gate () — coverage must INTERSECT the deletion set, not
# merely be PRESENT beside it.
#
# The original coverage gate asked "does .history/snapshots/<rel>/ hold AT LEAST
# ONE manifest". The new store writes one manifest PER VERSION, so that predicate
# answers "did this path migrate at all", never "did THESE snapshots migrate".
# It is rb-6344 exactly: presence of a backup is not intersection with the set
# about to be destroyed, and guard-1308 names the same step.
#
# Measured on the live world store 2026-08-09 (alpha, hostname cc-04): 14 legacy
# dirs held snapshots, 13 would PASS, and THREE of those had FEWER manifests than
# legacy snapshots — board/coordination.jsonl 502 vs 431, board/findings.jsonl
# 186 vs 62, board/general.jsonl 26 vs 21. Total 200 snapshots that the gate
# would have declared covered.
#
# WHY A COUNT AND NOT A SET: the two trees are keyed differently by construction
# (a legacy snapshot is <ts>_<agent><ext>.gz; a new-store manifest is
# <ts>_<agent>.yaml written at a DIFFERENT instant by the migration), so there is
# no honest identity join between them. A count comparison is the strongest
# predicate available that cannot pass while short, and it fails SAFE — it can
# only ever refuse a deletion the old gate would have allowed.
# ---------------------------------------------------------------------------

@test
def refuses_subtree_with_fewer_manifests_than_legacy_snapshots():
    """3 legacy snapshots vs 1 manifest → REFUSE. RED before .

    This is the exact live shape: board/findings.jsonl had 186 legacy snapshots
    against 62 manifests and passed the presence gate. Against the count-blind
    predicate this test fails, because the dir is deleted.
    """
    with sandbox() as world:
        leg_dir = world / ".history" / "data.txt"
        for i, day in enumerate((45, 44, 43)):
            _plant_legacy_snap(world, "data.txt", f"2026-04-0{i+1}T09-00-00",
                               "alpha", f"old{i}".encode(), mtime_age_days=day)
        # Exactly ONE manifest — the old gate's threshold, and 2 short.
        _plant_new_store(world, "data.txt", "beta", b"new content")

        with _capture() as (out, _):
            _run(["prune-legacy", "--apply"])
        assert leg_dir.exists(), (
            "subtree with FEWER manifests than legacy snapshots must be "
            f"preserved, but it was deleted: {leg_dir}"
        )
        assert "shortfall" in out.getvalue().lower(), (
            "the refusal must name the shortfall so a reader can act on it; "
            f"got: {out.getvalue()}"
        )


@test
def shortfall_dirs_are_reported_explicitly_in_dry_run():
    """A dry run must NAME each short dir with both counts, not pass silently.

    g-115-5513 verification outcome 2. Silent passing is what let 200 snapshots
    read as covered: the gate is real, well-commented and looks careful, so a
    reader has no prompt to check it.
    """
    with sandbox() as world:
        for i, day in enumerate((45, 44)):
            _plant_legacy_snap(world, "short.txt", f"2026-04-0{i+1}T09-00-00",
                               "alpha", f"s{i}".encode(), mtime_age_days=day)
        _plant_new_store(world, "short.txt", "beta", b"new short")

        with _capture() as (out, _):
            _run(["prune-legacy"])          # dry run, no --apply
        text = out.getvalue()
        assert "short.txt" in text, f"short dir not named in dry run: {text}"
        assert "2 legacy" in text and "1 manifest" in text, (
            f"dry run must show BOTH counts so the shortfall is checkable: {text}"
        )
        assert (world / ".history" / "short.txt").exists(), (
            "dry run must never delete"
        )


@test
def equal_counts_still_delete():
    """Coverage EQUAL to the deletion set stays eligible — the gate tightens
    only where it was actually blind, and does not become a blanket refusal."""
    with sandbox() as world:
        leg_dir = world / ".history" / "even.txt"
        _plant_legacy_snap(world, "even.txt", "2026-04-01T09-00-00",
                           "alpha", b"a", mtime_age_days=45)
        _plant_legacy_snap(world, "even.txt", "2026-04-02T09-00-00",
                           "alpha", b"b", mtime_age_days=44)
        # Two distinct saves → two manifests.
        _plant_new_store(world, "even.txt", "beta", b"new-1")
        _plant_new_store(world, "even.txt", "beta", b"new-2")

        with _capture() as (out, _):
            _run(["prune-legacy", "--apply"])
        assert not leg_dir.exists(), (
            f"equal coverage must remain eligible; output: {out.getvalue()}"
        )


@test
def surplus_manifests_still_delete():
    """MORE manifests than legacy snapshots is the healthy shape (nine of the
    live world dirs looked like this) and must stay eligible."""
    with sandbox() as world:
        leg_dir = world / ".history" / "surplus.txt"
        _plant_legacy_snap(world, "surplus.txt", "2026-04-01T09-00-00",
                           "alpha", b"a", mtime_age_days=45)
        for i in range(3):
            _plant_new_store(world, "surplus.txt", "beta", f"new-{i}".encode())

        with _capture() as (out, _):
            _run(["prune-legacy", "--apply"])
        assert not leg_dir.exists(), (
            f"surplus coverage must remain eligible; output: {out.getvalue()}"
        )


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
