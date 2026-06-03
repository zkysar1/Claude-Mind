#!/usr/bin/env python3
"""Stage 2 (2026-05-22): history.py CLI must merge legacy gz + new CAS-delta
stores. cmd_list shows both with [old]/[new] tags; cmd_restore and cmd_diff
dispatch on the snapshot source.

Also covers the cross-store read merging in _fileops._find_history_snapshots:
that helper now backs both the CLI commands AND read_jsonl_with_recovery, so
correct merge order (newest-first by parsed datetime) is critical.

Standalone runner — not pytest-collected. Invoke via:
    py -3 core/scripts/tests/test_history_cli_stage2.py
"""

import argparse
import gzip
import io
import os
import shutil
import subprocess
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
    """Per-test sandbox: tempdir as WORLD, second tempdir as META.

    Sets BOTH MIND_* and MIND_* env var families so the same test file
    runs in Ayoai-Mind (reads MIND_*) and Zak-Data-Solutions-Mind (reads
    MIND_*) without modification. The unused family is a no-op in the
    consuming repo's _paths.py.
    """
    world = Path(tempfile.mkdtemp(prefix="hist_cli_stage2_world_"))
    meta = Path(tempfile.mkdtemp(prefix="hist_cli_stage2_meta_"))
    tracked = ("MIND_WORLD", "MIND_META", "MIND_WORLD", "MIND_META",
               "FILEOPS_HISTORY_USE_NEW_STORE",
               "FILEOPS_HISTORY_KEEP_LEGACY_WRITES")
    prior = {k: os.environ.get(k) for k in tracked}
    os.environ["MIND_WORLD"] = str(world)
    os.environ["MIND_META"] = str(meta)
    os.environ["MIND_WORLD"] = str(world)
    os.environ["MIND_META"] = str(meta)
    # Reload fresh modules so they pick up the new env.
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


def _write_file(world, rel, content_bytes):
    """Write rel under world with content_bytes."""
    target = world / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content_bytes)
    return target


def _save_legacy_gz(world, rel, ts_str, agent, content_bytes):
    """Plant a legacy gz snapshot directly under .history/<rel>/."""
    snap_dir = world / ".history" / rel
    snap_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(rel).suffix
    name = f"{ts_str}_{agent}{ext}.gz"
    path = snap_dir / name
    with gzip.open(path, "wb") as f:
        f.write(content_bytes)
    return path


def _save_new_store(world, rel, agent, content_bytes, summary=""):
    """Save via _history_store.save into the CAS-delta tree."""
    import _history_store
    full = world / rel
    if not full.parent.exists():
        full.parent.mkdir(parents=True, exist_ok=True)
    return _history_store.save(
        full, content_bytes, str(world), agent, summary=summary,
    )


@contextmanager
def _capture():
    """Capture both stdout and stderr for one CLI invocation."""
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        yield out, err


def _run(cmd_argv):
    """Invoke history.main() with cmd_argv as sys.argv."""
    import history
    saved = sys.argv
    try:
        sys.argv = ["history.py"] + cmd_argv
        history.main()
    finally:
        sys.argv = saved


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@test
def find_history_snapshots_merges_both_stores():
    """Stage 2: legacy gz + new manifests merge into one newest-first list."""
    with sandbox() as world:
        target = _write_file(world, "data.txt", b"current content")

        # Plant a legacy gz dated 10s ago and a new-store manifest now.
        old_ts = time.strftime("%Y-%m-%dT%H-%M-%S",
                               time.localtime(time.time() - 10))
        legacy = _save_legacy_gz(world, "data.txt", old_ts, "alpha",
                                 b"old content")
        new_manifest = _save_new_store(world, "data.txt", "beta",
                                       b"newer content")

        import _fileops
        snaps = _fileops._find_history_snapshots(target)
        names = [s.name for s in snaps]
        assert len(snaps) == 2, f"expected 2 snapshots, got {len(snaps)}: {names}"
        # Newest first — new-store manifest written now > legacy 10s ago
        assert snaps[0] == new_manifest, f"newest should be {new_manifest.name}, got {snaps[0].name}"
        assert snaps[1] == legacy, f"second should be legacy, got {snaps[1].name}"


