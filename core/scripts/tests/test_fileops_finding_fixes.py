"""Regression tests for fix-ballooning-history Findings 1-4 (2026-05-22).

Finding 1: history.cmd_prune tolerates FileNotFoundError on snap.unlink
           (race with concurrent _prune_to_cap from save_history).
Finding 2: _fileops.read_jsonl_with_recovery retries with next-latest
           snapshot when the chosen one disappears mid-restore.
Finding 3: _fileops.save_history writes gzip snapshot atomically via
           <snapshot>.tmp + os.replace; a partial write leaves only the
           .tmp orphan, never a corrupt canonical snapshot.
Finding 4: _fileops._prune_to_cap calls _record_fallback_hit on OSError
           during unlink so chronic prune failure is observable instead
           of silent.

Standalone runner (not pytest-collected): py -3 <this-file>
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
    def wrapped():
        sandbox = Path(tempfile.mkdtemp(prefix="finding_fixes_world_"))
        meta_sandbox = Path(tempfile.mkdtemp(prefix="finding_fixes_meta_"))
        tracked = ("MIND_WORLD", "MIND_META",
                   "MIND_WORLD", "MIND_META",
                   "FILEOPS_HISTORY_KEEP_LEGACY_WRITES",
                   "FILEOPS_HISTORY_USE_NEW_STORE")
        prior = {k: os.environ.get(k) for k in tracked}
        try:
            os.environ["MIND_WORLD"] = str(sandbox)
            os.environ["MIND_META"] = str(meta_sandbox)
            # Dual-set MIND_*/MIND_* so the same test runs in Ayoai-Mind
            # and Zak-Data-Solutions-Mind without modification.
            os.environ["MIND_WORLD"] = str(sandbox)
            os.environ["MIND_META"] = str(meta_sandbox)
            # Findings 1-4 test the LEGACY gz tree behavior (per-file
            # cap, atomic gz write, find/restore from gz snapshots).
            # Stage 2 (2026-05-22) made the new CAS store the default;
            # to keep these tests focused on the legacy mechanism they
            # protect, force legacy-only mode: enable legacy writes,
            # disable the new-store write. _find_history_snapshots then
            # sees only the legacy snapshots these tests plant, so the
            # count assertions remain valid.
            os.environ["FILEOPS_HISTORY_KEEP_LEGACY_WRITES"] = "1"
            os.environ["FILEOPS_HISTORY_USE_NEW_STORE"] = "0"
            for mod in list(sys.modules):
                if mod in ("_fileops", "_paths", "history"):
                    del sys.modules[mod]
            import _fileops  # noqa: F401
            test_fn(sandbox, _fileops)
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)
            shutil.rmtree(meta_sandbox, ignore_errors=True)
            for var, val in prior.items():
                if val is not None:
                    os.environ[var] = val
                else:
                    os.environ.pop(var, None)
    wrapped.__name__ = test_fn.__name__
    return wrapped


def assert_true(cond, msg=""):
    if not cond:
        raise AssertionError(f"FAIL: {msg}")


def assert_eq(actual, expected, msg=""):
    if actual != expected:
        raise AssertionError(f"{msg}: expected {expected!r}, got {actual!r}")


# ---------------------------------------------------------------------------
# Finding 1 — cmd_prune tolerates missing snapshot
# ---------------------------------------------------------------------------

@with_sandbox
def cmd_prune_tolerates_missing_snapshot(sandbox, _fileops):
    """history.cmd_prune does NOT crash when an enumerated snapshot
    disappears between iterdir and unlink (race with auto-prune)."""
    import history
    from datetime import datetime, timedelta

    target = sandbox / "data.jsonl"
    target.write_text('{"v":1}\n', encoding="utf-8")
    hist_dir = sandbox / ".history" / "data.jsonl"
    hist_dir.mkdir(parents=True)

    # Seed 5 snapshots all >31 days old so weekly-prune logic engages
    # (it groups by ISO week and keeps the latest per week, dropping the
    # rest -- the "rest" go through the now-tolerant unlink path).
    old = datetime.now() - timedelta(days=45)
    for i in range(5):
        ts = (old + timedelta(hours=i)).strftime("%Y-%m-%dT%H-%M-%S")
        snap = hist_dir / f"{ts}_agent.jsonl.gz"
        snap.write_bytes(b"\x1f\x8b" + b"x" * 16)  # gzip-magic-prefixed stub

    # Delete one snapshot BEFORE cmd_prune runs (simulating concurrent removal
    # that happens between iterdir() and unlink()).
    snapshots = sorted(hist_dir.iterdir())
    snapshots[1].unlink()

    class Args:
        dry_run = False
    history.cmd_prune(Args())  # Must NOT raise FileNotFoundError


# ---------------------------------------------------------------------------
# Finding 2 — read_jsonl_with_recovery retries with next-latest
# ---------------------------------------------------------------------------

@with_sandbox
def read_jsonl_recovery_retries_when_chosen_snapshot_disappears(sandbox, _fileops):
    """When _restore_snapshot_to_target raises FileNotFoundError on the
    newest snapshot, read_jsonl_with_recovery falls back to the next."""
    target = sandbox / "data.jsonl"

    # Seed a valid snapshot (older), then a "phantom" newer snapshot we'll
    # delete just before recovery walks the list.
    target.write_text(json.dumps({"id": "good", "v": 1}) + "\n", encoding="utf-8")
    _fileops.save_history(target, sandbox, "agent-a", summary="older valid")

    # Wait 1 second so the new snapshot has a lexicographically-newer timestamp.
    import time
    time.sleep(1.1)
    target.write_text(json.dumps({"id": "phantom", "v": 2}) + "\n", encoding="utf-8")
    _fileops.save_history(target, sandbox, "agent-b", summary="will be deleted")

    snapshots = _fileops._find_history_snapshots(target)
    assert_true(len(snapshots) == 2, f"expected 2 snapshots, got {len(snapshots)}")
    newest = snapshots[0]
    assert_true("agent-b" in newest.name, f"newest should be agent-b's, got {newest.name}")

    # Simulate the race: delete the newest snapshot AFTER it's in our list.
    # When read_jsonl_with_recovery calls _restore_snapshot_to_target, it
    # raises FileNotFoundError on this candidate and falls back to the next.
    newest.unlink()

    # Now corrupt the target so recovery is triggered (severe corruption).
    target.write_bytes(b"\x00" * 256)

    items = _fileops.read_jsonl_with_recovery(target)
    assert_eq(len(items), 1, "recovery should restore the 1 record from older snapshot")
    assert_eq(items[0]["id"], "good", "recovered record should be from agent-a's snapshot")


@with_sandbox
def read_jsonl_recovery_reports_when_all_snapshots_disappear_pre_scan(sandbox, _fileops):
    """When no snapshots exist at scan time, recovery returns the parseable
    subset (empty here) and prints the no-history error. Covers the
    pre-existing 'snapshots is None' branch."""
    target = sandbox / "data.jsonl"
    target.write_text(json.dumps({"id": "x"}) + "\n", encoding="utf-8")
    _fileops.save_history(target, sandbox, "agent-x", summary="will vanish")

    snapshots = _fileops._find_history_snapshots(target)
    assert_true(len(snapshots) == 1, "expected 1 snapshot before sabotage")
    for s in snapshots:
        s.unlink()

    target.write_bytes(b"\x00" * 256)
    items = _fileops.read_jsonl_with_recovery(target)
    assert_eq(items, [], "no snapshots survived -- recovery returns empty parseable subset")


@with_sandbox
def read_jsonl_recovery_exhausts_all_candidates_mid_restore(sandbox, _fileops):
    """Covers Finding 2's NEW branch: snapshots survive _find_history_snapshots
    scan, but each one raises FileNotFoundError during _restore_snapshot_to_target
    (simulating an extreme prune storm). Recovery walks the entire list, then
    returns the parseable subset with the all-disappeared error."""
    target = sandbox / "data.jsonl"
    # Seed two valid snapshots so the candidate list has >1 entry.
    target.write_text(json.dumps({"id": "a"}) + "\n", encoding="utf-8")
    _fileops.save_history(target, sandbox, "agent-1", summary="snap-1")
    import time; time.sleep(1.1)
    target.write_text(json.dumps({"id": "b"}) + "\n", encoding="utf-8")
    _fileops.save_history(target, sandbox, "agent-2", summary="snap-2")

    # Monkey-patch _restore_snapshot_to_target to always raise
    # FileNotFoundError, simulating each candidate vanishing mid-restore.
    original = _fileops._restore_snapshot_to_target
    call_count = [0]
    def _always_missing(snapshot, target_path):
        call_count[0] += 1
        raise FileNotFoundError(f"simulated: {snapshot} gone mid-restore")
    _fileops._restore_snapshot_to_target = _always_missing
    try:
        target.write_bytes(b"\x00" * 256)  # severe corruption
        items = _fileops.read_jsonl_with_recovery(target)
    finally:
        _fileops._restore_snapshot_to_target = original

    assert_eq(items, [], "all candidates failed -- recovery returns parseable subset")
    assert_eq(call_count[0], 2,
              f"recovery should try ALL {2} candidates before giving up, got {call_count[0]}")


# ---------------------------------------------------------------------------
# Finding 3 — save_history atomic gzip write
# ---------------------------------------------------------------------------

@with_sandbox
def save_history_atomic_write_leaves_no_canonical_on_failure(sandbox, _fileops):
    """When gzip mid-stream write fails, the canonical snapshot path does
    NOT exist (only the .tmp orphan), so future _find_history_snapshots
    won't pick up a truncated gzip."""
    target = sandbox / "data.jsonl"
    target.write_text(json.dumps({"id": "x"}) + "\n", encoding="utf-8")

    # Monkey-patch shutil.copyfileobj to raise mid-stream.
    original_copy = _fileops.shutil.copyfileobj
    def _failing_copy(*args, **kwargs):
        raise OSError("simulated disk-full")
    _fileops.shutil.copyfileobj = _failing_copy
    try:
        try:
            _fileops.save_history(str(target), str(sandbox), "agent-a",
                                  summary="should fail mid-write")
        except OSError as e:
            assert_true("simulated" in str(e), f"expected simulated disk-full, got: {e}")
    finally:
        _fileops.shutil.copyfileobj = original_copy

    # The canonical snapshot must NOT exist; only the .tmp orphan may remain.
    history_dir = sandbox / ".history" / "data.jsonl"
    if history_dir.exists():
        names = [p.name for p in history_dir.iterdir()]
        canonical = [n for n in names if n.endswith(".gz") and not n.endswith(".tmp")]
        tmp_orphans = [n for n in names if n.endswith(".tmp")]
        assert_eq(canonical, [], f"canonical snapshot must NOT exist after failed write, got: {canonical}")
        # .tmp orphan presence is acceptable but not required (depends on
        # whether the gzip wrapper flushed anything before raising).
        assert_true(len(tmp_orphans) <= 1, f"at most 1 .tmp orphan, got: {tmp_orphans}")


