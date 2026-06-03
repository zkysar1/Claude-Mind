"""Stage 0 tests for _history_store.py (CAS-delta snapshot store).

Standalone TESTS-list runner: py -3 <this-file>

Covers:
- Round-trip: save -> restore yields exact bytes.
- First snapshot is always full (no prior to delta against).
- Second snapshot uses delta for text + small edits.
- Delta chain: N versions, restore each.
- Anchor interval forces full blobs at the right cadence.
- Identical content dedup: second save of same bytes adds a manifest only.
- Binary content skips delta.
- Huge content (>5MB) skips delta.
- list_snapshots returns newest-first.
- restore on dropped manifest raises ValueError.
- vacuum dry-run makes no changes.
- vacuum deletes orphan blobs + patches.
- vacuum keeps reachable storage intact.
- metadata-only-after-days drops blobs but keeps manifests.
- Manifest YAML round-trip (escaping etc).
- Cycle defense in _resolve_chain.
"""

import io
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))


def with_sandbox(test_fn):
    """Fresh temp dir + fresh _history_store import per test."""
    def wrapped():
        sandbox = Path(tempfile.mkdtemp(prefix="hstore_"))
        try:
            for mod in list(sys.modules):
                if mod == "_history_store":
                    del sys.modules[mod]
            import _history_store
            test_fn(sandbox, _history_store)
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)
    wrapped.__name__ = test_fn.__name__
    return wrapped


def assert_eq(actual, expected, msg=""):
    if actual != expected:
        raise AssertionError(f"{msg}: expected {expected!r}, got {actual!r}")


def assert_true(cond, msg=""):
    if not cond:
        raise AssertionError(f"FAIL: {msg}")


def _make_file(sandbox, name, content_bytes):
    """Create a sandbox-relative file path (NOT the file itself -- save uses
    the bytes directly; file_path is just an identifier for the manifest dir)."""
    return sandbox / name


# ---------------------------------------------------------------------------
# Round-trip + basic semantics
# ---------------------------------------------------------------------------

@with_sandbox
def round_trip_single_save(sandbox, store):
    content = b'{"id":"a","v":1}\n{"id":"b","v":2}\n'
    file_path = _make_file(sandbox, "data.jsonl", content)
    manifest = store.save(file_path, content, sandbox, agent="alpha", summary="first")
    restored = store.restore(file_path, manifest.name, sandbox)
    assert_eq(restored, content, "round-trip identity")


@with_sandbox
def first_snapshot_is_full(sandbox, store):
    content = b'{"id":"a"}\n'
    file_path = _make_file(sandbox, "data.jsonl", content)
    store.save(file_path, content, sandbox, agent="alpha")
    snaps = store.list_snapshots(file_path, sandbox)
    assert_eq(len(snaps), 1, "single snapshot")
    assert_eq(snaps[0]["encoding"], "full", "first snapshot must be full")


@with_sandbox
def second_text_snapshot_uses_delta(sandbox, store):
    """Delta savings depend on content size: a sub-100-byte file's delta
    overhead (JSON opcodes + gzip framing) easily exceeds 50% of a direct
    full-gzip. Use ~1.5KB of unique-token content (which gzip can't
    compress much) so a single-line append produces a delta well under
    the savings threshold."""
    file_path = _make_file(sandbox, "data.jsonl", b"")
    v1 = b"".join(
        f"line_{i:04d}_alpha_beta_gamma_delta_{i * 97}_{i * 137}\n".encode("utf-8")
        for i in range(50)
    )
    v2 = v1 + b"line_0050_alpha_beta_gamma_delta_4850_6850\n"
    store.save(file_path, v1, sandbox, agent="alpha")
    time.sleep(0.01)
    store.save(file_path, v2, sandbox, agent="beta")
    snaps = store.list_snapshots(file_path, sandbox)
    assert_eq(len(snaps), 2, "two snapshots")
    assert_eq(snaps[0]["encoding"], "delta", "newer should be delta")
    restored = store.restore(file_path, snaps[0]["snapshot_id"], sandbox)
    assert_eq(restored, v2, "delta restore yields v2")