@test
def find_history_snapshots_sorts_by_parsed_datetime_not_lex():
    """Lex sort across '.<microseconds>' vs '_' would put new BEFORE old
    even when old is newer. The merger must parse and sort by datetime."""
    with sandbox() as world:
        target = _write_file(world, "data.txt", b"current")
        # Plant a new-store manifest with timestamp 100s in the past, then
        # plant a legacy gz with timestamp now. By datetime the legacy wins,
        # by lex order on filename the new wins (because '.' < '_').
        import _history_store

        # First save (will land in new store with "now" timestamp - 100s? No,
        # _history_store uses datetime.now() — we can't backdate from outside.
        # Workaround: write new store first, then legacy second so it's newer.
        new_manifest = _save_new_store(world, "data.txt", "alpha", b"new content earlier")
        time.sleep(1.1)  # ensure legacy ts > new ts
        late_ts = time.strftime("%Y-%m-%dT%H-%M-%S")
        legacy = _save_legacy_gz(world, "data.txt", late_ts, "beta",
                                 b"old content later")

        import _fileops
        snaps = _fileops._find_history_snapshots(target)
        assert len(snaps) == 2
        assert snaps[0] == legacy, (
            f"datetime-newer legacy should sort first, got {snaps[0].name}"
        )
        assert snaps[1] == new_manifest


@test
def cmd_list_shows_both_stores_tagged():
    """cmd_list emits [old] and [new] tags so the user sees the store split."""
    with sandbox() as world:
        _write_file(world, "data.txt", b"current")
        old_ts = time.strftime("%Y-%m-%dT%H-%M-%S",
                               time.localtime(time.time() - 10))
        _save_legacy_gz(world, "data.txt", old_ts, "alpha", b"old")
        _save_new_store(world, "data.txt", "beta", b"newer", summary="stage2")

        with _capture() as (out, err):
            _run(["list", str(world / "data.txt")])
        text = out.getvalue()
        assert "[new]" in text, f"missing [new] tag: {text}"
        assert "[old]" in text, f"missing [old] tag: {text}"
        assert "beta" in text and "alpha" in text
        assert "stage2" in text, f"new-store summary not shown: {text}"
        # 2 versions reported
        assert "2 versions" in text, text


@test
def cmd_list_no_history():
    """cmd_list when nothing exists for the file."""
    with sandbox() as world:
        _write_file(world, "nope.txt", b"x")
        with _capture() as (out, err):
            _run(["list", str(world / "nope.txt")])
        assert "No history" in out.getvalue()


@test
def cmd_restore_dispatches_to_new_store():
    """Restoring a new-store manifest reconstructs from the CAS chain."""
    with sandbox() as world:
        target = _write_file(world, "data.txt", b"original v1")
        # First snapshot of v1
        m1 = _save_new_store(world, "data.txt", "alpha", b"original v1",
                             summary="v1")
        # Overwrite then snapshot v2
        target.write_bytes(b"changed v2")
        _save_new_store(world, "data.txt", "alpha", b"changed v2", summary="v2")

        # Current on-disk = v2; restore manifest m1 (v1)
        with _capture() as (out, err):
            _run(["restore", str(target), m1.name])
        assert target.read_bytes() == b"original v1", (
            f"restore from new store did not recover v1: got {target.read_bytes()!r}"
        )


@test
def cmd_restore_dispatches_to_legacy():
    """Restoring a legacy gz still works after the Stage-2 helper changes."""
    with sandbox() as world:
        target = _write_file(world, "data.txt", b"current")
        ts = time.strftime("%Y-%m-%dT%H-%M-%S",
                           time.localtime(time.time() - 10))
        legacy = _save_legacy_gz(world, "data.txt", ts, "alpha",
                                 b"legacy content")

        with _capture() as (out, err):
            _run(["restore", str(target), legacy.name])
        assert target.read_bytes() == b"legacy content"


