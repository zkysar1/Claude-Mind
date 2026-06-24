#!/usr/bin/env python3
"""test_loop_state_merge_gate_preservation.py — 8 regression test.

Pins the Mechanism C backstop (g-115-1411): loop-state-merge-gate.check() floors
the three monotonic top-level loop_state counters — goals_completed,
productive_goals (max), and counted_goals_this_session (union) — to the on-disk
committed values on EVERY write path. This closes the CAS coverage gap that
g-115-1394 left exposed: the CAS in _fileops.loop_state_cas_retry is wired only
into the 3 delta-writers, so a collateral full-slot writer (tree-encoding-drift-
gate.py, stale-sentinel-canary.py, productivity-stop-gate.sh, or the daemon
full-slot set) that captured loop_state BEFORE a peer's bump — keeping every
signals subkey (so rb-715 passes it) but carrying stale-lower counters — would,
under the guard-685 stale-lock-steal race, REVERT the committed bump.

THE live repro this pins (g-305-15, commit fda38a4e): closed deep reporting
goals_completed=11, but live loop_state read 10 with g-305-15 absent from
counted_goals_this_session. With this gate, a stale-lower full-slot write can no
longer lower the on-disk counters.

These tests call check() directly with a fresh fixture per case (rb-659:
multi-call tests against state-mutating logic need fresh fixtures) — the
cleanest way to assert "no revert" without spawning racing processes. The CAS
tier itself is pinned by test_loop_state_cas_stale_steal.py; the rb-715
subdict-clobber block by the existing gate behaviour (re-asserted here so this
fix cannot silently break it).

Refs: g-115-1418 (this fix), g-115-1411 (Mechanism C analysis), guard-746,
guard-685, guard-540, guard-154, rb-715, loop-state-integrity tree node,
core/scripts/loop-state-merge-gate.py::check / _preserve_monotonic_counters.

Run: py -3 core/scripts/tests/test_loop_state_merge_gate_preservation.py
"""
import copy
import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # core/scripts/
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# The gate filename is kebab-case (project convention for *.py under
# core/scripts/), so it cannot be imported as a module name — load via importlib
# exactly as wm.py cmd_set and the daemon wm_write.py do.
_GATE_PATH = SCRIPT_DIR / "loop-state-merge-gate.py"
_spec = importlib.util.spec_from_file_location("loop_state_merge_gate", _GATE_PATH)
_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gate)
check = _gate.check


def _ls(goals_completed, productive_goals, counted, signals=None):
    """Build a minimal loop_state dict in the real shape (top-level counters +
    counted_goals_this_session list + signals subdict)."""
    ls = {
        "goals_completed": goals_completed,
        "productive_goals": productive_goals,
        "counted_goals_this_session": list(counted),
    }
    # Default signals carries the same subkeys on both sides so the rb-715
    # subdict-clobber block does NOT fire — these tests isolate the counter path.
    ls["signals"] = signals if signals is not None else {
        "routine_streak_global": 0,
        "productive_streak": 0,
    }
    return ls


def test_collateral_revert_floored():
    """THE regression (Mechanism C /  shape): a collateral full-slot
    write carrying stale-lower counters + a subset counted_goals must NOT lower
    the on-disk committed state. goals_completed 10<11, productive 7<8,
    counted [g1,g2] subset of [g1,g2,g3]."""
    on_disk = _ls(11, 8, ["g1", "g2", "g3"])
    incoming = _ls(10, 7, ["g1", "g2"])
    incoming_snapshot = copy.deepcopy(incoming)

    r = check(incoming, on_disk)

    assert r["would_block"] is False, f"should not block: {r['reason']}"
    assert r["counters_preserved"] is True, "expected preservation to fire"
    pv = r["preserved_value"]
    assert pv["goals_completed"] == 11, f"goals_completed floored to 11, got {pv['goals_completed']}"
    assert pv["productive_goals"] == 8, f"productive_goals floored to 8, got {pv['productive_goals']}"
    assert pv["counted_goals_this_session"] == ["g1", "g2", "g3"], \
        f"counted_goals unioned, got {pv['counted_goals_this_session']}"
    # Inputs not mutated.
    assert incoming == incoming_snapshot, "check() must not mutate incoming"
    return True


def test_equal_counters_no_preservation():
    """Counters and counted_goals identical -> no preservation, value unchanged."""
    on_disk = _ls(11, 8, ["g1", "g2", "g3"])
    incoming = _ls(11, 8, ["g1", "g2", "g3"])

    r = check(incoming, on_disk)

    assert r["would_block"] is False
    assert r["counters_preserved"] is False, "nothing to preserve when equal"
    assert r["preserved_value"] is incoming, "no-op returns the incoming object unchanged"
    return True


def test_incoming_higher_not_lowered():
    """The legitimate bump landing: incoming HIGHER than on-disk must pass
    through unchanged (monotonic up — never floor down)."""
    on_disk = _ls(11, 8, ["g1", "g2"])
    incoming = _ls(12, 9, ["g1", "g2", "g3"])  # the bump that added g3

    r = check(incoming, on_disk)

    assert r["would_block"] is False
    assert r["counters_preserved"] is False, "higher incoming is not a revert"
    assert r["preserved_value"]["goals_completed"] == 12
    assert r["preserved_value"]["counted_goals_this_session"] == ["g1", "g2", "g3"]
    return True