@with_sandbox
def save_history_atomic_write_succeeds_normally(sandbox, _fileops):
    """Happy path: atomic write produces canonical snapshot AND no .tmp orphan."""
    target = sandbox / "data.jsonl"
    target.write_text(json.dumps({"id": "x"}) + "\n", encoding="utf-8")
    _fileops.save_history(str(target), str(sandbox), "agent-a", summary="healthy")

    history_dir = sandbox / ".history" / "data.jsonl"
    canonical = [p for p in history_dir.iterdir() if p.name.endswith(".gz") and not p.name.endswith(".tmp")]
    tmps = [p for p in history_dir.iterdir() if p.name.endswith(".tmp")]
    assert_eq(len(canonical), 1, f"exactly 1 canonical snapshot, got {len(canonical)}")
    assert_eq(len(tmps), 0, f"no .tmp orphan after successful write, got {len(tmps)}")


@with_sandbox
def find_history_snapshots_filters_tmp_orphans(sandbox, _fileops):
    """_find_history_snapshots ignores .tmp orphans so failed writes can't
    poison restore paths."""
    target = sandbox / "data.jsonl"
    target.write_text(json.dumps({"id": "x"}) + "\n", encoding="utf-8")
    _fileops.save_history(str(target), str(sandbox), "agent-a", summary="real")

    # Drop a .tmp orphan into the history dir (simulating a prior failed write).
    history_dir = sandbox / ".history" / "data.jsonl"
    orphan = history_dir / "2030-01-01T00-00-00_zzz.jsonl.gz.tmp"
    orphan.write_bytes(b"\x1f\x8b" + b"junk")

    snapshots = _fileops._find_history_snapshots(target)
    names = [s.name for s in snapshots]
    assert_true(all(not n.endswith(".tmp") for n in names),
                f"_find_history_snapshots must filter .tmp; got: {names}")


