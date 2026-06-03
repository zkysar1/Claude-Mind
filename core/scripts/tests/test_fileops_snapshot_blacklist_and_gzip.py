"""fix-ballooning-history-2026-05-22 — snapshot blacklist + gzip compression.

Pins the two outcomes of the history-storage hardening pass:
  1. _fileops.save_history skips snapshots for files matching
     _SNAPSHOT_BLACKLIST (changelog still fires; only the .history copy
     is dropped). Today's entries: world/presence/* and meta/gate-firings.jsonl.
  2. _fileops.save_history writes gzip-compressed snapshots with a `.gz`
     suffix. Roundtrip via gzip read returns the original bytes byte-for-byte.
     The companion sidecar lookup uses `<snap>.gz.meta`.
  3. history.parse_snapshot_name strips trailing `.gz` before parsing the
     timestamp/agent token so prune and list both work on gzipped snapshots.
  4. history._find_snapshot_by_name (Stage 2 successor to
     _resolve_version_path) accepts the user-supplied version name in three
     shapes: literal-on-disk, bare-without-.gz, and explicit-with-.gz. The
     Stage 2 helper takes (file_path, version_name) instead of (history_dir,
     version_name) so it can merge across both stores; the convenience
     suffix handling is preserved.

Run: py -3 core/scripts/tests/test_fileops_snapshot_blacklist_and_gzip.py
"""
import gzip
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
    """Spin up tmp WORLD_DIR + META_DIR sandboxes, reload _fileops against them.

    Enables FILEOPS_HISTORY_KEEP_LEGACY_WRITES=1 so the legacy gz tree
    receives writes during these tests. Items 1-4 of fix-ballooning-history
    (blacklist + gzip + per-file cap) test the LEGACY mechanism. Stage 2
    (2026-05-22) made legacy writes opt-in via the rollback hatch; this
    suite opts in so the items-1-4 regression coverage stays in place.
    """
    def wrapped():
        sandbox = Path(tempfile.mkdtemp(prefix="snap_bl_gz_world_"))
        meta_sandbox = Path(tempfile.mkdtemp(prefix="snap_bl_gz_meta_"))
        tracked = ("MIND_WORLD", "MIND_META",
                   "MIND_WORLD", "MIND_META",
                   "FILEOPS_HISTORY_KEEP_LEGACY_WRITES")
        prior = {k: os.environ.get(k) for k in tracked}
        try:
            os.environ["MIND_WORLD"] = str(sandbox)
            os.environ["MIND_META"] = str(meta_sandbox)
            # Dual-set MIND_*/MIND_* for cross-repo portability.
            os.environ["MIND_WORLD"] = str(sandbox)
            os.environ["MIND_META"] = str(meta_sandbox)
            os.environ["FILEOPS_HISTORY_KEEP_LEGACY_WRITES"] = "1"
            for mod in list(sys.modules):
                if mod in ("_fileops", "_paths"):
                    del sys.modules[mod]
            import _fileops  # noqa: F401
            test_fn(sandbox, meta_sandbox, _fileops)
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
# Outcome 1 — snapshot blacklist
# ---------------------------------------------------------------------------

@with_sandbox
def save_history_skips_blacklisted_presence_dir(sandbox, meta_sandbox, _fileops):
    """presence/<agent>.jsonl under WORLD must NOT create a .history snapshot."""
    presence_dir = sandbox / "presence"
    presence_dir.mkdir(parents=True)
    target = presence_dir / "zeta.jsonl"
    target.write_text(json.dumps({"agent": "zeta", "ts": "now"}) + "\n",
                      encoding="utf-8")
    _fileops.save_history(str(target), str(sandbox), "zeta",
                          summary="presence heartbeat")
    history_root = sandbox / ".history" / "presence"
    # The blacklist must short-circuit BEFORE mkdir, so the .history/presence/
    # tree should not exist at all (clean fresh sandbox).
    assert_true(not history_root.exists(),
                f"presence/ snapshots should be blacklisted; "
                f"{history_root} unexpectedly exists")