@with_sandbox
def delta_chain_restores_every_version(sandbox, store):
    file_path = _make_file(sandbox, "data.jsonl", b"")
    versions = []
    for i in range(10):
        content = ("".join(f"line{j}\n" for j in range(i + 1))).encode("utf-8")
        versions.append(content)
        store.save(file_path, content, sandbox, agent=f"a{i}")
        time.sleep(0.005)  # disambiguate manifest names
    snaps = store.list_snapshots(file_path, sandbox)
    # newest-first; snaps[0] corresponds to versions[-1]
    for i, snap in enumerate(snaps):
        target = versions[len(versions) - 1 - i]
        restored = store.restore(file_path, snap["snapshot_id"], sandbox)
        assert_eq(restored, target, f"restore snapshot {i} (version {len(versions) - 1 - i})")


# ---------------------------------------------------------------------------
# Anchor logic
# ---------------------------------------------------------------------------

@with_sandbox
def anchor_interval_forces_full_at_correct_cadence(sandbox, store):
    """With anchor_interval=5: save 1 full, 4 deltas, save 1 full, 4 deltas, ...
    Over 25 saves -> 5 fulls (at indices 0, 5, 10, 15, 20 zero-based, or
    1/6/11/16/21 one-based).

    Each version appends one line of unique content to a ~50-line baseline
    so the delta is always tiny relative to the full content, well under
    the 50% savings threshold (otherwise the delta path falls back to full
    and the anchor cadence becomes invisible)."""
    def _content_at(version):
        return b"".join(
            f"line_{j:04d}_alpha_beta_gamma_delta_{j * 97}_{j * 137}\n".encode("utf-8")
            for j in range(50 + version)
        )

    file_path = _make_file(sandbox, "data.jsonl", b"")
    for i in range(25):
        store.save(file_path, _content_at(i), sandbox, agent=f"a{i:02d}", anchor_interval=5)
        time.sleep(0.003)
    snaps = store.list_snapshots(file_path, sandbox)
    full_count = sum(1 for s in snaps if s["encoding"] == "full")
    assert_eq(full_count, 5, "25 saves with anchor_interval=5 -> 5 full blobs")
    # Verify all 25 restore correctly.
    for i, snap in enumerate(snaps):
        target_version_idx = 25 - 1 - i  # newest-first
        actual = store.restore(file_path, snap["snapshot_id"], sandbox)
        assert_eq(actual, _content_at(target_version_idx),
                  f"snap {i} restores to version {target_version_idx}")


@with_sandbox
def chain_length_resets_after_anchor(sandbox, store):
    """After a forced anchor, chain_length resets to 0 and counts up again."""
    file_path = _make_file(sandbox, "data.jsonl", b"")
    # First save: full, chain_length=0
    store.save(file_path, b"a\n", sandbox, agent="x0", anchor_interval=3)
    time.sleep(0.003)
    # 2nd save: delta, chain_length=1
    store.save(file_path, b"a\nb\n", sandbox, agent="x1", anchor_interval=3)
    time.sleep(0.003)
    # 3rd save: forced anchor (prior chain_length == anchor_interval-1 == 2 ?)
    # Wait, chain_length=1 < 2 so 3rd save = delta, chain_length=2.
    store.save(file_path, b"a\nb\nc\n", sandbox, agent="x2", anchor_interval=3)
    time.sleep(0.003)
    # 4th save: prior.chain_length=2 == anchor_interval-1=2 -> forced full.
    store.save(file_path, b"a\nb\nc\nd\n", sandbox, agent="x3", anchor_interval=3)
    snaps = store.list_snapshots(file_path, sandbox)
    # newest-first: snaps[0]=4th (full), snaps[1]=3rd (delta, cl=2), snaps[2]=2nd (delta, cl=1), snaps[3]=1st (full, cl=0)
    assert_eq(snaps[0]["encoding"], "full", "4th save anchors")
    assert_eq(snaps[3]["encoding"], "full", "1st save is full")
    # Verify all restore correctly.
    assert_eq(store.restore(file_path, snaps[0]["snapshot_id"], sandbox), b"a\nb\nc\nd\n", "v4")
    assert_eq(store.restore(file_path, snaps[1]["snapshot_id"], sandbox), b"a\nb\nc\n", "v3")
    assert_eq(store.restore(file_path, snaps[2]["snapshot_id"], sandbox), b"a\nb\n", "v2")
    assert_eq(store.restore(file_path, snaps[3]["snapshot_id"], sandbox), b"a\n", "v1")