@with_sandbox
def prune_to_cap_filters_tmp_orphans(sandbox, _fileops):
    """_prune_to_cap ignores .tmp orphans (they stay as forensic evidence)."""
    target = sandbox / "data.jsonl"
    target.write_text("x", encoding="utf-8")
    history_dir = sandbox / ".history" / "data.jsonl"
    history_dir.mkdir(parents=True)

    # 3 canonical + 2 .tmp orphans, cap=2 -> prune drops 1 canonical, leaves both .tmp.
    from datetime import datetime, timedelta
    base = datetime.now() - timedelta(hours=10)
    for i in range(3):
        ts = (base + timedelta(hours=i)).strftime("%Y-%m-%dT%H-%M-%S")
        (history_dir / f"{ts}_agent.jsonl.gz").write_bytes(b"\x1f\x8b" + b"x")
    (history_dir / "2030-01-01T00-00-00_zzz.jsonl.gz.tmp").write_bytes(b"orphan-a")
    (history_dir / "2031-01-01T00-00-00_yyy.jsonl.gz.tmp").write_bytes(b"orphan-b")

    removed = _fileops._prune_to_cap(history_dir, cap=2)
    remaining = sorted(p.name for p in history_dir.iterdir())
    canonical = [n for n in remaining if not n.endswith(".tmp")]
    tmps = [n for n in remaining if n.endswith(".tmp")]
    assert_eq(removed, 1, f"expected 1 canonical removed, got {removed}")
    assert_eq(len(canonical), 2, f"expected 2 canonical remaining, got {len(canonical)}")
    assert_eq(len(tmps), 2, f"both .tmp orphans must remain (forensic), got {len(tmps)}")


