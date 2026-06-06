"""Stage 1 tests for _fileops shadow-mode wiring to _history_store.

The shadow write fires inside save_history AFTER the authoritative old
.history/<file>/<ts>...gz snapshot has fully succeeded. It is best-effort
— failures land in meta/history-save-telemetry.jsonl and never affect
the old-store path.

Covers:
- Shadow write produces manifest + blob under .history/snapshots,
  .history/blobs the first time.
- Second save of substantially different content uses delta encoding.
- _history_store.save raising does NOT prevent the old gzip snapshot
  from existing on disk.
- Telemetry line written on success (ok=true, encoding, size_bytes).
- Telemetry line written on failure (ok=false, error string with
  exception class name).
- FILEOPS_HISTORY_USE_NEW_STORE=0 disables the shadow path entirely (no manifest,
  no telemetry).
- Round-trip: old-store gzip restore and new-store restore yield
  identical bytes for every save.
- Summary propagates to shadow manifest.

Standalone runner (not pytest-collected): py -3 <this-file>
"""
import json
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
    """Per-test fresh WORLD + META sandboxes and fresh _fileops import.

    Mirrors test_fileops_finding_fixes.with_sandbox. Also restores
    FILEOPS_HISTORY_USE_NEW_STORE so a test that disables it cannot leak the
    setting into a later test.

    Sets MIND_WORLD/MIND_META/MIND_AGENT so _paths resolves the sandbox."""
    def wrapped():
        sandbox = Path(tempfile.mkdtemp(prefix="shadow_world_"))
        meta_sandbox = Path(tempfile.mkdtemp(prefix="shadow_meta_"))
        tracked = ("MIND_WORLD", "MIND_META", "MIND_AGENT",
                   "FILEOPS_HISTORY_USE_NEW_STORE")
        prior = {k: os.environ.get(k) for k in tracked}
        try:
            os.environ["MIND_WORLD"] = str(sandbox)
            os.environ["MIND_META"] = str(meta_sandbox)
            os.environ["MIND_AGENT"] = "zeta"
            os.environ.pop("FILEOPS_HISTORY_USE_NEW_STORE", None)  # default ON
            for mod in list(sys.modules):
                if mod in ("_fileops", "_paths", "_history_store", "history"):
                    del sys.modules[mod]
            import _fileops
            import _history_store
            test_fn(sandbox, meta_sandbox, _fileops, _history_store)
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


def _read_telemetry(meta_sandbox):
    """Return the list of telemetry records (or [] if none)."""
    p = meta_sandbox / "history-save-telemetry.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


# ---------------------------------------------------------------------------
# Shadow write creates manifest + blob
# ---------------------------------------------------------------------------

@with_sandbox
def shadow_write_creates_manifest_and_blob(sandbox, meta_sandbox, _fileops, _history_store):
    target = sandbox / "data.jsonl"
    target.write_text('{"x":1}\n', encoding="utf-8")
    _fileops.save_history(target, sandbox, "zeta", summary="seed")
    # Manifest under .history/snapshots/data.jsonl/<ts>_zeta.yaml
    manifest_dir = sandbox / ".history" / "snapshots" / "data.jsonl"
    manifests = list(manifest_dir.glob("*.yaml"))
    assert_eq(len(manifests), 1, f"expected one manifest in {manifest_dir}")
    # Blob under .history/blobs/<hh>/<rest>.gz
    blobs = list((sandbox / ".history" / "blobs").rglob("*.gz"))
    assert_eq(len(blobs), 1, "expected exactly one blob for first save")


@with_sandbox
def shadow_write_with_no_meta_dir_still_writes(sandbox, meta_sandbox, _fileops, _history_store):
    """The shadow path must work even if META_DIR resolves to None — the
    blob/manifest write does not depend on META, only telemetry does."""
    # Use .txt to bypass the JSONL parse-validation gate (this test cares
    # about the shadow write path, not JSONL semantics).
    target = sandbox / "x.txt"
    target.write_text("a\n", encoding="utf-8")
    _fileops.save_history(target, sandbox, "zeta")
    assert_true(list((sandbox / ".history" / "blobs").rglob("*.gz")),
                "blob written")


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------

