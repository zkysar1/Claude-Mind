#!/usr/bin/env python3
"""test_loop_state_slot_meta_hygiene.py —  +  regression tests.

Pins the slot_meta hygiene invariant for every direct whole-WM writer: a script
that bypasses wm-set.sh and dumps working-memory.yaml itself MUST still advance
slot_meta.<slot>.updated_at + increment update_count. Bypassing the wrapper does
not exempt a writer from the wrapper's bookkeeping (guard-449, guard-540).
Without it, wm-prune's stale-detection evicts the freshly-written slot — it
keys on slot_meta.updated_at, NOT on the slot's own content.

Covers all three such writers: loop-state-bump-counters.py and
recurring-loop-state-mutate.py (g-115-682, loop_state), and
tree-encoding-drift-gate.py (g-115-3307, force_tree_maintain — the third writer,
left behind when the first two were fixed 2026-05-13).

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
from datetime import datetime, timedelta
from pathlib import Path

try:
    import yaml
except ImportError:
    print("FAIL: PyYAML not installed", file=sys.stderr)
    sys.exit(2)


SCRIPT_DIR = Path(__file__).resolve().parent.parent  # core/scripts/
BUMP_HELPER = SCRIPT_DIR / "loop-state-bump-counters.py"
MUTATE_HELPER = SCRIPT_DIR / "recurring-loop-state-mutate.py"
DRIFT_GATE = SCRIPT_DIR / "tree-encoding-drift-gate.py"   # 3rd writer ()
WM_HELPER = SCRIPT_DIR / "wm.py"                          # for the prune harm test

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
    # guard-862 / guard-3375 (): os.environ.copy() inherits everything,
    # and on a worker Body bash-agent-inject.py exports BODY_WM_PATH. wm.wm_path()
    # (wm.py) returns BODY_WM_PATH whenever it is set and only falls back to
    # AGENT_DIR/session/working-memory.yaml when it is not — so it OUTRANKS the
    # MIND_AGENT_DIR isolation set two lines up, and every helper below wrote the
    # live per-Body WM instead of this tmpdir fixture. The fixture's slot_meta then
    # stayed frozen at SEED_TIMESTAMP, which is verbatim the assertion that fired:
    # all 7 tests here failed on every worker-run suite, on any box, while passing
    # on a reducer. Measured on cc-08 2026-08-31: 7/7 FAIL with the var inherited,
    # 7/7 PASS under `env -u BODY_WM_PATH`, no other change.
    # BODY_WM_PATH is the only BODY_* var that reaches this test: BODY_ROLE is read
    # only by close-phase-skip-check.py and uncommitted-work-gate.py (neither is
    # exercised here), and _paths.py mentions it in prose only — so it is
    # deliberately NOT popped. Sibling fix: test_class_balance_cross_session.py's
    # with_sandbox () saves/sets/restores the same var.
    env.pop("BODY_WM_PATH", None)
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
    """ field-op mode (--evolution-fired) advances
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


def _seed_wm_for_drift_gate(tmpdir: Path) -> Path:
    """WM seeded so the drift gate crosses its threshold on one tick.

    counter=98 crosses ANY configured threshold, so this fixture does not
    silently depend on tree_encoding_drift_threshold staying 3.

    slot_meta.force_tree_maintain is back-dated past the CONFIGURED eviction
    threshold — the REAL-WORLD condition. The sentinel's meta entry is only ever
    refreshed by `wm-set.sh force_tree_maintain null` (the Phase 0-pre consumer's
    clear), so by the time the gate re-sets the slot that timestamp is routinely
    hours old. Seeding it fresh is what made the original hand-probe of this bug
    come back clean.

    The back-date is DERIVED from evict_threshold_minutes, never hardcoded
    (fresh-eyes F1, g-115-3307). A fixed 3h against the current 120min default
    leaves only 60min of margin: raise that config above 180 and this test
    silently stops discriminating — prune would decline to evict even with the
    fix reverted, so the test passes trivially and the regression guard
    evaporates with no signal. That is the same "the suite cannot see the bug"
    failure this test was written to prevent, so it must not be reintroduced by
    the test itself.
    """
    session_dir = tmpdir / "session"
    session_dir.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(SCRIPT_DIR))
    import wm as _wm
    evict_mins = int(_wm.get_pruning_config(_wm.read_config()).get(
        "evict_threshold_minutes", 120))
    stale = (datetime.now() - timedelta(minutes=evict_mins * 2 + 30)
             ).isoformat(timespec="seconds")
    wm = {
        "slots": {
            "force_tree_maintain": None,
            "loop_state": {
                "goals_completed": 10,
                "signals": {"goals_since_last_tree_update": 98},
                "routine_streaks": {},
            },
        },
        "slot_meta": {
            "force_tree_maintain": {
                "updated_at": stale,
                "accessed_at": None,
                "update_count": 7,
            },
            "loop_state": {
                "updated_at": stale,
                "accessed_at": None,
                "update_count": SEED_UPDATE_COUNT,
            },
        },
    }
    wm_path = session_dir / "working-memory.yaml"
    wm_path.write_text(yaml.safe_dump(wm, sort_keys=False), encoding="utf-8")
    return wm_path