@with_sandbox
def save_history_skips_blacklisted_gate_firings_meta(sandbox, meta_sandbox, _fileops):
    """meta/gate-firings.jsonl must NOT create a .history snapshot."""
    target = meta_sandbox / "gate-firings.jsonl"
    target.write_text(json.dumps({"gate": "test", "rc": 0}) + "\n",
                      encoding="utf-8")
    _fileops.save_history(str(target), str(meta_sandbox), "test-agent",
                          summary="gate fire")
    history_path = meta_sandbox / ".history" / "gate-firings.jsonl"
    assert_true(not history_path.exists(),
                f"gate-firings.jsonl snapshots should be blacklisted; "
                f"{history_path} unexpectedly exists")


@with_sandbox
def save_history_does_not_skip_same_name_in_wrong_base(sandbox, meta_sandbox, _fileops):
    """gate-firings.jsonl pattern is META-only; a same-name file under WORLD must snapshot.

    Defends the dict-by-base structure of _SNAPSHOT_BLACKLIST. A meta pattern
    must not bleed into the world basedir's policy.
    """
    target = sandbox / "gate-firings.jsonl"
    target.write_text(json.dumps({"x": 1}) + "\n", encoding="utf-8")
    _fileops.save_history(str(target), str(sandbox), "test", summary="")
    history_dir = sandbox / ".history" / "gate-firings.jsonl"
    snaps = [s for s in history_dir.iterdir() if not s.name.endswith(".meta")]
    assert_eq(len(snaps), 1,
              "non-blacklisted same-name under WORLD must still snapshot")
    assert_true(snaps[0].name.endswith(".jsonl.gz"),
                f"expected .jsonl.gz, got {snaps[0].name}")


# ---------------------------------------------------------------------------
# Outcome 2 — gzip snapshot format
# ---------------------------------------------------------------------------

@with_sandbox
def save_history_writes_gzip_compressed_snapshot(sandbox, meta_sandbox, _fileops):
    """A non-blacklisted file produces a `.gz` snapshot whose contents decompress
    to byte-equal original."""
    target = sandbox / "aspirations.jsonl"
    payload = (json.dumps({"id": "asp-001", "title": "first"}) + "\n" +
               json.dumps({"id": "asp-002", "title": "second"}) + "\n")
    target.write_text(payload, encoding="utf-8")

    _fileops.save_history(str(target), str(sandbox), "test", summary="initial")

    history_dir = sandbox / ".history" / "aspirations.jsonl"
    snaps = [s for s in history_dir.iterdir() if s.name.endswith(".jsonl.gz")]
    assert_eq(len(snaps), 1, f"expected 1 gzip snapshot, got {len(snaps)}")
    snap = snaps[0]

    with gzip.open(snap, "rt", encoding="utf-8") as f:
        decompressed = f.read()
    assert_eq(decompressed, payload,
              "gzip snapshot must decompress to original content")

    # Sidecar uses the FULL <name>.gz.meta filename so cmd_list's .meta
    # filter excludes it correctly.
    meta = snap.with_suffix(snap.suffix + ".meta")
    assert_true(meta.exists(),
                f"summary sidecar should exist at {meta.name}")
    assert_true(meta.name.endswith(".jsonl.gz.meta"),
                f"sidecar must end in .jsonl.gz.meta, got {meta.name}")
    assert_eq(meta.read_text(encoding="utf-8").strip(), "initial",
              "sidecar should carry summary text uncompressed")


@with_sandbox
def save_history_gzip_reduces_size_meaningfully(sandbox, meta_sandbox, _fileops):
    """A repetitive JSONL file should compress to a fraction of original size.

    Validates that the format choice is actually doing the job we built it
    for — not just adding `.gz` overhead without compression.
    """
    target = sandbox / "reasoning-bank.jsonl"
    # Repetitive content gzip compresses very well — typical for our JSONL.
    payload = "\n".join(json.dumps({"id": f"rb-{i:03d}",
                                    "title": "repetitive content for gzip",
                                    "category": "test",
                                    "tags": ["sample", "noise"],
                                    "status": "active"})
                       for i in range(200)) + "\n"
    target.write_text(payload, encoding="utf-8")

    _fileops.save_history(str(target), str(sandbox), "test")

    history_dir = sandbox / ".history" / "reasoning-bank.jsonl"
    # Finding #3 cleanup: explicit list + assertion beats `next(...)` without
    # default — a missing snapshot produces a clear failure message rather
    # than an opaque StopIteration traceback.
    snaps = [s for s in history_dir.iterdir() if s.name.endswith(".jsonl.gz")]
    assert_eq(len(snaps), 1,
              f"expected exactly one .jsonl.gz snapshot for cap=100 "
              f"on first write, got {len(snaps)}")
    snap = snaps[0]
    original = len(payload.encode("utf-8"))
    compressed = snap.stat().st_size
    ratio = original / max(compressed, 1)
    # Repetitive JSON typically compresses ~10x; demand at least 3x to be
    # safe across gzip-level/platform variation.
    assert_true(ratio >= 3.0,
                f"compression too weak: original={original}, "
                f"compressed={compressed}, ratio={ratio:.2f}")