@test
def cmd_restore_unknown_version_lists_all():
    """Unknown version name lists ALL snapshots from BOTH stores in error."""
    with sandbox() as world:
        target = _write_file(world, "data.txt", b"current")
        ts = time.strftime("%Y-%m-%dT%H-%M-%S",
                           time.localtime(time.time() - 10))
        legacy = _save_legacy_gz(world, "data.txt", ts, "alpha", b"old")
        m = _save_new_store(world, "data.txt", "beta", b"newer")

        with _capture() as (out, err):
            try:
                _run(["restore", str(target), "does-not-exist.yaml"])
            except SystemExit as e:
                assert e.code == 1
        msg = err.getvalue()
        assert legacy.name in msg, f"legacy snap not listed: {msg}"
        assert m.name in msg, f"new-store manifest not listed: {msg}"
        assert "[new]" in msg and "[old]" in msg, f"missing tags: {msg}"


@test
def cmd_diff_against_new_store_manifest():
    """cmd_diff reconstructs new-store content via _history_store.restore."""
    with sandbox() as world:
        target = _write_file(world, "data.txt", b"line one\nline two\n")
        m = _save_new_store(world, "data.txt", "alpha", b"line one\nline two\n")
        # Mutate the live file
        target.write_bytes(b"line one\nline two MODIFIED\nline three\n")

        with _capture() as (out, err):
            _run(["diff", str(target), m.name])
        text = out.getvalue()
        assert "line two MODIFIED" in text or "+line two MODIFIED" in text, text
        assert "line three" in text, text


@test
def cmd_diff_against_legacy_gz():
    """cmd_diff still reads legacy gz when version_name points there."""
    with sandbox() as world:
        target = _write_file(world, "data.txt", b"a\nb\n")
        ts = time.strftime("%Y-%m-%dT%H-%M-%S",
                           time.localtime(time.time() - 10))
        legacy = _save_legacy_gz(world, "data.txt", ts, "alpha", b"a\nb\n")
        target.write_bytes(b"a\nb\nc\n")

        with _capture() as (out, err):
            _run(["diff", str(target), legacy.name])
        assert "+c" in out.getvalue()


@test
def cmd_diff_through_delta_chain():
    """cmd_diff against an OLDER manifest reconstructs via patches."""
    with sandbox() as world:
        target = world / "data.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        v1 = b"alpha bravo charlie\n" * 80
        v2 = b"alpha bravo CHARLIE\n" * 80  # tiny diff from v1
        v3 = b"alpha bravo DELTA\n" * 80    # tiny diff from v2

        target.write_bytes(v1)
        m1 = _save_new_store(world, "data.txt", "agent", v1)
        target.write_bytes(v2)
        _save_new_store(world, "data.txt", "agent", v2)
        target.write_bytes(v3)
        _save_new_store(world, "data.txt", "agent", v3)

        # diff current (v3) against m1 (v1): expect CHARLIE→DELTA upgrade
        with _capture() as (out, err):
            _run(["diff", str(target), m1.name])
        text = out.getvalue()
        # v1 had "alpha bravo charlie", v3 has "alpha bravo DELTA"
        assert "-alpha bravo charlie" in text, text
        assert "+alpha bravo DELTA" in text, text


@test
def find_snapshot_by_name_in_both_stores():
    """The lookup helper handles version names from either store."""
    with sandbox() as world:
        _write_file(world, "data.txt", b"x")
        ts = time.strftime("%Y-%m-%dT%H-%M-%S",
                           time.localtime(time.time() - 10))
        legacy = _save_legacy_gz(world, "data.txt", ts, "alpha", b"a")
        m = _save_new_store(world, "data.txt", "beta", b"b")

        import history
        # New-store lookup
        found_new = history._find_snapshot_by_name(world / "data.txt", m.name)
        assert found_new == m

        # Legacy lookup
        found_legacy = history._find_snapshot_by_name(world / "data.txt", legacy.name)
        assert found_legacy == legacy

        # Unknown returns None
        assert history._find_snapshot_by_name(world / "data.txt", "nothing.yaml") is None


@test
def find_snapshot_by_name_handles_gz_optional():
    """Legacy convenience: accept name with or without .gz suffix."""
    with sandbox() as world:
        _write_file(world, "data.txt", b"x")
        ts = time.strftime("%Y-%m-%dT%H-%M-%S",
                           time.localtime(time.time() - 10))
        legacy = _save_legacy_gz(world, "data.txt", ts, "alpha", b"a")

        import history
        # Caller passes the bare name (no .gz)
        bare = legacy.name[:-3]
        assert history._find_snapshot_by_name(world / "data.txt", bare) == legacy


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
