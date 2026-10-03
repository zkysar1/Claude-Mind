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


@with_sandbox
def vacuum_skips_yaml_named_source_dir(sandbox, store):
    """Regression -a: a source file whose NAME ends in .yaml creates a
    manifest DIRECTORY that also ends in .yaml, so rglob('*.yaml') matches that
    directory. Reading a directory as a manifest raised 'Is a directory', and
    vacuum Phase 2a's fail-safe then treated it as a corrupt manifest and ABORTED
    the ENTIRE vacuum (12 such .yaml-named source dirs on the live store made
    vacuum reclaim 0 of ~4G). vacuum must SKIP the directory, not abort."""
    file_path = _make_file(sandbox, "config.yaml", b"")
    for i in range(3):
        store.save(file_path, f"k: v{i}\n".encode("utf-8"), sandbox, agent=f"a{i}")
        time.sleep(0.003)
    # The manifest dir itself ends in .yaml — the exact bug trigger.
    manifest_dir = sandbox / ".history" / "snapshots" / "config.yaml"
    assert_true(manifest_dir.is_dir(), "manifest dir named config.yaml exists")
    # Plant an orphan blob so a WORKING vacuum has real work to do (reached only
    # if Phase 2a did NOT abort).
    orphan_dir = sandbox / ".history" / "blobs" / "zz"
    orphan_dir.mkdir(parents=True, exist_ok=True)
    orphan = orphan_dir / "fakehashzzz.gz"
    orphan.write_bytes(b"orphan payload")
    result = store.vacuum(sandbox, dry_run=False)
    # Must NOT abort on the .yaml-named manifest directory.
    assert_true(result.get("aborted") is None,
                f"vacuum must not abort on .yaml-named source dir: "
                f"aborted={result.get('aborted')} corrupt={result.get('corrupt_manifests')}")
    assert_eq(result["corrupt_manifests"], [], "no false-corrupt from the manifest directory")
    # Orphan swept; the 3 reachable snapshots survive and still restore.
    assert_true(result["blobs_deleted"] >= 1, f"orphan blob deleted: {result}")
    assert_true(not orphan.exists(), "orphan gone")
    snaps = store.list_snapshots(file_path, sandbox)
    assert_eq(len(snaps), 3, "all 3 reachable snapshots preserved")
    for i, snap in enumerate(snaps):
        target_idx = len(snaps) - 1 - i
        expected = f"k: v{target_idx}\n".encode("utf-8")
        actual = store.restore(file_path, snap["snapshot_id"], sandbox)
        assert_eq(actual, expected, f"snap {snap['snapshot_id']} restores after vacuum")


# ---------------------------------------------------------------------------
#  — fast path must not inherit encoding=dropped after a vacuum
# ---------------------------------------------------------------------------

@with_sandbox
def fast_path_does_not_inherit_dropped_after_vacuum(sandbox, store):
    """: a metadata-only vacuum rewrites the newest manifest to
    encoding=dropped and deletes its blob. A save of UNCHANGED content must NOT
    inherit that dropped encoding (which would publish a manifest whose restore
    raises, while history-save still exits 0). It must re-anchor with a fresh
    full blob so the snapshot is restorable again."""
    file_path = _make_file(sandbox, "data.jsonl", b"")
    content = b'{"id":"a","v":1}\n{"id":"b","v":2}\n'
    store.save(file_path, content, sandbox, agent="alpha")
    snaps = store.list_snapshots(file_path, sandbox)
    assert_eq(snaps[0]["encoding"], "full", "seed snapshot is full")

    # Backdate the manifest so a metadata-only vacuum drops it.
    long_ago = time.time() - 999 * 86400
    for m_path in (sandbox / ".history" / "snapshots").rglob("*.yaml"):
        os.utime(m_path, (long_ago, long_ago))
    result = store.vacuum(sandbox, dry_run=False, metadata_only_after_days=1)
    assert_eq(result["manifests_dropped"], 1, "vacuum drops the sole manifest")
    blobs_after_vac = list((sandbox / ".history" / "blobs").rglob("*.gz"))
    assert_eq(len(blobs_after_vac), 0, "vacuum deleted the blob")

    # THE DEFECT SCENARIO: save the SAME bytes again.
    time.sleep(0.01)
    store.save(file_path, content, sandbox, agent="beta")
    snaps2 = store.list_snapshots(file_path, sandbox)
    newest = snaps2[0]
    # The new manifest must be a real, restorable snapshot — NOT dropped.
    assert_eq(newest["encoding"], "full",
              "unchanged-content save after vacuum must re-anchor as full, not inherit dropped")
    # A fresh blob must have been written.
    blobs_after_save = list((sandbox / ".history" / "blobs").rglob("*.gz"))
    assert_eq(len(blobs_after_save), 1, "re-anchor wrote a fresh full blob")
    # restore() must return the exact bytes (the goal's prescribed test).
    restored = store.restore(file_path, newest["snapshot_id"], sandbox)
    assert_eq(restored, content, "post-vacuum unchanged-content snapshot restores byte-exact")