# ---------------------------------------------------------------------------
# Content-addressed dedup
# ---------------------------------------------------------------------------

@with_sandbox
def identical_content_no_duplicate_blob(sandbox, store):
    """Two saves of identical content: 1 blob, 2 manifests."""
    file_path = _make_file(sandbox, "data.jsonl", b"")
    content = b'{"id":"a"}\n'
    store.save(file_path, content, sandbox, agent="alpha")
    time.sleep(0.005)
    store.save(file_path, content, sandbox, agent="beta")
    blobs = list((sandbox / ".history" / "blobs").rglob("*.gz"))
    assert_eq(len(blobs), 1, "exactly one blob for two identical-content saves")
    snaps = store.list_snapshots(file_path, sandbox)
    assert_eq(len(snaps), 2, "but two manifests")
    # Both must restore correctly.
    for snap in snaps:
        assert_eq(store.restore(file_path, snap["snapshot_id"], sandbox), content,
                  f"restore {snap['snapshot_id']}")


@with_sandbox
def cross_file_dedup_via_content_address(sandbox, store):
    """Two different files with identical content share storage."""
    file_a = _make_file(sandbox, "a.jsonl", b"")
    file_b = _make_file(sandbox, "b.jsonl", b"")
    content = b'{"x":1}\n'
    store.save(file_a, content, sandbox, agent="alpha")
    store.save(file_b, content, sandbox, agent="alpha")
    blobs = list((sandbox / ".history" / "blobs").rglob("*.gz"))
    assert_eq(len(blobs), 1, "one blob for identical content across two files")


# ---------------------------------------------------------------------------
# Binary / huge fallback
# ---------------------------------------------------------------------------

@with_sandbox
def binary_content_skips_delta(sandbox, store):
    file_path = _make_file(sandbox, "data.bin", b"")
    v1 = bytes(range(256)) * 4  # contains nulls
    v2 = v1 + b"\x00\x01\x02"
    store.save(file_path, v1, sandbox, agent="alpha")
    time.sleep(0.005)
    store.save(file_path, v2, sandbox, agent="beta")
    snaps = store.list_snapshots(file_path, sandbox)
    assert_eq(snaps[0]["encoding"], "full", "binary v2 falls back to full")
    # Restore must work.
    assert_eq(store.restore(file_path, snaps[0]["snapshot_id"], sandbox), v2, "binary restore")


@with_sandbox
def huge_content_skips_delta(sandbox, store):
    """Content larger than DEFAULT_FULL_BLOB_MAX_SIZE always stores as full."""
    file_path = _make_file(sandbox, "data.bin", b"")
    small = b"hello\n"
    huge = ("X" * (store.DEFAULT_FULL_BLOB_MAX_SIZE + 1)).encode("utf-8")
    store.save(file_path, small, sandbox, agent="alpha")
    time.sleep(0.005)
    store.save(file_path, huge, sandbox, agent="beta")
    snaps = store.list_snapshots(file_path, sandbox)
    assert_eq(snaps[0]["encoding"], "full", "huge content falls back to full")


# ---------------------------------------------------------------------------
# Listing + special encodings
# ---------------------------------------------------------------------------

@with_sandbox
def list_snapshots_newest_first(sandbox, store):
    file_path = _make_file(sandbox, "data.jsonl", b"")
    for i in range(3):
        store.save(file_path, f"v{i}\n".encode("utf-8"), sandbox, agent=f"a{i}")
        time.sleep(0.005)
    snaps = store.list_snapshots(file_path, sandbox)
    assert_eq(len(snaps), 3, "three snapshots")
    # Check ordering: timestamps should be strictly descending (newest first).
    timestamps = [s["timestamp"] for s in snaps]
    assert_eq(timestamps, sorted(timestamps, reverse=True), "newest-first")