def test_drift_gate_advances_sentinel_slot_meta():
    """tree-encoding-drift-gate.py advances slot_meta.force_tree_maintain.

    Third direct whole-WM writer (after the two above), fixed g-115-3307. It
    bypasses wm-set.sh deliberately (Windows bash-subprocess hang, see its
    header) — but bypassing the wrapper does not exempt it from the wrapper's
    bookkeeping (guard-449, guard-540).
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="drift-gate-meta-test-"))
    try:
        wm_path = _seed_wm_for_drift_gate(tmpdir)
        rc, _, stderr = _run_helper(DRIFT_GATE, tmpdir)
        assert rc == 0, f"drift-gate rc={rc} stderr={stderr}"
        wm = yaml.safe_load(wm_path.read_text(encoding="utf-8"))
        sentinel = wm["slots"].get("force_tree_maintain")
        assert sentinel, f"drift-gate — sentinel not set at all (stderr={stderr})"
        meta = (wm.get("slot_meta") or {}).get("force_tree_maintain") or {}
        assert meta.get("update_count") == 8, (
            f"drift-gate — update_count expected 8, got {meta.get('update_count')}"
        )
        assert (datetime.now() - datetime.fromisoformat(meta["updated_at"])
                ).total_seconds() < 300, (
            f"drift-gate — updated_at still stale ({meta.get('updated_at')}); the "
            f"gate wrote the slot without advancing its slot_meta"
        )
        print(f"PASS: drift-gate advances sentinel slot_meta (update_count 7 → "
              f"{meta['update_count']}, updated_at → {meta['updated_at']})")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_drift_gate_sentinel_survives_prune():
    """THE HARM TEST — the sentinel must still exist after wm-prune runs.

    This is the assertion the suite was missing. Checking only "updated_at
    advanced" would still pass if prune's eviction predicate changed; this pins
    the property that actually matters end-to-end.

    g-115-3307: the gate set force_tree_maintain and reset the drift counter in
    ONE atomic write, but left slot_meta stale. wm-prune (loop Phase 11, EVERY
    iteration) then read that stale updated_at, computed mins_since >
    evict_threshold_minutes, and set the freshly-written sentinel back to None.
    Measured: evicted_slots=[{"slot":"force_tree_maintain","minutes_stale":180}].

    The asymmetry that hid it for so long: the counter reset from the SAME write
    SURVIVES, because it is nested inside loop_state and prune never descends
    into a slot's interior. So the drift signal was CONSUMED (counter zeroed)
    while its consumer never ran — silently, on every firing, while the gate
    printed success from a flag it computed before the write.

    Mutation-proven: stubbing out only the gate's update_modified call restores
    the eviction exactly.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="drift-gate-prune-test-"))
    try:
        wm_path = _seed_wm_for_drift_gate(tmpdir)
        rc, _, stderr = _run_helper(DRIFT_GATE, tmpdir)
        assert rc == 0, f"drift-gate rc={rc} stderr={stderr}"
        assert yaml.safe_load(wm_path.read_text(encoding="utf-8"))["slots"].get(
            "force_tree_maintain"), "precondition — gate did not set the sentinel"

        rc, _, stderr = _run_helper(WM_HELPER, tmpdir, "prune")
        assert rc == 0, f"wm prune rc={rc} stderr={stderr}"

        wm = yaml.safe_load(wm_path.read_text(encoding="utf-8"))
        sentinel = wm["slots"].get("force_tree_maintain")
        counter = wm["slots"]["loop_state"]["signals"]["goals_since_last_tree_update"]
        assert sentinel is not None, (
            "drift-gate sentinel was EVICTED by wm-prune — the g-115-3307 "
            "regression is back. The gate must call update_modified() for "
            "force_tree_maintain, or prune reads the stale timestamp left by "
            "the last wm-set.sh clear and drops the slot in the same iteration."
        )
        assert counter == 0, f"counter reset should persist, got {counter}"
        print("PASS: drift-gate sentinel survives wm-prune "
              f"(sentinel={sentinel.get('source')}, counter={counter})")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main() -> int:
    tests = [
        test_bump_advances_slot_meta,
        test_mutate_advances_slot_meta,
        test_bump_creates_slot_meta_when_missing,
        test_mutate_creates_slot_meta_when_missing,
        test_field_op_advances_slot_meta,
        test_drift_gate_advances_sentinel_slot_meta,
        test_drift_gate_sentinel_survives_prune,
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
        print(f"  ALL {pass_count} TESTS PASS — slot_meta hygiene pinned for all 3 writers")
        print("════════════════════════════════════════════")
        return 0
    print(f"\nFAIL: {len(tests) - pass_count}/{len(tests)} test(s) failed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