# ---------------------------------------------------------------------------
#  — >5MB files skip the delta attempt (whole-file gzip per save)
# ---------------------------------------------------------------------------

@with_sandbox
def large_file_pure_append_uses_suffix_delta_not_full(sandbox, store):
    """ o1: a pure append to a file LARGER than 5MB must write a
    delta (suffix) entry, not a full blob. Pre-fix, the 5MB cap in can_try_delta
    made every save of such a file a whole-file gzip copy (measured: a 5.8MB
    file averaged 5.42MB per save over 2,550 saves)."""
    file_path = _make_file(sandbox, "big.jsonl", b"")
    # ~6MB baseline of hard-to-compress unique lines (51 bytes/line).
    base = b"".join(
        f"row_{i:06d}_alpha_beta_gamma_delta_{i * 97}_{i * 137}\n".encode("utf-8")
        for i in range(120000)
    )
    assert_true(len(base) > store.DEFAULT_FULL_BLOB_MAX_SIZE,
                f"baseline must exceed the 5MB cap, got {len(base)}")
    store.save(file_path, base, sandbox, agent="alpha")
    snaps = store.list_snapshots(file_path, sandbox)
    assert_eq(snaps[0]["encoding"], "full", "first snapshot is full")

    # Append one record to the >5MB file.
    appended = base + b'{"id":"next","seq":120000,"v":42}\n'
    time.sleep(0.01)
    store.save(file_path, appended, sandbox, agent="beta")
    snaps2 = store.list_snapshots(file_path, sandbox)
    newest = snaps2[0]
    assert_eq(newest["encoding"], "delta",
              f">5MB pure append must be a delta (suffix), got {newest['encoding']}")
    # The stored delta payload must be TINY (just the appended tail).
    patch = (sandbox / ".history" / "patches" /
             store._hash(appended)[:2] /
             f"{store._hash(appended)[2:]}.from.{store._hash(base)}.gz")
    assert_true(patch.exists(), f"suffix patch landed at {patch}")
    assert_true(patch.stat().st_size < 4096,
                f"suffix patch must be tiny, got {patch.stat().st_size} bytes")
    # Restore byte-exact.
    restored = store.restore(file_path, newest["snapshot_id"], sandbox)
    assert_eq(restored, appended, ">5MB append restores byte-exact")


@with_sandbox
def large_file_non_append_edit_restores_byte_exact(sandbox, store):
    """ o2 regression control: a NON-append edit to a >5MB file may
    still store a full blob (the difflib path stays size-capped — it is O(n^2)
    and rarely beats a full gzip on large non-appends), but it MUST restore
    byte-exact. Dropping the cap for appends must not break large non-appends."""
    file_path = _make_file(sandbox, "big2.jsonl", b"")
    base = b"".join(
        f"row_{i:06d}_gamma_delta_epsilon_{i * 131}_{i * 71}\n".encode("utf-8")
        for i in range(120000)
    )
    assert_true(len(base) > store.DEFAULT_FULL_BLOB_MAX_SIZE,
                f"baseline must exceed the 5MB cap, got {len(base)}")
    store.save(file_path, base, sandbox, agent="alpha")
    # Mutate the FIRST line — a non-append edit.
    lines = base.split(b"\n")
    lines[0] = b"row_000000_MUTATED_non_append_edit"
    mutated = b"\n".join(lines)
    assert_true(not mutated.startswith(base), "mutation is not a pure append")
    time.sleep(0.01)
    store.save(file_path, mutated, sandbox, agent="beta")
    snaps = store.list_snapshots(file_path, sandbox)
    newest = snaps[0]
    restored = store.restore(file_path, newest["snapshot_id"], sandbox)
    assert_eq(restored, mutated, ">5MB non-append edit restores byte-exact")


