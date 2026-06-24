#!/usr/bin/env python3
"""test_loop_state_slot_meta_hygiene.py —  regression test.

Pins the slot_meta hygiene invariant: both bash single-writers of loop_state
MUST advance slot_meta.loop_state.updated_at + increment update_count on every
write. Without this, wm-prune's stale-detection evicts loop_state mid-session
even though F1 (protected_slots) flagged the slot for protection.

Origin of bug: zeta session 28 iter 5 (2026-05-13, g-115-681). loop_state was
evicted by wm-prune despite frequent writes because both bash writers
(loop-state-bump-counters.py + recurring-loop-state-mutate.py) called
yaml.safe_dump directly without calling update_modified() — so
slot_meta.loop_state.updated_at lagged. F1 (protected_slots) shipped 2026-05-13
to make wm-prune skip loop_state during prune sweeps. F2 (this fix) closes
the root cause so slot_meta.updated_at stays fresh on every write.

This test invokes each helper directly against a tempdir-isolated WM file and
asserts (a) slot_meta entry exists post-call, (b) updated_at advanced from the
seeded baseline, (c) update_count incremented by 1.

Refs: g-115-682 (this fix), g-115-681 (root-cause investigation by zeta),
guard-540 (sibling), core/scripts/loop-state-bump-counters.py,
core/scripts/recurring-loop-state-mutate.py.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import yaml
except ImportError:
    print("FAIL: PyYAML not installed", file=sys.stderr)
    sys.exit(2)


SCRIPT_DIR = Path(__file__).resolve().parent.parent  # core/scripts/
BUMP_HELPER = SCRIPT_DIR / "loop-state-bump-counters.py"
MUTATE_HELPER = SCRIPT_DIR / "recurring-loop-state-mutate.py"

# Seed baseline timestamp known to be in the past — any post-call updated_at
# value strictly newer indicates the writer called update_modified.
SEED_TIMESTAMP = "2020-01-01T00:00:00"
SEED_UPDATE_COUNT = 7  # Arbitrary baseline — post-call must be SEED+1.


def _seed_wm(tmpdir: Path, with_slot_meta: bool = True) -> Path:
    """Write a minimal WM with loop_state seeded, slot_meta optional."""
    session_dir = tmpdir / "session"
    session_dir.mkdir(parents=True, exist_ok=True)
    wm = {
        "slots": {
            "loop_state": {
                "goals_completed": 10,
                "productive_goals": 8,
                "evolutions": 0,
                "signals": {
                    "routine_streak_global": 0,
                    "productive_streak": 0,
                    "routine_count_total": 0,
                    "consecutive_blocked_sleeps": 0,
                },
                "routine_streaks": {},
                "goals_completed_this_session": 0,
                "productive_goals_this_session": 0,
            },
            "active_context": {"session_id": "test-session"},
        }
    }
    if with_slot_meta:
        wm["slot_meta"] = {
            "loop_state": {
                "updated_at": SEED_TIMESTAMP,
                "accessed_at": SEED_TIMESTAMP,
                "update_count": SEED_UPDATE_COUNT,
            }
        }
    wm_path = session_dir / "working-memory.yaml"
    wm_path.write_text(yaml.safe_dump(wm, sort_keys=False), encoding="utf-8")
    return wm_path


def _read_slot_meta(wm_path: Path) -> dict:
    wm = yaml.safe_load(wm_path.read_text(encoding="utf-8"))
    return (wm.get("slot_meta") or {}).get("loop_state") or {}


def _run_helper(helper: Path, tmpdir: Path, *extra_args) -> tuple:
    env = os.environ.copy()
    env["MIND_AGENT_DIR"] = str(tmpdir)
    env["MIND_AGENT"] = "test-agent-isolated"
    result = subprocess.run(
        ["py", "-3", str(helper), *extra_args],
        env=env,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def test_bump_advances_slot_meta():
    """loop-state-bump-counters.py advances slot_meta.loop_state.updated_at + update_count."""
    tmpdir = Path(tempfile.mkdtemp(prefix="loop-state-meta-test-"))
    try:
        wm_path = _seed_wm(tmpdir, with_slot_meta=True)
        rc, _, stderr = _run_helper(BUMP_HELPER, tmpdir, "--outcome", "deep")
        assert rc == 0, f"bump rc={rc} stderr={stderr}"
        meta = _read_slot_meta(wm_path)
        assert meta, "bump — slot_meta.loop_state missing post-call"
        assert meta.get("updated_at") != SEED_TIMESTAMP, (
            f"bump — updated_at not advanced from seed (still {SEED_TIMESTAMP})"
        )
        expected_count = SEED_UPDATE_COUNT + 1
        assert meta.get("update_count") == expected_count, (
            f"bump — update_count expected {expected_count}, got {meta.get('update_count')}"
        )
        print(
            f"PASS: bump advances slot_meta (updated_at: {SEED_TIMESTAMP} → {meta['updated_at']}, "
            f"update_count: {SEED_UPDATE_COUNT} → {meta['update_count']})"
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_mutate_advances_slot_meta():
    """recurring-loop-state-mutate.py advances slot_meta.loop_state.updated_at + update_count."""
    tmpdir = Path(tempfile.mkdtemp(prefix="loop-state-meta-test-"))
    try:
        wm_path = _seed_wm(tmpdir, with_slot_meta=True)
        rc, _, stderr = _run_helper(
            MUTATE_HELPER, tmpdir, "--goal-id", "g-test-001", "--outcome", "routine"
        )
        assert rc == 0, f"mutate rc={rc} stderr={stderr}"
        meta = _read_slot_meta(wm_path)
        assert meta, "mutate — slot_meta.loop_state missing post-call"
        assert meta.get("updated_at") != SEED_TIMESTAMP, (
            f"mutate — updated_at not advanced from seed (still {SEED_TIMESTAMP})"
        )
        expected_count = SEED_UPDATE_COUNT + 1
        assert meta.get("update_count") == expected_count, (
            f"mutate — update_count expected {expected_count}, got {meta.get('update_count')}"
        )
        print(
            f"PASS: mutate advances slot_meta (updated_at: {SEED_TIMESTAMP} → {meta['updated_at']}, "
            f"update_count: {SEED_UPDATE_COUNT} → {meta['update_count']})"
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_bump_creates_slot_meta_when_missing():
    """When slot_meta absent entirely, bump creates it with sensible defaults."""
    tmpdir = Path(tempfile.mkdtemp(prefix="loop-state-meta-test-"))
    try:
        wm_path = _seed_wm(tmpdir, with_slot_meta=False)
        rc, _, stderr = _run_helper(BUMP_HELPER, tmpdir, "--outcome", "deep")
        assert rc == 0, f"bump-missing-meta rc={rc} stderr={stderr}"
        meta = _read_slot_meta(wm_path)
        assert meta, "bump-missing-meta — slot_meta.loop_state still missing after write"
        assert meta.get("update_count") == 1, (
            f"bump-missing-meta — fresh update_count expected 1, got {meta.get('update_count')}"
        )
        assert meta.get("updated_at"), "bump-missing-meta — updated_at not set"
        print(
            f"PASS: bump creates slot_meta when missing (update_count=1, updated_at={meta['updated_at']})"
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_mutate_creates_slot_meta_when_missing():
    """When slot_meta absent entirely, mutate creates it with sensible defaults."""
    tmpdir = Path(tempfile.mkdtemp(prefix="loop-state-meta-test-"))
    try:
        wm_path = _seed_wm(tmpdir, with_slot_meta=False)
        rc, _, stderr = _run_helper(
            MUTATE_HELPER, tmpdir, "--goal-id", "g-test-002", "--outcome", "deep"
        )
        assert rc == 0, f"mutate-missing-meta rc={rc} stderr={stderr}"
        meta = _read_slot_meta(wm_path)
        assert meta, "mutate-missing-meta — slot_meta.loop_state still missing after write"
        assert meta.get("update_count") == 1, (
            f"mutate-missing-meta — fresh update_count expected 1, got {meta.get('update_count')}"
        )
        assert meta.get("updated_at"), "mutate-missing-meta — updated_at not set"
        print(
            f"PASS: mutate creates slot_meta when missing (update_count=1, updated_at={meta['updated_at']})"
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_field_op_advances_slot_meta():
    """1 field-op mode (--evolution-fired) advances
    slot_meta.loop_state.updated_at + update_count (guard-540 — the new bash
    writers must not let slot_meta lag, else wm-prune evicts the slot). The
    --reset-alignment / --evolution-fired path uses its OWN _run_field_op
    write, separate from the --outcome bump, so it needs its own pin."""
    tmpdir = Path(tempfile.mkdtemp(prefix="loop-state-meta-test-"))
    try:
        wm_path = _seed_wm(tmpdir, with_slot_meta=True)
        rc, _, stderr = _run_helper(BUMP_HELPER, tmpdir, "--evolution-fired")
        assert rc == 0, f"field-op rc={rc} stderr={stderr}"
        meta = _read_slot_meta(wm_path)
        assert meta, "field-op — slot_meta.loop_state missing post-call"
        assert meta.get("updated_at") != SEED_TIMESTAMP, (
            f"field-op — updated_at not advanced from seed (still {SEED_TIMESTAMP})"
        )
        expected_count = SEED_UPDATE_COUNT + 1
        assert meta.get("update_count") == expected_count, (
            f"field-op — update_count expected {expected_count}, got {meta.get('update_count')}"
        )
        print(
            f"PASS: field-op (--evolution-fired) advances slot_meta "
            f"(update_count: {SEED_UPDATE_COUNT} → {meta['update_count']})"
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main() -> int:
    tests = [
        test_bump_advances_slot_meta,
        test_mutate_advances_slot_meta,
        test_bump_creates_slot_meta_when_missing,
        test_mutate_creates_slot_meta_when_missing,
        test_field_op_advances_slot_meta,
    ]
    pass_count = 0
    for t in tests:
        try:
            t()
            pass_count += 1
        except AssertionError as e:
            print(f"FAIL: {t.__name__}: {e}", file=sys.stderr)
    if pass_count == len(tests):
        print("\n════════════════════════════════════════════")
        print(f"  ALL {pass_count} TESTS PASS — slot_meta hygiene pinned for both writers")
        print("════════════════════════════════════════════")
        return 0
    print(f"\nFAIL: {len(tests) - pass_count}/{len(tests)} test(s) failed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