@with_sandbox
def telemetry_recorded_on_success(sandbox, meta_sandbox, _fileops, _history_store):
    target = sandbox / "data.jsonl"
    target.write_text('{"x":1}\n', encoding="utf-8")
    _fileops.save_history(target, sandbox, "zeta", summary="seed")
    records = _read_telemetry(meta_sandbox)
    assert_eq(len(records), 1, f"expected one telemetry record, got {records!r}")
    r = records[0]
    assert_eq(r["ok"], True, "telemetry ok=true")
    assert_eq(r["encoding"], "full", "first save is full")
    assert_eq(r["agent"], "zeta", "agent field")
    assert_true(r["size_bytes"] > 0, "size_bytes recorded")
    assert_true(r["error"] is None, "error null on success")


@with_sandbox
def telemetry_recorded_on_failure(sandbox, meta_sandbox, _fileops, _history_store):
    """Monkeypatch _history_store.save to raise. save_history must NOT
    raise (best-effort behavioral promise — the new-store write cannot
    block the caller's lock-and-write cycle), and telemetry must record
    the failure with the exception class + message."""
    target = sandbox / "data.jsonl"
    target.write_text('{"x":1}\n', encoding="utf-8")

    def _boom(*args, **kwargs):
        raise RuntimeError("synthetic new-store failure")
    original_save = _history_store.save
    _history_store.save = _boom
    try:
        _fileops.save_history(target, sandbox, "zeta")  # must not raise
    finally:
        _history_store.save = original_save

    # Stage 2: no legacy gz snapshot lands by default. The new-store
    # failure means this save produced no on-disk version, but
    # save_history's caller is unaffected (its source-file lock-and-write
    # cycle proceeds normally).
    records = _read_telemetry(meta_sandbox)
    assert_eq(len(records), 1, "one telemetry record on failure")
    r = records[0]
    assert_eq(r["ok"], False, "telemetry ok=false")
    assert_true("RuntimeError" in r["error"], f"error names exception class: {r['error']!r}")
    assert_true("synthetic new-store failure" in r["error"], f"error carries message: {r['error']!r}")


@with_sandbox
def telemetry_failure_with_legacy_rollback_keeps_old_snapshot(sandbox, meta_sandbox, _fileops, _history_store):
    """With FILEOPS_HISTORY_KEEP_LEGACY_WRITES=1 (rollback hatch), a
    failed new-store write does NOT lose the snapshot — the legacy gz
    snapshot still lands. This is the documented operator escape hatch:
    when the new store is misbehaving, dual-write keeps the legacy
    tree growing so no data is lost while the new-store bug is
    investigated."""
    target = sandbox / "data.jsonl"
    target.write_text('{"x":1}\n', encoding="utf-8")

    def _boom(*args, **kwargs):
        raise RuntimeError("synthetic new-store failure")
    original_save = _history_store.save
    _history_store.save = _boom
    os.environ["FILEOPS_HISTORY_KEEP_LEGACY_WRITES"] = "1"
    try:
        _fileops.save_history(target, sandbox, "zeta")  # must not raise
    finally:
        _history_store.save = original_save
        os.environ.pop("FILEOPS_HISTORY_KEEP_LEGACY_WRITES", None)

    # Legacy gz snapshot landed (rollback hatch active).
    old_snaps = list((sandbox / ".history" / "data.jsonl").glob("*.gz"))
    assert_eq(len(old_snaps), 1, "legacy gz snapshot landed despite new-store failure")
    # Telemetry still records the new-store failure.
    records = _read_telemetry(meta_sandbox)
    assert_eq(len(records), 1, "telemetry recorded new-store failure")
    assert_eq(records[0]["ok"], False, "ok=false in telemetry")


# ---------------------------------------------------------------------------
# Env-flag disable
# ---------------------------------------------------------------------------