# ---------------------------------------------------------------------------
#  — Windows 259-char temp path drops delta saves (forced-limit tests)
# ---------------------------------------------------------------------------
#
# The defect: _unique_tmp() used to name the temp file <target name> + a
# 26-27 char suffix. On the ZDS Windows box (LongPathsEnabled=0) the FINAL
# patch path was 236 chars — under 259 — but the temp path was 261-263, so
# open() raised FileNotFoundError and 9,593 delta saves were silently dropped
# (full snapshots on shorter paths still landed). This box is Linux with no
# MAX_PATH enforcement, so the tests FORCE the limit: a guarded open() refuses
# paths over 259 chars (the exact cross-platform clause the goal prescribes).
#
# Path geometry (chosen so the FORCED limit reproduces the ZDS failure shape
# on any box): the store base_dir is a directory whose ABSOLUTE path is
# exactly _DEEP_BASE_LEN chars (computed dynamically, nested under the temp
# sandbox). Suffix lengths (measured on this box, 7-digit pid): patch =
# base + 156 ("/.history/patches/aa/62.from.64.gz"); blob = base + 84;
# manifest = base + 69 ("store.jsonl/26-char-ts_agent.yaml"). With base = 103:
#   final patch path  = 259            (fits, exactly at the limit; headroom
#                                       0 < the 26 the goal names)
#   old tmp patch path = 259 + 29      = 288 (OVER -> refused: the ZDS
#                                       failure shape, 27-28 with a shorter pid)
#   blob final path   = 187; old blob tmp = 187 + 29 = 216 (fits — matches
#                       ZDS where full snapshots DID land while delta temps
#                       overflowed)
#   new (fixed) tmp   = 146 (short name in the target's own dir)
#   manifest path     = 172 (fits)
# ---------------------------------------------------------------------------

_WINDOWS_MAX_PATH = 259
_DEEP_BASE_LEN = 103


def _force_max_path_limit(limit=_WINDOWS_MAX_PATH):
    """Context manager: make open() refuse string paths longer than `limit`
    chars, simulating the Windows MAX_PATH (259) refusal on Linux.

    BOTH builtins.open and io.open are patched, because pathlib's
    Path.open/write_bytes/read_bytes call io.open DIRECTLY (the C
    implementation), not the builtins alias — patching only builtins.open
    would leave the store's Path-based writes unguarded (the guard would be
    vacuous). Mirrors the OS behavior the defect depends on: open() raises
    FileNotFoundError for the over-long path; syscalls that don't go through
    open (mkdir/stat/replace/unlink) are untouched, exactly as on Windows
    (where MAX_PATH historically applies to the path used by the C runtime
    open)."""
    import builtins
    import io as _io
    real_builtins_open = builtins.open
    real_io_open = _io.open

    def guarded_open(file, *args, **kwargs):
        try:
            p = os.fspath(file)
        except TypeError:
            p = None
        if isinstance(p, str) and len(p) > limit:
            raise FileNotFoundError(
                3, f"[simulated Windows MAX_PATH] path over {limit} chars", p)
        return real_builtins_open(file, *args, **kwargs)

    class _Guard:
        def __enter__(self):
            builtins.open = guarded_open
            _io.open = guarded_open
            return self
        def __exit__(self, *exc):
            builtins.open = real_builtins_open
            _io.open = real_io_open
            return False
    return _Guard()