def test_counted_goals_only_preservation():
    """Ints equal but counted_goals a stale subset -> union path fires
    independently of the max path."""
    on_disk = _ls(11, 8, ["g1", "g2", "g3"])
    incoming = _ls(11, 8, ["g1", "g2"])  # missing g3

    r = check(incoming, on_disk)

    assert r["counters_preserved"] is True
    assert r["preserved_value"]["counted_goals_this_session"] == ["g1", "g2", "g3"]
    assert r["preserved_value"]["goals_completed"] == 11
    return True


def test_counted_goals_union_superset():
    """Both sides have a goal the other lacks -> union is a superset, no id lost
    (incoming order first, on-disk-only appended)."""
    on_disk = _ls(11, 8, ["g1", "g2", "g3"])
    incoming = _ls(11, 8, ["g1", "g2", "g4"])  # has g4, missing g3

    r = check(incoming, on_disk)

    assert r["counters_preserved"] is True
    merged = r["preserved_value"]["counted_goals_this_session"]
    assert set(merged) == {"g1", "g2", "g3", "g4"}, f"union superset, got {merged}"
    assert merged == ["g1", "g2", "g4", "g3"], f"incoming order + on-disk-only appended, got {merged}"
    return True


def test_reorder_is_not_a_change():
    """Same membership, different order -> NOT a change (only genuine drops
    trigger preservation; avoids rewriting on a benign reorder)."""
    on_disk = _ls(11, 8, ["g2", "g1"])
    incoming = _ls(11, 8, ["g1", "g2"])

    r = check(incoming, on_disk)

    assert r["counters_preserved"] is False, "reorder with same membership is not a revert"
    assert r["preserved_value"] is incoming
    return True


def test_override_skips_preservation():
    """--override-merge-gate signals an intentional wholesale reset -> skip
    preservation; the stale-lower value passes through unchanged."""
    on_disk = _ls(11, 8, ["g1", "g2", "g3"])
    incoming = _ls(0, 0, [])  # explicit reset

    r = check(incoming, on_disk, override="session boundary reset")

    assert r["counters_preserved"] is False, "override must skip preservation"
    assert r["preserved_value"]["goals_completed"] == 0, "reset value passes through under override"
    assert r["preserved_value"]["counted_goals_this_session"] == []
    return True


def test_first_write_on_disk_none():
    """on-disk is None (first write / post-clear re-init) -> nothing to
    preserve, pass through."""
    incoming = _ls(1, 1, ["g1"])

    r = check(incoming, None)

    assert r["would_block"] is False
    assert r["counters_preserved"] is False
    assert r["preserved_value"] is incoming
    return True


def test_none_incoming_clear():
    """Incoming None is the legitimate session-end clear -> pass through with
    preserved_value None (the null-clear still works)."""
    on_disk = _ls(11, 8, ["g1", "g2", "g3"])

    r = check(None, on_disk)

    assert r["would_block"] is False
    assert r["counters_preserved"] is False
    assert r["preserved_value"] is None, "clear must write None, not the on-disk dict"
    return True


def test_rb715_block_still_fires():
    """Regression guard for the EXISTING gate behaviour: a write that DROPS an
    on-disk signals subkey must still block (rb-715) — this fix must not weaken
    it. Counter preservation is computed but the block takes precedence."""
    on_disk = _ls(11, 8, ["g1", "g2"], signals={
        "routine_streak_global": 2,
        "quiescence": {"some": "state"},   # incoming will drop this
    })
    incoming = _ls(10, 7, ["g1"], signals={
        "routine_streak_global": 2,        # quiescence dropped
    })

    r = check(incoming, on_disk)

    assert r["would_block"] is True, "dropping an on-disk signals subkey must still block (rb-715)"
    assert "quiescence" in r["missing_subkeys"], f"missing_subkeys should name quiescence: {r['missing_subkeys']}"
    return True


def test_type_transition_still_refused():
    """Regression guard (): on-disk dict + non-dict incoming must still
    refuse, and the new preservation code must not crash on the non-dict path."""
    on_disk = _ls(11, 8, ["g1"])

    r = check("a stray traceback string", on_disk)

    assert r["would_block"] is True, "type-transition must still be refused"
    return True


def main():
    tests = [
        test_collateral_revert_floored,
        test_equal_counters_no_preservation,
        test_incoming_higher_not_lowered,
        test_counted_goals_only_preservation,
        test_counted_goals_union_superset,
        test_reorder_is_not_a_change,
        test_override_skips_preservation,
        test_first_write_on_disk_none,
        test_none_incoming_clear,
        test_rb715_block_still_fires,
        test_type_transition_still_refused,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL: {t.__name__} — {e}", file=sys.stderr)
            failed += 1
        except Exception as e:  # pragma: no cover - unexpected
            print(f"ERROR: {t.__name__} — {type(e).__name__}: {e}", file=sys.stderr)
            failed += 1
    if failed == 0:
        print("\n════════════════════════════════════════════")
        print(f"  ALL {passed} TESTS PASS — loop_state merge-gate preservation pinned")
        print("════════════════════════════════════════════")
        return 0
    print(f"\nFAIL: {failed}/{passed + failed} test(s) failed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