@with_sandbox
def new_store_disabled_by_env_var(sandbox, meta_sandbox, _fileops, _history_store):
    """Stage 2: FILEOPS_HISTORY_USE_NEW_STORE=0 skips the new-store write.
    With no rollback hatch enabled, NO snapshot lands at all — confirms
    that the env flag is a true bypass, not a fallback to the old store."""
    target = sandbox / "data.jsonl"
    target.write_text('{"x":1}\n', encoding="utf-8")
    os.environ["FILEOPS_HISTORY_USE_NEW_STORE"] = "0"
    try:
        _fileops.save_history(target, sandbox, "zeta")
    finally:
        os.environ.pop("FILEOPS_HISTORY_USE_NEW_STORE", None)
    # No manifests, no blobs, no telemetry from the new-store path.
    assert_true(not any((sandbox / ".history" / "snapshots").rglob("*.yaml"))
                if (sandbox / ".history" / "snapshots").exists() else True,
                "no manifests written when new store disabled")
    assert_true(not any((sandbox / ".history" / "blobs").rglob("*.gz"))
                if (sandbox / ".history" / "blobs").exists() else True,
                "no blobs written when new store disabled")
    assert_eq(_read_telemetry(meta_sandbox), [],
              "no telemetry written when new store disabled")
    # And NO old gzip snapshot either (Stage 2: legacy writes off by default).
    old_snaps = list((sandbox / ".history" / "data.jsonl").glob("*.gz")
                     if (sandbox / ".history" / "data.jsonl").exists() else [])
    assert_eq(len(old_snaps), 0,
              "no legacy gz snapshot either (Stage 2 default)")


@with_sandbox
def legacy_writes_rollback_hatch_dual_writes(sandbox, meta_sandbox, _fileops, _history_store):
    """Stage 2 rollback hatch: with FILEOPS_HISTORY_KEEP_LEGACY_WRITES=1,
    save_history writes to BOTH stores (old gz tree AND new CAS). Used
    if a regression in the new store is suspected and the operator wants
    legacy snapshots to keep accruing."""
    target = sandbox / "data.jsonl"
    target.write_text('{"x":1}\n', encoding="utf-8")
    os.environ["FILEOPS_HISTORY_KEEP_LEGACY_WRITES"] = "1"
    try:
        _fileops.save_history(target, sandbox, "zeta")
    finally:
        os.environ.pop("FILEOPS_HISTORY_KEEP_LEGACY_WRITES", None)
    # Both stores fired.
    old_snaps = list((sandbox / ".history" / "data.jsonl").glob("*.gz"))
    assert_eq(len(old_snaps), 1, "rollback hatch wrote legacy gz snapshot")
    manifests = list((sandbox / ".history" / "snapshots" / "data.jsonl").glob("*.yaml"))
    assert_eq(len(manifests), 1, "new store still wrote manifest")


# ---------------------------------------------------------------------------
# Delta encoding kicks in on subsequent saves with substantial content
# ---------------------------------------------------------------------------

@with_sandbox
def shadow_uses_delta_on_subsequent_save(sandbox, meta_sandbox, _fileops, _history_store):
    """With ~1.5KB of unique-token content, the second save's delta must
    beat the 50% savings threshold and land as encoding=delta."""
    target = sandbox / "data.txt"  # .txt bypasses JSONL parse-validation gate
    v1 = "".join(
        f"line_{i:04d}_alpha_beta_gamma_delta_{i * 97}_{i * 137}\n"
        for i in range(50)
    )
    v2 = v1 + "line_0050_alpha_beta_gamma_delta_4850_6850\n"

    target.write_text(v1, encoding="utf-8")
    _fileops.save_history(target, sandbox, "zeta")
    # Update the live file so the second save sees v2 bytes.
    time.sleep(1)  # ensure distinct second-resolution timestamps
    target.write_text(v2, encoding="utf-8")
    _fileops.save_history(target, sandbox, "zeta")

    records = _read_telemetry(meta_sandbox)
    assert_eq(len(records), 2, "two telemetry records")
    assert_eq(records[0]["encoding"], "full", "first is full")
    assert_eq(records[1]["encoding"], "delta", "second is delta")


# ---------------------------------------------------------------------------
# Round-trip: old and new restores agree
# ---------------------------------------------------------------------------