def _deep_base_dir(sandbox, target_len=_DEEP_BASE_LEN):
    """Create a store base_dir whose absolute path is exactly target_len chars
    (one long-named dir under the temp sandbox), and return it."""
    prefix = str(sandbox) + os.sep
    name_len = target_len - len(prefix)
    assert_true(name_len >= 1,
                f"sandbox prefix {len(prefix)} leaves no room for a "
                f"{target_len}-char base dir")
    base = sandbox / ("p" * name_len)
    base.mkdir(parents=True, exist_ok=False)
    assert_eq(len(str(base)), target_len, "base_dir path is exactly the target length")
    return base


def _old_unique_tmp(target):
    """The PRE-FIX _unique_tmp (reproduced verbatim): the target's name plus a
    26-27 char suffix — the exact naming that overflowed MAX_PATH on Windows."""
    suffix = f".{os.getpid()}-{os.urandom(8).hex()}.tmp"
    return target.with_suffix(target.suffix + suffix)


def _patch_path_for(store, sandbox, current, base):
    return (sandbox / ".history" / "patches" /
            store._hash(current)[:2] /
            f"{store._hash(current)[2:]}.from.{store._hash(base)}.gz")


@with_sandbox
def forced_max_path_delta_saves_and_restores_byte_for_byte(sandbox, store):
    """ o4: with the patch path in the <26-headroom band below 259
    and open() forced to refuse >259-char paths, the FIXED code (short tmp name
    in the target's own dir) still saves the delta and restores byte-for-byte.
    Under the pre-fix naming the SAME save's temp open would be refused (see
    the positive control below)."""
    base_dir = _deep_base_dir(sandbox)
    file_path = base_dir / "store.jsonl"
    content_base = b"".join(
        f"line_{i:04d}_zeta_eta_theta_{i * 83}_{i * 113}\n".encode("utf-8")
        for i in range(40)
    )
    store.save(file_path, content_base, base_dir, agent="alpha")

    # Confirm the geometry is the ZDS failure band before the assertion that
    # matters: final patch path fits under 259 with <26 chars of headroom.
    appended = content_base + b'{"id":"tail","seq":40}\n'
    patch = _patch_path_for(store, base_dir, appended, content_base)
    final_len = len(str(patch))
    assert_true(final_len <= _WINDOWS_MAX_PATH,
                f"final patch path must fit: {final_len}")
    assert_true(_WINDOWS_MAX_PATH - final_len < 26,
                f"final patch path must leave <26 chars of headroom (the band "
                f"the goal names), got {259 - final_len}")

    time.sleep(0.01)
    with _force_max_path_limit():
        store.save(file_path, appended, base_dir, agent="beta")
    snaps = store.list_snapshots(file_path, base_dir)
    newest = snaps[0]
    assert_eq(newest["encoding"], "delta", "delta saved despite the forced limit")
    restored = store.restore(file_path, newest["snapshot_id"], base_dir)
    assert_eq(restored, appended, "content restores byte-for-byte")


@with_sandbox
def forced_max_path_delta_write_raises_falls_back_to_full(sandbox, store):
    """ o5: when the DELTA write raises (forced limit + the pre-fix
    long tmp naming, whose temp path really does overflow it while the final
    name fits), save() must NOT drop the snapshot: it records ok=true with a
    FULL snapshot instead of ok=false with nothing. The full-blob path stays
    under the limit here (as on ZDS, where fulls still landed)."""
    base_dir = _deep_base_dir(sandbox)
    file_path = base_dir / "store2.jsonl"
    content_base = b"".join(
        f"line_{i:04d}_iota_kappa_lambda_{i * 67}_{i * 97}\n".encode("utf-8")
        for i in range(40)
    )
    store.save(file_path, content_base, base_dir, agent="alpha")
    appended = content_base + b'{"id":"tail","seq":40}\n'
    patch = _patch_path_for(store, base_dir, appended, content_base)

    # Pre-fix long tmp naming so the delta temp path genuinely overflows the
    # forced limit (final name fits, only the tmp does — the ZDS shape).
    orig_unique_tmp = store._unique_tmp
    store._unique_tmp = _old_unique_tmp
    try:
        time.sleep(0.01)
        with _force_max_path_limit():
            store.save(file_path, appended, base_dir, agent="beta")
    finally:
        store._unique_tmp = orig_unique_tmp

    snaps = store.list_snapshots(file_path, base_dir)
    newest = snaps[0]
    # A snapshot MUST exist and restore byte-exact (ok=true semantics).
    restored = store.restore(file_path, newest["snapshot_id"], base_dir)
    assert_eq(restored, appended, "fallback snapshot restores byte-exact")
    # Proof the fallback fired: the overflowing delta patch is absent, and a
    # full blob for the new content landed.
    assert_true(not patch.exists(),
                "the overflowing delta patch was NOT written (delta write raised)")
    blob = (base_dir / ".history" / "blobs" /
            store._hash(appended)[:2] /
            f"{store._hash(appended)[2:]}.gz")
    assert_true(blob.exists(), "fallback wrote a full blob (ok=true, not a drop)")