# ---------------------------------------------------------------------------
# Items 3 + 4 — per-file snapshot cap + auto-prune on write
# ---------------------------------------------------------------------------

@with_sandbox
def per_file_cap_drops_oldest_snapshots_on_write(sandbox, meta_sandbox, _fileops):
    """save_history enforces the per-file cap after each write — oldest first.

    Stamps 6 snapshots into the history dir by hand at known timestamps,
    then triggers one more save_history that pushes the count over the
    cap. Verifies the oldest snapshot was dropped and the newest survives.
    """
    target = sandbox / "aspirations.jsonl"
    target.write_text(json.dumps({"id": "asp-001"}) + "\n",
                      encoding="utf-8")
    history_dir = sandbox / ".history" / "aspirations.jsonl"
    history_dir.mkdir(parents=True)

    # Pre-seed 6 snapshot files at fixed timestamps so ordering is deterministic.
    # (Live test would be slower and rely on datetime.now ordering — too fragile.)
    seeded = []
    for hour in range(6):
        name = f"2026-05-22T0{hour}-00-00_seed.jsonl.gz"
        snap = history_dir / name
        with gzip.open(snap, "wb") as f:
            f.write(b'{"seeded": true}\n')
        seeded.append(snap)

    # Override the cap for this file to 5 so 6 + 1 (new save_history below) = 7 → 2 drops.
    _fileops._PER_FILE_SNAPSHOT_CAP["world"]["aspirations.jsonl"] = 5

    try:
        _fileops.save_history(str(target), str(sandbox), "test-agent",
                              summary="trigger cap")
        remaining = sorted(s.name for s in history_dir.iterdir()
                           if s.name.endswith(".gz"))
        assert_eq(len(remaining), 5,
                  f"cap=5 should leave 5 snapshots, got {len(remaining)}: {remaining}")
        # The two oldest (00- and 01-) should be gone; 02- and later must survive.
        assert_true(not any("T00-00-00" in n for n in remaining),
                    f"oldest snapshot (T00-) should be dropped: {remaining}")
        assert_true(not any("T01-00-00" in n for n in remaining),
                    f"second-oldest snapshot (T01-) should be dropped: {remaining}")
        assert_true(any("T05-00-00" in n for n in remaining),
                    f"newest seeded snapshot (T05-) should survive: {remaining}")
    finally:
        # Restore the override so subsequent tests see the default.
        _fileops._PER_FILE_SNAPSHOT_CAP["world"]["aspirations.jsonl"] = 100


@with_sandbox
def per_file_cap_drops_paired_meta_sidecars(sandbox, meta_sandbox, _fileops):
    """When _prune_to_cap drops a snapshot, its `.meta` sidecar is dropped too."""
    target = sandbox / "aspirations.jsonl"
    target.write_text(json.dumps({"id": "x"}) + "\n", encoding="utf-8")
    history_dir = sandbox / ".history" / "aspirations.jsonl"
    history_dir.mkdir(parents=True)

    # Seed one old snapshot + its meta sidecar.
    old_snap = history_dir / "2026-05-22T00-00-00_seed.jsonl.gz"
    with gzip.open(old_snap, "wb") as f:
        f.write(b'{}\n')
    old_meta = old_snap.with_suffix(old_snap.suffix + ".meta")
    old_meta.write_text("doomed\n", encoding="utf-8")

    _fileops._PER_FILE_SNAPSHOT_CAP["world"]["aspirations.jsonl"] = 1
    try:
        # One new save_history → 2 snapshots, cap=1, oldest dropped.
        _fileops.save_history(str(target), str(sandbox), "test", summary="new")
        assert_true(not old_snap.exists(),
                    "oldest snapshot should be dropped")
        assert_true(not old_meta.exists(),
                    "oldest snapshot's .meta sidecar should be dropped with it")
        # The new snapshot's own sidecar should survive.
        new_metas = list(history_dir.glob("*.meta"))
        assert_eq(len(new_metas), 1,
                  f"only the new sidecar should remain, got {[m.name for m in new_metas]}")
    finally:
        _fileops._PER_FILE_SNAPSHOT_CAP["world"]["aspirations.jsonl"] = 100


