"""Regression test for meta-backpressure cmd_check evolution-monitor skip (7).

`cmd_check` iterates `active_monitors` assuming the meta_strategy schema
(`goals_since_change` / `imp_k_samples` / `baseline_imp_k` / `strategy_file` /
`field`). Evolution monitors (`monitor_kind` in `EVOLUTION_KINDS`) share the
SAME `active_monitors` list but carry a DISJOINT schema (`revision_id` /
`file_path` / `metric_samples` / `baseline`). Before the fix, the first
evolution monitor in the list raised `KeyError` on
`monitor["goals_since_change"] += 1`, silently killing the regression-rollback
safety mechanism for ~2 weeks (every state-update-audit run logged
`backpressure check_failed`). The fix skips evolution monitors in `cmd_check`
(they are checked by `cmd_evolution_check` instead), symmetric to the
`if kind not in EVOLUTION_KINDS: continue` filter already present there.

These tests lock the skip: `cmd_check` must (a) never raise on an evolution
monitor, (b) still process meta_strategy monitors, (c) leave evolution monitors
untouched. Loader pattern mirrors test_blocker_recheck_tolerant_parse.py
(hyphenated script name -> spec_from_file_location).
"""
import argparse
import contextlib
import importlib.util
import io
import sys
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def load_module():
    spec = importlib.util.spec_from_file_location(
        "meta_backpressure_module",
        SCRIPT_DIR / "meta-backpressure.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = load_module()


def _meta_strategy_monitor():
    """Schema written by cmd_monitor (a meta-strategy change)."""
    return {
        "meta_change_id": "mc-test-001",
        "strategy_file": "goal-selection-strategy.yaml",
        "field": "exploration_fraction",
        "old_value": 0.2,
        "new_value": 0.3,
        "baseline_imp_k": 0.40,
        "goals_since_change": 0,
        "imp_k_samples": [],
        "consecutive_below_baseline": 0,
        "consecutive_above_baseline": 0,
        "status": "monitoring",
        "created": "2026-05-28T00:00:00",
    }


def _evolution_monitor(revision_id="skill-test-001"):
    """Schema written by cmd_evolution_monitor -- DISJOINT from meta_strategy.

    Note the ABSENCE of goals_since_change / imp_k_samples / baseline_imp_k /
    strategy_file / field. Those absences are exactly what crashed cmd_check
    before the EVOLUTION_KINDS skip (g-115-1277).
    """
    return {
        "monitor_kind": "skill_evolution",
        "revision_id": revision_id,
        "file_path": ".claude/skills/forge-skill/SKILL.md",
        "agent": "zeta",
        "history_snapshot": "snap-001",
        "baseline": {"forge_success_rate": 0.5},
        "metric_samples": [],
        "consecutive_below_baseline": 0,
        "consecutive_above_baseline": 0,
        "status": "monitoring",
        "created": "2026-05-28T00:00:00",
    }


@contextlib.contextmanager
def _bp_state(tmp_path, monitors):
    """Point BP_PATH at a tmp fixture file and neutralize the locking writer.

    write_yaml is swapped for a plain dumper -- the _fileops locking/history
    layer is not under test here, and routing it through tmp avoids touching
    the real meta/backpressure.yaml.
    """
    bp_file = tmp_path / "backpressure.yaml"
    bp_file.write_text(
        yaml.safe_dump({
            "version": 1,
            "active_monitors": monitors,
            "rollback_history": [],
        }),
        encoding="utf-8",
    )
    orig_path = M.BP_PATH
    orig_write = M.write_yaml
    M.BP_PATH = bp_file
    M.write_yaml = lambda path, data: Path(path).write_text(
        yaml.safe_dump(data), encoding="utf-8")
    try:
        yield bp_file
    finally:
        M.BP_PATH = orig_path
        M.write_yaml = orig_write


def _run_check(learning_value=0.5):
    """Invoke cmd_check; swallow its JSON stdout. Raises pre-fix on evo monitors."""
    args = argparse.Namespace(learning_value=learning_value)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        M.cmd_check(args)
    return buf.getvalue()


# -- Case 1: mixed list -- the core regression lock --------------------------
def test_cmd_check_mixed_list_no_keyerror(tmp_path):
    """meta_strategy + evolution monitor in one list. Pre-fix: KeyError on the
    evolution monitor. Post-fix: no raise, meta processed, evolution untouched."""
    with _bp_state(tmp_path, [_meta_strategy_monitor(), _evolution_monitor()]) as bp_file:
        _run_check(learning_value=0.5)  # raises KeyError pre-fix
        state = yaml.safe_load(bp_file.read_text(encoding="utf-8"))

    assert len(state["active_monitors"]) == 2, "both monitors must remain monitoring"

    meta = next(m for m in state["active_monitors"] if "meta_change_id" in m)
    evo = next(m for m in state["active_monitors"]
               if m.get("monitor_kind") == "skill_evolution")

    # meta_strategy monitor WAS processed by cmd_check
    assert meta["goals_since_change"] == 1
    assert meta["imp_k_samples"] == [0.5]
    assert meta["consecutive_above_baseline"] == 1  # 0.5 >= 0.40 - 0.10 threshold

    # evolution monitor was NOT touched -- no meta_strategy fields injected
    assert evo["metric_samples"] == []
    assert "goals_since_change" not in evo
    assert "imp_k_samples" not in evo


# -- Case 2: evolution-only list -- the exact live-incident shape ------------
def test_cmd_check_evolution_only_no_keyerror(tmp_path):
    """The live backpressure.yaml had 10 skill_evolution monitors and zero
    meta_strategy monitors -- cmd_check crashed on the first one. Lock that
    shape: an all-evolution list must process cleanly and untouched."""
    monitors = [_evolution_monitor(f"skill-{i:03d}") for i in range(3)]
    with _bp_state(tmp_path, monitors) as bp_file:
        _run_check(learning_value=0.5)  # raises KeyError pre-fix
        state = yaml.safe_load(bp_file.read_text(encoding="utf-8"))

    assert len(state["active_monitors"]) == 3
    for m in state["active_monitors"]:
        assert m["metric_samples"] == []
        assert "goals_since_change" not in m


# -- Case 3: meta_strategy-only list -- the skip must not break the base case -
def test_cmd_check_meta_only_still_processed(tmp_path):
    """Regression guard in the other direction: the EVOLUTION_KINDS skip uses
    monitor.get('monitor_kind') -- a meta_strategy monitor (no monitor_kind
    key) returns None, which is NOT in EVOLUTION_KINDS, so it must still be
    processed exactly as before the fix."""
    with _bp_state(tmp_path, [_meta_strategy_monitor()]) as bp_file:
        _run_check(learning_value=0.5)
        state = yaml.safe_load(bp_file.read_text(encoding="utf-8"))

    assert len(state["active_monitors"]) == 1
    meta = state["active_monitors"][0]
    assert meta["goals_since_change"] == 1
    assert meta["imp_k_samples"] == [0.5]


# -- Case 4: cmd_graduate mixed list -- 7 sibling of the check() skip -
def test_cmd_graduate_mixed_list_no_keyerror(tmp_path):
    """cmd_graduate iterated active_monitors doing monitor["meta_change_id"];
    an evolution monitor (no meta_change_id) KeyError'd the WHOLE endpoint,
    breaking graduate fleet-wide (g-115-2677 -- the sibling cmd_check got fixed
    in g-115-1277 but graduate was missed). Post-fix: .get() skips evolution
    monitors, the named weight monitor graduates, evolution monitors untouched.
    Evolution monitor is FIRST in the list -- exactly where the pre-fix KeyError
    fired."""
    monitors = [_evolution_monitor("skill-a"), _meta_strategy_monitor(),
                _evolution_monitor("skill-b")]
    args = argparse.Namespace(change_id="mc-test-001")
    with _bp_state(tmp_path, monitors) as bp_file:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            M.cmd_graduate(args)          # raises KeyError pre-fix (evo monitor first)
        out = buf.getvalue()
        state = yaml.safe_load(bp_file.read_text(encoding="utf-8"))

    assert '"status": "graduated"' in out
    # graduated weight monitor removed from the active list; both evolution
    # monitors remain (still monitoring, never a match for the weight change_id)
    assert len(state["active_monitors"]) == 2
    for m in state["active_monitors"]:
        assert m.get("monitor_kind") == "skill_evolution"
    assert all(m.get("meta_change_id") != "mc-test-001"
               for m in state["active_monitors"])


# -- Case 5: cmd_graduate evolution-only list -- not-found, no crash, no drop --
def test_cmd_graduate_evolution_only_not_found(tmp_path):
    """An all-evolution active_monitors list + graduate for a weight change_id
    must report not-found WITHOUT raising (the pre-fix crash) and must NOT drop
    any evolution monitor (not-found returns before write_yaml)."""
    monitors = [_evolution_monitor(f"skill-{i}") for i in range(3)]
    args = argparse.Namespace(change_id="mc-nonexistent")
    with _bp_state(tmp_path, monitors) as bp_file:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            M.cmd_graduate(args)          # raises KeyError pre-fix
        out = buf.getvalue()
        state = yaml.safe_load(bp_file.read_text(encoding="utf-8"))

    assert "not found" in out
    assert len(state["active_monitors"]) == 3   # nothing dropped on not-found