@with_sandbox
def restore_dropped_manifest_raises(sandbox, store):
    file_path = _make_file(sandbox, "data.jsonl", b"")
    store.save(file_path, b"hello\n", sandbox, agent="alpha")
    snaps = store.list_snapshots(file_path, sandbox)
    snap_id = snaps[0]["snapshot_id"]
    # Manually rewrite this manifest to encoding=dropped.
    mpath = sandbox / ".history" / "snapshots" / "data.jsonl" / snap_id
    manifest = store._read_manifest(mpath)
    manifest["encoding"] = "dropped"
    manifest["base"] = None
    store._write_manifest(mpath, manifest)
    try:
        store.restore(file_path, snap_id, sandbox)
    except ValueError as e:
        assert_true("vacuumed" in str(e), f"error mentions vacuum: {e}")
        return
    raise AssertionError("restore on dropped manifest must raise ValueError")


# ---------------------------------------------------------------------------
# Vacuum
# ---------------------------------------------------------------------------

@with_sandbox
def vacuum_dry_run_changes_nothing(sandbox, store):
    file_path = _make_file(sandbox, "data.jsonl", b"")
    for i in range(5):
        store.save(file_path, f"v{i}\n".encode("utf-8"), sandbox, agent=f"a{i}")
        time.sleep(0.003)
    blobs_before = sorted(p.name for p in (sandbox / ".history" / "blobs").rglob("*.gz"))
    patches_before = sorted(p.name for p in (sandbox / ".history" / "patches").rglob("*.gz"))
    result = store.vacuum(sandbox, dry_run=True)
    blobs_after = sorted(p.name for p in (sandbox / ".history" / "blobs").rglob("*.gz"))
    patches_after = sorted(p.name for p in (sandbox / ".history" / "patches").rglob("*.gz"))
    assert_eq(blobs_before, blobs_after, "dry-run preserves blobs")
    assert_eq(patches_before, patches_after, "dry-run preserves patches")


@with_sandbox
def vacuum_keeps_reachable_storage(sandbox, store):
    """All reachable blobs/patches must survive vacuum --apply."""
    file_path = _make_file(sandbox, "data.jsonl", b"")
    for i in range(5):
        store.save(file_path, f"v{i}\n".encode("utf-8"), sandbox, agent=f"a{i}")
        time.sleep(0.003)
    # Vacuum without metadata_only_after_days: only orphans deleted.
    result = store.vacuum(sandbox, dry_run=False)
    assert_eq(result["blobs_deleted"], 0, "no reachable blobs deleted")
    assert_eq(result["patches_deleted"], 0, "no reachable patches deleted")
    # All snapshots must still restore.
    snaps = store.list_snapshots(file_path, sandbox)
    for i, snap in enumerate(snaps):
        target_idx = len(snaps) - 1 - i
        expected = f"v{target_idx}\n".encode("utf-8")
        actual = store.restore(file_path, snap["snapshot_id"], sandbox)
        assert_eq(actual, expected, f"reachable snap {snap['snapshot_id']} survives vacuum")


@with_sandbox
def vacuum_deletes_orphan_blob(sandbox, store):
    file_path = _make_file(sandbox, "data.jsonl", b"")
    store.save(file_path, b"real\n", sandbox, agent="alpha")
    # Plant an orphan blob.
    orphan_dir = sandbox / ".history" / "blobs" / "zz"
    orphan_dir.mkdir(parents=True, exist_ok=True)
    orphan = orphan_dir / "fakehashzzz.gz"
    orphan.write_bytes(b"orphan payload")
    result = store.vacuum(sandbox, dry_run=False)
    assert_true(result["blobs_deleted"] >= 1, f"orphan should be deleted: {result}")
    assert_true(not orphan.exists(), "orphan file gone")


@with_sandbox
def vacuum_deletes_orphan_patch(sandbox, store):
    file_path = _make_file(sandbox, "data.jsonl", b"")
    store.save(file_path, b"real\n", sandbox, agent="alpha")
    # Plant an orphan patch.
    orphan_dir = sandbox / ".history" / "patches" / "ab"
    orphan_dir.mkdir(parents=True, exist_ok=True)
    orphan = orphan_dir / (("c" * 62) + ".from." + ("d" * 64) + ".gz")
    orphan.write_bytes(b"orphan patch")
    result = store.vacuum(sandbox, dry_run=False)
    assert_true(result["patches_deleted"] >= 1, f"orphan patch should be deleted: {result}")
    assert_true(not orphan.exists(), "orphan patch gone")