@with_sandbox
def per_file_cap_leaves_unparseable_files_alone(sandbox, meta_sandbox, _fileops):
    """Files that don't match the snapshot-name format are NOT touched by the cap.

    Defends against accidentally deleting legacy or non-snapshot artifacts.
    """
    target = sandbox / "aspirations.jsonl"
    target.write_text(json.dumps({"id": "x"}) + "\n", encoding="utf-8")
    history_dir = sandbox / ".history" / "aspirations.jsonl"
    history_dir.mkdir(parents=True)

    # Seed unparseable artifacts that look nothing like our snapshot format.
    stranger = history_dir / "manual-backup-something.jsonl"
    stranger.write_text("manual\n", encoding="utf-8")
    readme = history_dir / "README.txt"
    readme.write_text("notes\n", encoding="utf-8")

    # Seed two valid snapshots so cap=1 would force a drop if it had teeth.
    for hour in range(2):
        snap = history_dir / f"2026-05-22T0{hour}-00-00_seed.jsonl.gz"
        with gzip.open(snap, "wb") as f:
            f.write(b'{}\n')

    _fileops._PER_FILE_SNAPSHOT_CAP["world"]["aspirations.jsonl"] = 1
    try:
        _fileops.save_history(str(target), str(sandbox), "test")
        assert_true(stranger.exists(),
                    "unparseable file 'manual-backup-something.jsonl' must survive")
        assert_true(readme.exists(),
                    "README.txt must survive (non-snapshot artifact)")
    finally:
        _fileops._PER_FILE_SNAPSHOT_CAP["world"]["aspirations.jsonl"] = 100


@with_sandbox
def per_file_cap_uses_default_for_unlisted_files(sandbox, meta_sandbox, _fileops):
    """Files not in _PER_FILE_SNAPSHOT_CAP use DEFAULT_SNAPSHOT_CAP."""
    # An invented file name that's NOT in the overrides table.
    target = sandbox / "some-rare-file.yaml"
    target.write_text("k: v\n", encoding="utf-8")
    cap = _fileops._get_snapshot_cap(sandbox, Path("some-rare-file.yaml"))
    assert_eq(cap, _fileops.DEFAULT_SNAPSHOT_CAP,
              f"unlisted file should fall back to DEFAULT_SNAPSHOT_CAP")


@with_sandbox
def per_file_cap_lookup_respects_base_kind(sandbox, meta_sandbox, _fileops):
    """A pattern under 'world' must not leak into 'meta' (parallel to blacklist)."""
    # aspirations.jsonl is capped at 100 under world, but not listed under meta.
    world_cap = _fileops._get_snapshot_cap(sandbox, Path("aspirations.jsonl"))
    meta_cap = _fileops._get_snapshot_cap(meta_sandbox, Path("aspirations.jsonl"))
    assert_eq(world_cap, 100, "world/aspirations.jsonl cap should be 100")
    assert_eq(meta_cap, _fileops.DEFAULT_SNAPSHOT_CAP,
              "meta/aspirations.jsonl (not in overrides) should fall to default")


@with_sandbox
def per_file_cap_bounds_drops_per_call(sandbox, meta_sandbox, _fileops):
    """When existing surplus is huge, no single save_history call drops more
    than MAX_SNAPSHOTS_DROPPED_PER_CALL.

    Guards against the latency cliff where the first write to a file with
    thousands of pre-existing snapshots would otherwise unlink them all
    synchronously inside the locked write — 30-60s pauses on OneDrive.
    """
    target = sandbox / "aspirations.jsonl"
    target.write_text(json.dumps({"id": "x"}) + "\n", encoding="utf-8")
    history_dir = sandbox / ".history" / "aspirations.jsonl"
    history_dir.mkdir(parents=True)

    # Seed 200 snapshots. With cap=1, drop_count would be 199 if uncapped —
    # but MAX_SNAPSHOTS_DROPPED_PER_CALL = 50 bounds it to 50 per call.
    for hour in range(200):
        # Hour as 4-digit timestamp prefix so they sort distinctly.
        ts = f"2026-05-22T{hour // 60:02d}-{hour % 60:02d}-00"
        snap = history_dir / f"{ts}_seed.jsonl.gz"
        with gzip.open(snap, "wb") as f:
            f.write(b'{}\n')

    _fileops._PER_FILE_SNAPSHOT_CAP["world"]["aspirations.jsonl"] = 1
    try:
        _fileops.save_history(str(target), str(sandbox), "test")
        # 200 seeded + 1 new = 201 total. Cap=1, surplus=200.
        # Per-call limit caps drops at 50, so 201 - 50 = 151 should remain.
        surviving = [s for s in history_dir.iterdir() if s.name.endswith(".gz")]
        assert_eq(len(surviving), 151,
                  f"per-call cap should bound drops at "
                  f"MAX_SNAPSHOTS_DROPPED_PER_CALL=50; "
                  f"expected 151 survivors (201 - 50), got {len(surviving)}")
    finally:
        _fileops._PER_FILE_SNAPSHOT_CAP["world"]["aspirations.jsonl"] = 100