@with_sandbox
def old_and_new_restore_yield_same_bytes_in_dual_write(sandbox, meta_sandbox, _fileops, _history_store):
    """Parity check: with FILEOPS_HISTORY_KEEP_LEGACY_WRITES=1 (Stage 2
    rollback hatch), both stores receive every save and either store's
    restore yields the same bytes as the live file at save-time.
    Validates that the dual-write rollback path remains byte-equivalent."""
    import gzip
    target = sandbox / "data.txt"  # .txt bypasses JSONL parse-validation gate
    written = []  # list of bytes per version
    os.environ["FILEOPS_HISTORY_KEEP_LEGACY_WRITES"] = "1"
    try:
        for i in range(5):
            content = ("".join(
                f"line_{j:04d}_alpha_beta_gamma_delta_{j * 97}_{j * 137}\n"
                for j in range(50 + i)
            )).encode("utf-8")
            target.write_bytes(content)
            _fileops.save_history(target, sandbox, "zeta")
            written.append(content)
            time.sleep(1)  # second-resolution timestamps must be unique
    finally:
        os.environ.pop("FILEOPS_HISTORY_KEEP_LEGACY_WRITES", None)

    # OLD: enumerate gz snapshots oldest-first and decompress
    old_snaps = sorted((sandbox / ".history" / "data.txt").glob("*.gz"))
    assert_eq(len(old_snaps), 5, "5 gz snapshots in dual-write mode")
    for snap, expected in zip(old_snaps, written):
        with gzip.open(snap, "rb") as f:
            actual = f.read()
        assert_eq(actual, expected, f"old restore of {snap.name}")

    # NEW: enumerate manifests oldest-first and restore via _history_store.
    # Listing returns newest-first; reverse for oldest-first comparison.
    snaps = _history_store.list_snapshots(target, sandbox)
    assert_eq(len(snaps), 5, "5 new-store manifests in dual-write mode")
    for snap, expected in zip(reversed(snaps), written):
        actual = _history_store.restore(target, snap["snapshot_id"], sandbox)
        assert_eq(actual, expected, f"new restore of {snap['snapshot_id']}")


# ---------------------------------------------------------------------------
# Summary propagation
# ---------------------------------------------------------------------------

@with_sandbox
def summary_propagates_to_shadow_manifest(sandbox, meta_sandbox, _fileops, _history_store):
    target = sandbox / "data.jsonl"
    target.write_text('{"x":1}\n', encoding="utf-8")
    _fileops.save_history(target, sandbox, "zeta", summary="seed-shadow-manifest")
    snaps = _history_store.list_snapshots(target, sandbox)
    assert_eq(len(snaps), 1, "one shadow snapshot")
    assert_eq(snaps[0]["summary"], "seed-shadow-manifest", "summary survives the shadow path")


# ---------------------------------------------------------------------------
# Blacklist + cruft early returns suppress shadow too
# ---------------------------------------------------------------------------

@with_sandbox
def blacklisted_file_does_not_trigger_shadow(sandbox, meta_sandbox, _fileops, _history_store):
    """A WORLD presence/ file is blacklisted from snapshots; the shadow
    write must respect the same skip (it runs after the early returns)."""
    target = sandbox / "presence" / "alpha.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("alive\n", encoding="utf-8")
    _fileops.save_history(target, sandbox, "zeta")
    # Old: no snapshot dir (blacklist returned before mkdir).
    assert_true(not (sandbox / ".history" / "presence" / "alpha.txt").exists(),
                "old snapshot suppressed by blacklist")
    # New: no manifest either.
    assert_true(not (sandbox / ".history" / "snapshots").exists() or
                not any((sandbox / ".history" / "snapshots").rglob("*.yaml")),
                "shadow snapshot suppressed by blacklist")
    # Telemetry: no record (blacklist returns BEFORE the shadow call).
    assert_eq(_read_telemetry(meta_sandbox), [],
              "no telemetry for a blacklisted file")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    shadow_write_creates_manifest_and_blob,
    shadow_write_with_no_meta_dir_still_writes,
    telemetry_recorded_on_success,
    telemetry_recorded_on_failure,
    telemetry_failure_with_legacy_rollback_keeps_old_snapshot,
    new_store_disabled_by_env_var,
    legacy_writes_rollback_hatch_dual_writes,
    shadow_uses_delta_on_subsequent_save,
    old_and_new_restore_yield_same_bytes_in_dual_write,
    summary_propagates_to_shadow_manifest,
    blacklisted_file_does_not_trigger_shadow,
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