@with_sandbox
def vacuum_metadata_only_drops_old_blobs(sandbox, store):
    """metadata_only_after_days rewrites stale manifests + drops their blobs."""
    file_path = _make_file(sandbox, "data.jsonl", b"")
    # Save 3 versions.
    for i in range(3):
        store.save(file_path, f"v{i}\n".encode("utf-8"), sandbox, agent=f"a{i}")
        time.sleep(0.005)
    snaps_before = store.list_snapshots(file_path, sandbox)
    assert_eq(len(snaps_before), 3, "3 snapshots seeded")
    blobs_before = list((sandbox / ".history" / "blobs").rglob("*.gz"))
    patches_before = list((sandbox / ".history" / "patches").rglob("*.gz"))
    assert_true(len(blobs_before) >= 1, "at least one blob exists")
    # Backdate ALL manifests' mtime so they're older than the cutoff.
    long_ago = time.time() - 999 * 86400
    for m_path in (sandbox / ".history" / "snapshots").rglob("*.yaml"):
        os.utime(m_path, (long_ago, long_ago))
    # Run vacuum with metadata_only_after_days=1.
    result = store.vacuum(sandbox, dry_run=False, metadata_only_after_days=1)
    assert_eq(result["manifests_dropped"], 3, "all 3 manifests dropped to metadata-only")
    # Blobs + patches should all be gone (none reachable after drop).
    blobs_after = list((sandbox / ".history" / "blobs").rglob("*.gz"))
    patches_after = list((sandbox / ".history" / "patches").rglob("*.gz"))
    assert_eq(len(blobs_after), 0, "all blobs vacuumed")
    assert_eq(len(patches_after), 0, "all patches vacuumed")
    # Manifests still exist with encoding=dropped.
    snaps_after = store.list_snapshots(file_path, sandbox)
    assert_eq(len(snaps_after), 3, "3 manifests preserved as audit trail")
    for s in snaps_after:
        assert_eq(s["encoding"], "dropped", "all manifests now dropped")
    # Restore must raise.
    try:
        store.restore(file_path, snaps_after[0]["snapshot_id"], sandbox)
    except ValueError:
        pass
    else:
        raise AssertionError("restore on dropped manifest should raise")


# ---------------------------------------------------------------------------
# Manifest YAML round-trip + escaping
# ---------------------------------------------------------------------------

@with_sandbox
def manifest_summary_with_special_chars_round_trips(sandbox, store):
    """Summary containing colons, quotes, newlines must round-trip via the YAML parser."""
    file_path = _make_file(sandbox, "data.jsonl", b"")
    summary = 'commit: "fixed issue: with quotes"'
    manifest_path = store.save(file_path, b"x\n", sandbox, agent="alpha", summary=summary)
    parsed = store._read_manifest(manifest_path)
    assert_eq(parsed["summary"], summary, "summary survives quote escaping")


@with_sandbox
def manifest_with_empty_summary_round_trips(sandbox, store):
    file_path = _make_file(sandbox, "data.jsonl", b"")
    manifest_path = store.save(file_path, b"x\n", sandbox, agent="alpha", summary="")
    parsed = store._read_manifest(manifest_path)
    # Empty string is canonically represented as null in our flat YAML.
    assert_true(parsed["summary"] in (None, ""), f"empty summary normalizes to null or empty, got {parsed['summary']!r}")


# ---------------------------------------------------------------------------
# Cycle defense
# ---------------------------------------------------------------------------

@with_sandbox
def cycle_in_patch_chain_raises(sandbox, store):
    """Manually plant a malformed patch that cycles; restore must raise."""
    file_path = _make_file(sandbox, "data.jsonl", b"")
    store.save(file_path, b"hello\n", sandbox, agent="alpha")
    # Plant a self-referencing patch (hash X.from.X).
    fake_hash = "f" * 64
    patch_dir = sandbox / ".history" / "patches" / fake_hash[:2]
    patch_dir.mkdir(parents=True, exist_ok=True)
    patch_path = patch_dir / f"{fake_hash[2:]}.from.{fake_hash}.gz"
    patch_path.write_bytes(b"any bytes")
    try:
        store._resolve_chain(fake_hash, sandbox)
    except (ValueError, FileNotFoundError) as e:
        return
    raise AssertionError("cycle should raise ValueError or FileNotFoundError")