@with_sandbox
def per_file_cap_skips_when_under_cap(sandbox, meta_sandbox, _fileops):
    """No drops fire when snapshot count is <= cap. Counts return 0, files preserved."""
    target = sandbox / "aspirations.jsonl"
    target.write_text(json.dumps({"id": "x"}) + "\n", encoding="utf-8")
    history_dir = sandbox / ".history" / "aspirations.jsonl"
    history_dir.mkdir(parents=True)
    # Seed 2 snapshots
    for hour in range(2):
        snap = history_dir / f"2026-05-22T0{hour}-00-00_seed.jsonl.gz"
        with gzip.open(snap, "wb") as f:
            f.write(b'{}\n')

    # cap=100 → 2 existing + 1 new = 3 well under cap; no drops.
    _fileops.save_history(str(target), str(sandbox), "test")
    surviving = [s for s in history_dir.iterdir() if s.name.endswith(".gz")]
    assert_eq(len(surviving), 3,
              f"under-cap save should keep all 3 snapshots, got {len(surviving)}")


# ---------------------------------------------------------------------------
# Error-precedence (finding #1 cleanup verification)
# ---------------------------------------------------------------------------

@with_sandbox
def save_history_raises_value_error_for_path_outside_base(sandbox, meta_sandbox, _fileops):
    """A path outside base_dir raises ValueError BEFORE any JSONL parse runs.

    Verifies finding #1 cleanup: the try/except wrapping relative_to is gone
    so the standard ValueError surfaces first when the caller misuses save_history.
    """
    # Create the source file in meta_sandbox; declare base_dir as world sandbox.
    bogus = meta_sandbox / "alien.jsonl"
    bogus.write_text(json.dumps({"id": "x"}) + "\n", encoding="utf-8")
    try:
        _fileops.save_history(str(bogus), str(sandbox), "test")
    except ValueError as e:
        # Standard relative_to error mentions both paths; that's enough signal.
        assert_true(True, f"got expected ValueError: {e}")
        return
    raise AssertionError("expected ValueError for path outside base_dir")


# ---------------------------------------------------------------------------
# Outcome 3 — history.parse_snapshot_name handles .gz
# ---------------------------------------------------------------------------

def parse_snapshot_name_strips_gz_suffix():
    """parse_snapshot_name extracts timestamp + agent from a .gz filename."""
    import history  # noqa: F401 — module under test
    ts, agent = history.parse_snapshot_name("2026-05-22T09-15-00_zeta.jsonl.gz")
    assert_true(ts is not None,
                f"expected parseable timestamp for .gz filename")
    assert_eq(agent, "zeta",
              "agent token must come from underlying ext, not .gz")


def parse_snapshot_name_handles_legacy_uncompressed():
    """Legacy uncompressed filenames continue to parse correctly."""
    import history  # noqa: F401
    ts, agent = history.parse_snapshot_name("2026-05-22T09-15-00_alpha.md")
    assert_true(ts is not None, "legacy .md snapshot should still parse")
    assert_eq(agent, "alpha", "legacy agent token should still parse")


# ---------------------------------------------------------------------------
# Outcome 4 — _resolve_version_path tolerance
# ---------------------------------------------------------------------------

