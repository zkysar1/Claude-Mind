""" follow-ups A/B/C: shared atomic-write helper + read recovery.

Tests for the hoisted _atomic_write_with_fallback helper and the
read_jsonl_with_recovery read-side recovery in core/scripts/_fileops.py.

Tier A (helper):     happy path, fallback path, counter observability
Tier B (recovery):   happy read, skip-corrupt-line, severe-corruption restore
Tier C (counter):    fallback hits land in <WORLD_DIR>/.fallback-stats.jsonl

Run: py -3 core/scripts/tests/test_atomic_write_fallback.py
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


# ---------------------------------------------------------------------------
# Test infrastructure: each test runs in a fresh tmpdir with WORLD_DIR
# pointed at a sandbox so .fallback-stats.jsonl writes go there.
# ---------------------------------------------------------------------------

def with_sandbox(test_fn):
    """Decorator: spin up a tmp WORLD_DIR sandbox and reload _fileops against it."""
    def wrapped():
        sandbox = Path(tempfile.mkdtemp(prefix="atomic_write_test_"))
        meta_sandbox = Path(tempfile.mkdtemp(prefix="atomic_write_meta_"))
        prior_world = os.environ.get("MIND_WORLD")
        prior_meta = os.environ.get("MIND_META")
        try:
            os.environ["MIND_WORLD"] = str(sandbox)
            os.environ["MIND_META"] = str(meta_sandbox)
            # Force re-import so WORLD_DIR/META_DIR pick up the sandbox paths.
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


def assert_eq(actual, expected, msg=""):
    if actual != expected:
        raise AssertionError(f"{msg}: expected {expected!r}, got {actual!r}")


def assert_true(cond, msg=""):
    if not cond:
        raise AssertionError(f"FAIL: {msg}")


# ---------------------------------------------------------------------------
# Tier A — _atomic_write_with_fallback
# ---------------------------------------------------------------------------

@with_sandbox
def helper_happy_path_writes_atomically(sandbox, _fileops):
    target = sandbox / "data.jsonl"
    items = [{"id": "a", "v": 1}, {"id": "b", "v": 2}]

    def _write(h):
        for it in items:
            h.write(json.dumps(it) + "\n")
    _fileops._atomic_write_with_fallback(target, _write)

    assert_true(target.exists(), "target file must exist")
    lines = target.read_text(encoding="utf-8").splitlines()
    assert_eq(len(lines), 2, "two lines written")
    assert_eq(json.loads(lines[0]), items[0])
    assert_eq(json.loads(lines[1]), items[1])
    # tmp file must be cleaned up
    assert_true(not (sandbox / "data.jsonl.tmp").exists(),
                "tmp file should be cleaned up")
    # No fallback stats should exist (atomic path was taken)
    stats = sandbox / ".fallback-stats.jsonl"
    assert_true(not stats.exists() or stats.read_text() == "",
                "no fallback stats on happy path")


@with_sandbox
def helper_fallback_path_writes_in_place(sandbox, _fileops):
    """Simulate os.replace failure by monkey-patching it. Helper must fall back
    to in-place rewrite and record a counter hit."""
    target = sandbox / "data.jsonl"
    target.write_text('{"id":"original"}\n', encoding="utf-8")

    items = [{"id": "new", "v": 99}]
    original_replace = os.replace
    call_count = {"n": 0}

    def fail_replace(*args, **kwargs):
        call_count["n"] += 1
        raise PermissionError(13, "Simulated OneDrive lock")

    os.replace = fail_replace
    try:
        def _write(h):
            for it in items:
                h.write(json.dumps(it) + "\n")
        _fileops._atomic_write_with_fallback(
            target, _write,
            fallback_counter_key="test_fallback",
            max_retries=3)
    finally:
        os.replace = original_replace

    assert_eq(call_count["n"], 3, "max_retries=3 should be tried")
    lines = target.read_text(encoding="utf-8").splitlines()
    assert_eq(len(lines), 1, "in-place rewrite produced one line")
    assert_eq(json.loads(lines[0]), items[0])
    assert_true(not (sandbox / "data.jsonl.tmp").exists(),
                "tmp file cleaned up after fallback")

    stats = sandbox / ".fallback-stats.jsonl"
    assert_true(stats.exists(), "fallback stats file must exist")
    rec = json.loads(stats.read_text(encoding="utf-8").splitlines()[0])
    assert_eq(rec["key"], "test_fallback")
    assert_true("Simulated OneDrive lock" in rec["error"],
                "error message preserved in stats")


@with_sandbox
def helper_no_counter_when_atomic_succeeds(sandbox, _fileops):
    target = sandbox / "data.jsonl"

    def _write(h):
        h.write('{"v":1}\n')
    _fileops._atomic_write_with_fallback(
        target, _write, fallback_counter_key="should_not_record")

    stats = sandbox / ".fallback-stats.jsonl"
    assert_true(not stats.exists(),
                "no stats file when atomic path succeeded")


@with_sandbox
def helper_propagates_writer_exceptions(sandbox, _fileops):
    target = sandbox / "data.jsonl"

    class BadWrite(Exception):
        pass

    def _bad_write(h):
        raise BadWrite("boom")

    raised = False
    try:
        _fileops._atomic_write_with_fallback(target, _bad_write)
    except BadWrite:
        raised = True
    assert_true(raised, "writer exception propagated")
    assert_true(not (sandbox / "data.jsonl.tmp").exists(),
                "tmp cleaned up after writer exception")


# ---------------------------------------------------------------------------
# Tier B — read_jsonl_with_recovery
# ---------------------------------------------------------------------------

@with_sandbox
def recovery_happy_path_reads_clean_file(sandbox, _fileops):
    target = sandbox / "data.jsonl"
    target.write_text(
        '{"id":"a"}\n{"id":"b"}\n{"id":"c"}\n', encoding="utf-8")
    items = _fileops.read_jsonl_with_recovery(target)
    assert_eq(len(items), 3)
    assert_eq([i["id"] for i in items], ["a", "b", "c"])


@with_sandbox
def recovery_skips_one_corrupt_line_no_restore(sandbox, _fileops):
    target = sandbox / "data.jsonl"
    target.write_text(
        '{"id":"a"}\n{this is not valid json}\n{"id":"c"}\n',
        encoding="utf-8")
    items = _fileops.read_jsonl_with_recovery(target)
    assert_eq(len(items), 2, "two clean lines, one skipped")
    assert_eq([i["id"] for i in items], ["a", "c"])
    # No .corrupt backup should exist (mild corruption, no restore)
    assert_true(not Path(str(target) + ".corrupt").exists(),
                "no backup on mild corruption")


@with_sandbox
def recovery_restores_from_history_on_severe_corruption(sandbox, _fileops):
    target = sandbox / "data.jsonl"
    # Set up a clean snapshot in .history/
    history_dir = sandbox / ".history" / "data.jsonl"
    history_dir.mkdir(parents=True, exist_ok=True)
    snapshot = history_dir / "2026-05-07T10-00-00_alpha.jsonl"
    snapshot.write_text(
        '{"id":"snap1"}\n{"id":"snap2"}\n', encoding="utf-8")

    # Now write severely corrupt content to the live file (zero parseable
    # lines, ≥3 lines)
    target.write_text(
        "garbage1\nnot json either\nstill not\nbroken\n", encoding="utf-8")

    items = _fileops.read_jsonl_with_recovery(target)

    # After recovery, items should match the snapshot
    assert_eq(len(items), 2, "two records recovered from snapshot")
    assert_eq([i["id"] for i in items], ["snap1", "snap2"])

    # The corrupt version must be preserved as <path>.corrupt
    backup = Path(str(target) + ".corrupt")
    assert_true(backup.exists(), "corrupt file preserved for forensics")
    assert_true("garbage1" in backup.read_text(encoding="utf-8"),
                "corrupt content preserved verbatim")

    # The live file is now the snapshot
    assert_true("snap1" in target.read_text(encoding="utf-8"),
                "live file replaced with snapshot")

    # Recovery must record an observability hit
    stats = sandbox / ".fallback-stats.jsonl"
    assert_true(stats.exists(), "recovery logged to fallback stats")
    rec = json.loads(stats.read_text(encoding="utf-8").splitlines()[0])
    assert_eq(rec["key"], "read_jsonl_recovery")


@with_sandbox
def recovery_returns_partial_when_no_history_snapshot(sandbox, _fileops):
    target = sandbox / "data.jsonl"
    # No .history/ directory at all; severe corruption must NOT raise —
    # caller gets whatever was parseable (zero records here).
    target.write_text(
        "garbage1\nnot json either\nbroken\n", encoding="utf-8")
    items = _fileops.read_jsonl_with_recovery(target)
    assert_eq(items, [], "no snapshot → empty list, no crash")


@with_sandbox
def recovery_handles_empty_file(sandbox, _fileops):
    target = sandbox / "data.jsonl"
    target.write_text("", encoding="utf-8")
    items = _fileops.read_jsonl_with_recovery(target)
    assert_eq(items, [], "empty file → empty list (no recovery)")


@with_sandbox
def recovery_handles_missing_file(sandbox, _fileops):
    target = sandbox / "missing.jsonl"
    items = _fileops.read_jsonl_with_recovery(target)
    assert_eq(items, [], "missing file → empty list")


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

ALL_TESTS = [
    helper_happy_path_writes_atomically,
    helper_fallback_path_writes_in_place,
    helper_no_counter_when_atomic_succeeds,
    helper_propagates_writer_exceptions,
    recovery_happy_path_reads_clean_file,
    recovery_skips_one_corrupt_line_no_restore,
    recovery_restores_from_history_on_severe_corruption,
    recovery_returns_partial_when_no_history_snapshot,
    recovery_handles_empty_file,
    recovery_handles_missing_file,
]


def main():
    failures = 0
    for t in ALL_TESTS:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception:
            failures += 1
            print(f"  FAIL  {t.__name__}")
            traceback.print_exc()
    if failures:
        print(f"\n{failures}/{len(ALL_TESTS)} tests failed")
        sys.exit(1)
    print(f"\nAll {len(ALL_TESTS)} tests passed")


if __name__ == "__main__":
    main()