# ---------------------------------------------------------------------------
# Fresh-eyes regression tests (2026-05-22)
# ---------------------------------------------------------------------------
# Cover bugs surfaced by adversarial review of Stage 0 + Stage 1:
#   - vacuum must refuse to delete anything if it sees a corrupt manifest
#     (silently treating an unparseable manifest as "no references" would
#     mark its real blobs/patches as orphans and delete them)
#   - _atomic_write_bytes must use a unique tmp suffix so two writers of
#     identical content (cross-file dedup) can't race on the .tmp filename

@with_sandbox
def vacuum_aborts_on_unknown_encoding(sandbox, store):
    """A manifest with encoding=<unknown> must abort vacuum, not be ignored."""
    file_path = _make_file(sandbox, "data.jsonl", b"")
    store.save(file_path, b"real-content\n", sandbox, agent="alpha")
    blob_count_before = len(list((sandbox / ".history" / "blobs").rglob("*.gz")))
    assert_true(blob_count_before >= 1, "seed blob exists")
    # Plant a corrupt manifest with encoding=bogus.
    bad_manifest = sandbox / ".history" / "snapshots" / "data.jsonl" / "1999-01-01T00-00-00.000001_evil.yaml"
    bad_manifest.parent.mkdir(parents=True, exist_ok=True)
    bad_manifest.write_text(
        "hash: deadbeef\nencoding: bogus\nbase: null\nsize_bytes: 0\n"
        "agent: evil\nsummary: corrupt\ntimestamp: 1999-01-01T00-00-00.000001\n"
        "chain_length: 0\n", encoding="utf-8")
    # Apply vacuum.
    result = store.vacuum(sandbox, dry_run=False)
    assert_eq(result["aborted"], "corrupt_manifests_detected",
              f"vacuum should abort on corrupt manifest, got {result}")
    assert_eq(result["blobs_deleted"], 0,
              "vacuum must NOT delete blobs when aborting")
    assert_eq(result["patches_deleted"], 0,
              "vacuum must NOT delete patches when aborting")
    # Original blob still on disk.
    blob_count_after = len(list((sandbox / ".history" / "blobs").rglob("*.gz")))
    assert_eq(blob_count_after, blob_count_before, "blob preserved")


@with_sandbox
def vacuum_aborts_on_missing_hash(sandbox, store):
    """A manifest with encoding=full but no hash field aborts vacuum."""
    file_path = _make_file(sandbox, "data.jsonl", b"")
    store.save(file_path, b"real-content\n", sandbox, agent="alpha")
    blob_count_before = len(list((sandbox / ".history" / "blobs").rglob("*.gz")))
    # Plant a manifest with encoding=full but hash:null.
    bad_manifest = sandbox / ".history" / "snapshots" / "data.jsonl" / "1999-01-01T00-00-00.000001_evil.yaml"
    bad_manifest.parent.mkdir(parents=True, exist_ok=True)
    bad_manifest.write_text(
        "hash: null\nencoding: full\nbase: null\nsize_bytes: 0\n"
        "agent: evil\nsummary: corrupt\ntimestamp: 1999-01-01T00-00-00.000001\n"
        "chain_length: 0\n", encoding="utf-8")
    result = store.vacuum(sandbox, dry_run=False)
    assert_eq(result["aborted"], "corrupt_manifests_detected",
              f"vacuum should abort on missing hash, got {result}")
    assert_eq(result["blobs_deleted"], 0, "no blobs deleted on abort")
    blob_count_after = len(list((sandbox / ".history" / "blobs").rglob("*.gz")))
    assert_eq(blob_count_after, blob_count_before, "blob preserved")