# ---------------------------------------------------------------------------
# Finding 4 — _prune_to_cap logs OSError via _record_fallback_hit
# ---------------------------------------------------------------------------

@with_sandbox
def prune_to_cap_logs_unlink_failure_via_fallback_hit(sandbox, _fileops):
    """When snap.unlink raises OSError, _record_fallback_hit fires so the
    failure is observable in world/.fallback-stats.jsonl."""
    target = sandbox / "data.jsonl"
    target.write_text("x", encoding="utf-8")
    history_dir = sandbox / ".history" / "data.jsonl"
    history_dir.mkdir(parents=True)

    # 3 snapshots, cap=1 -> 2 to drop. Patch Path.unlink to raise.
    from datetime import datetime, timedelta
    base = datetime.now() - timedelta(hours=10)
    snaps = []
    for i in range(3):
        ts = (base + timedelta(hours=i)).strftime("%Y-%m-%dT%H-%M-%S")
        s = history_dir / f"{ts}_agent.jsonl.gz"
        s.write_bytes(b"\x1f\x8b" + b"x")
        snaps.append(s)

    # Capture _record_fallback_hit calls.
    captured = []
    original = _fileops._record_fallback_hit
    def _capture(counter_key, target_path, error_msg):
        captured.append((counter_key, target_path, error_msg))
    _fileops._record_fallback_hit = _capture

    # Patch Path.unlink to raise OSError for the snapshot path.
    import pathlib
    original_unlink = pathlib.Path.unlink
    def _failing_unlink(self, *args, **kwargs):
        if self.name.endswith(".gz"):
            raise PermissionError("simulated lock")
        return original_unlink(self, *args, **kwargs)
    pathlib.Path.unlink = _failing_unlink

    try:
        removed = _fileops._prune_to_cap(history_dir, cap=1)
    finally:
        pathlib.Path.unlink = original_unlink
        _fileops._record_fallback_hit = original

    assert_eq(removed, 0, "all unlink calls failed -- removed count must be 0")
    # Expect 2 capture entries (one per failed unlink).
    snap_skips = [c for c in captured if c[0] == "snapshot_prune_skip"]
    assert_eq(len(snap_skips), 2,
              f"expected 2 snapshot_prune_skip records, got {len(snap_skips)}: {captured}")
    for key, path, msg in snap_skips:
        assert_true("PermissionError" in msg, f"error message should name the exception: {msg}")
        assert_true("simulated lock" in msg, f"error message should include detail: {msg}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    cmd_prune_tolerates_missing_snapshot,
    read_jsonl_recovery_retries_when_chosen_snapshot_disappears,
    read_jsonl_recovery_reports_when_all_snapshots_disappear_pre_scan,
    read_jsonl_recovery_exhausts_all_candidates_mid_restore,
    save_history_atomic_write_leaves_no_canonical_on_failure,
    save_history_atomic_write_succeeds_normally,
    find_history_snapshots_filters_tmp_orphans,
    prune_to_cap_filters_tmp_orphans,
    prune_to_cap_logs_unlink_failure_via_fallback_hit,
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