@with_sandbox
def forced_max_path_positive_control_old_scheme_raises(sandbox, store):
    """ o6 POSITIVE CONTROL: the test is not vacuous. Under the
    forced limit, the PRE-FIX tmp naming (reproduced verbatim) makes the delta
    write raise FileNotFoundError and leaves no patch on disk — the exact
    ok=false / no-snapshot loss the goal describes (pre-fix code had no
    fallback, so the exception propagated to the telemetry layer as
    ok=false; here we exercise the identical write call directly)."""
    base_dir = _deep_base_dir(sandbox)
    content_base = b"".join(
        f"line_{i:04d}_mu_nu_xi_{i * 53}_{i * 73}\n".encode("utf-8")
        for i in range(40)
    )
    content_new = content_base + b'{"id":"tail","seq":40}\n'
    patch = _patch_path_for(store, base_dir, content_new, content_base)
    final_len = len(str(patch))
    assert_true(final_len <= _WINDOWS_MAX_PATH, f"final name fits: {final_len}")

    # Document the failure shape: old tmp over the limit, final name under it.
    old_tmp_len = final_len + len(f".{os.getpid()}-") + 16 + len(".tmp") + 1
    assert_true(old_tmp_len > _WINDOWS_MAX_PATH,
                f"old tmp path must overflow the limit: {old_tmp_len}")

    orig_unique_tmp = store._unique_tmp
    # Phase 1 (positive control): old tmp scheme under the forced limit.
    store._unique_tmp = _old_unique_tmp
    try:
        with _force_max_path_limit():
            try:
                store._atomic_write_bytes(patch, b"delta-payload")
            except FileNotFoundError as e:
                exc = e
            else:
                raise AssertionError(
                    "positive control failed: old tmp scheme should raise "
                    "FileNotFoundError under the forced limit")
    finally:
        store._unique_tmp = orig_unique_tmp
    # The exception is the recorded pre-fix signal (ok=false, FileNotFoundError).
    assert_true(isinstance(exc, FileNotFoundError), "pre-fix signal: FileNotFoundError")
    assert_true(not patch.exists(), "pre-fix: no patch landed (the lost snapshot)")
    # Phase 2: the FIXED short tmp name does NOT overflow — the same write
    # succeeds under the identical forced limit.
    fixed_tmp_len = len(str(patch.parent)) + 1 + 22
    assert_true(fixed_tmp_len <= _WINDOWS_MAX_PATH,
                f"fixed tmp path must fit: {fixed_tmp_len}")
    with _force_max_path_limit():
        store._atomic_write_bytes(patch, b"delta-payload")
    assert_true(patch.exists(), "fixed short tmp: same write lands")
    assert_eq(patch.read_bytes(), b"delta-payload", "fixed write content intact")


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
    # -a regression (2026-07-20): .yaml-named source dir must not abort vacuum
    vacuum_skips_yaml_named_source_dir,
    #  regression (2026-10-01): fast path must not inherit encoding=dropped
    fast_path_does_not_inherit_dropped_after_vacuum,
    #  regressions (2026-10-01): >5MB appends get a suffix delta
    large_file_pure_append_uses_suffix_delta_not_full,
    large_file_non_append_edit_restores_byte_exact,
    #  regressions (2026-10-01): forced 259-char limit (cross-platform)
    forced_max_path_delta_saves_and_restores_byte_for_byte,
    forced_max_path_delta_write_raises_falls_back_to_full,
    forced_max_path_positive_control_old_scheme_raises,
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