@with_sandbox
def vacuum_aborts_on_missing_base_for_delta(sandbox, store):
    """A manifest with encoding=delta but no base field aborts vacuum."""
    file_path = _make_file(sandbox, "data.jsonl", b"")
    store.save(file_path, b"real-content\n", sandbox, agent="alpha")
    # Plant a manifest with encoding=delta but base:null.
    bad_manifest = sandbox / ".history" / "snapshots" / "data.jsonl" / "1999-01-01T00-00-00.000001_evil.yaml"
    bad_manifest.parent.mkdir(parents=True, exist_ok=True)
    bad_manifest.write_text(
        "hash: deadbeef\nencoding: delta\nbase: null\nsize_bytes: 0\n"
        "agent: evil\nsummary: corrupt\ntimestamp: 1999-01-01T00-00-00.000001\n"
        "chain_length: 1\n", encoding="utf-8")
    result = store.vacuum(sandbox, dry_run=False)
    assert_eq(result["aborted"], "corrupt_manifests_detected",
              f"vacuum should abort on delta-without-base, got {result}")
    assert_eq(result["blobs_deleted"], 0, "no blobs deleted on abort")
    # Verify the corrupt manifest is named in the result.
    paths = [p for p, _ in result["corrupt_manifests"]]
    assert_true(any(str(bad_manifest) in p for p in paths),
                f"corrupt manifest listed in result: {result['corrupt_manifests']}")


@with_sandbox
def atomic_write_uses_unique_tmp_suffix(sandbox, store):
    """_atomic_write_bytes must use a unique .tmp name per writer so
    concurrent writers of identical content can't race on the same .tmp.

    Probes the implementation by pre-planting a leftover .tmp at the
    deterministic name (the OLD behavior) and verifying _atomic_write_bytes
    does NOT trip over it -- with the unique-suffix fix, the new tmp name
    will not collide with the pre-existing one."""
    blob_dir = sandbox / ".history" / "blobs" / "aa"
    blob_dir.mkdir(parents=True, exist_ok=True)
    target = blob_dir / ("b" * 62 + ".gz")
    # Pre-plant the OLD deterministic tmp at <target>.tmp to simulate a
    # stale leftover from a crashed writer. The fix should not collide.
    stale_tmp = target.with_suffix(target.suffix + ".tmp")
    stale_tmp.write_bytes(b"stale leftover bytes")
    # Now write the real content via _atomic_write_bytes. With unique
    # tmp suffix, this proceeds without touching the stale leftover.
    store._atomic_write_bytes(target, b"real content")
    assert_true(target.exists(), "target landed")
    assert_eq(target.read_bytes(), b"real content", "target has real content")
    # The stale leftover remains untouched (it's a leftover; not our problem).
    assert_true(stale_tmp.exists(), "stale leftover from prior writer untouched")
    # And no new .tmp file lingers under our deterministic name.
    # (Our unique tmp had pid+random; os.replace consumed it.)
    leftover_unique_tmps = [
        p for p in blob_dir.iterdir()
        if p.name.startswith(target.name) and p.name.endswith(".tmp")
        and p != stale_tmp
    ]
    assert_eq(leftover_unique_tmps, [], "no leftover unique-tmp")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    round_trip_single_save,
    first_snapshot_is_full,
    second_text_snapshot_uses_delta,
    delta_chain_restores_every_version,
    anchor_interval_forces_full_at_correct_cadence,
    chain_length_resets_after_anchor,
    identical_content_no_duplicate_blob,
    cross_file_dedup_via_content_address,
    binary_content_skips_delta,
    huge_content_skips_delta,
    list_snapshots_newest_first,
    restore_dropped_manifest_raises,
    vacuum_dry_run_changes_nothing,
    vacuum_keeps_reachable_storage,
    vacuum_deletes_orphan_blob,
    vacuum_deletes_orphan_patch,
    vacuum_metadata_only_drops_old_blobs,
    manifest_summary_with_special_chars_round_trips,
    manifest_with_empty_summary_round_trips,
    cycle_in_patch_chain_raises,
    # Fresh-eyes regression tests (2026-05-22)
    vacuum_aborts_on_unknown_encoding,
    vacuum_aborts_on_missing_hash,
    vacuum_aborts_on_missing_base_for_delta,
    atomic_write_uses_unique_tmp_suffix,
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