def _setup_history_dir():
    """Plant a legacy gz snapshot under a sandbox WORLD's .history tree.

    Stage 2's _find_snapshot_by_name takes (file_path, version_name) and
    resolves the snapshot via _find_history_snapshots, which needs a real
    base_dir. So the fixture now creates an MIND_WORLD sandbox + plants
    the snapshot under <world>/.history/<rel>/<snap-name>.

    Returns (sandbox, target_file, snap_path). Caller cleans up sandbox.
    The caller is also responsible for env var restoration via the same
    pattern used by with_sandbox above — here we keep it inline because
    these three lookup tests need to invoke history._find_snapshot_by_name
    against a known-good base_dir without going through save_history.
    """
    sandbox = Path(tempfile.mkdtemp(prefix="hist_resolve_world_"))
    os.environ["MIND_WORLD"] = str(sandbox)
    os.environ["MIND_WORLD"] = str(sandbox)
    meta = str(Path(tempfile.mkdtemp(prefix="hist_resolve_meta_")))
    os.environ.setdefault("MIND_META", meta)
    os.environ.setdefault("MIND_META", meta)
    for mod in list(sys.modules):
        if mod in ("_fileops", "_paths", "history"):
            del sys.modules[mod]
    target = sandbox / "data.jsonl"
    target.write_bytes(b'{"hello": "world"}\n')
    snap_dir = sandbox / ".history" / "data.jsonl"
    snap_dir.mkdir(parents=True, exist_ok=True)
    snap = snap_dir / "2026-05-22T09-15-00_zeta.jsonl.gz"
    with gzip.open(snap, "wb") as f:
        f.write(b'{"hello": "world"}\n')
    return sandbox, target, snap


def _teardown_history_dir(sandbox):
    shutil.rmtree(sandbox, ignore_errors=True)
    os.environ.pop("MIND_WORLD", None)
    os.environ.pop("MIND_WORLD", None)
    # MIND_META / MIND_META intentionally left — the wrapper above sets
    # them lazily and subsequent tests via with_sandbox overwrite them.


def resolve_version_path_accepts_literal_gz_filename():
    """Stage 2 successor _find_snapshot_by_name finds the literal name."""
    # Fixture MUST run before `import history` — _setup_history_dir clears
    # cached _paths/_fileops/history so the next import picks up the fresh
    # MIND_WORLD. Importing history first would bind to a stale module.
    sandbox, target, snap = _setup_history_dir()
    try:
        import history
        found = history._find_snapshot_by_name(target, snap.name)
        assert_eq(found, snap, "literal .gz filename should resolve")
    finally:
        _teardown_history_dir(sandbox)


def resolve_version_path_accepts_bare_filename_without_gz():
    """User passes 'foo.jsonl' but on-disk is 'foo.jsonl.gz' — must resolve."""
    sandbox, target, snap = _setup_history_dir()
    try:
        import history
        bare = snap.name[:-3]  # strip .gz
        found = history._find_snapshot_by_name(target, bare)
        assert_eq(found, snap,
                  "bare filename should resolve to its .gz on-disk form")
    finally:
        _teardown_history_dir(sandbox)


def resolve_version_path_returns_none_for_missing():
    sandbox, target, _ = _setup_history_dir()
    try:
        import history
        found = history._find_snapshot_by_name(target, "does-not-exist.jsonl.gz")
        assert_eq(found, None, "missing version should return None")
    finally:
        _teardown_history_dir(sandbox)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    save_history_skips_blacklisted_presence_dir,
    save_history_skips_blacklisted_gate_firings_meta,
    save_history_does_not_skip_same_name_in_wrong_base,
    save_history_writes_gzip_compressed_snapshot,
    save_history_gzip_reduces_size_meaningfully,
    parse_snapshot_name_strips_gz_suffix,
    parse_snapshot_name_handles_legacy_uncompressed,
    resolve_version_path_accepts_literal_gz_filename,
    resolve_version_path_accepts_bare_filename_without_gz,
    resolve_version_path_returns_none_for_missing,
    # Items 3 + 4 — per-file cap + auto-prune on write
    per_file_cap_drops_oldest_snapshots_on_write,
    per_file_cap_drops_paired_meta_sidecars,
    per_file_cap_leaves_unparseable_files_alone,
    per_file_cap_uses_default_for_unlisted_files,
    per_file_cap_lookup_respects_base_kind,
    per_file_cap_bounds_drops_per_call,
    per_file_cap_skips_when_under_cap,
    # Finding #1 cleanup — error precedence
    save_history_raises_value_error_for_path_outside_base,
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
