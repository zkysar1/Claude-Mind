"""Counter-before-interval write order in cargo-cult-detector auto-contract (guard-382).

THE INCIDENT (g-326-533 / g-326-85, measured 2026-08-21 on zeta/cc-02). The
auto-contract wrote `interval_hours` FIRST and called `reset_consecutive_deep`
only inside `if update_interval_hours(...)`. On g-326-85 the interval write
LANDED (6h -> 4.0h; the stored float is exactly `str(round(6.0/1.5, 2))`) and
still reported failure, so the gated reset never ran:

    interval_hours   = 4.0   <- contraction applied
    consecutive_deep = 4     <- NEVER reset, and >= threshold 3

A stale counter at or above threshold re-fires the contract on every subsequent
deep close, spending evidence that was already spent — 6h -> 4.0 -> 2.67 -> and
then through the 1.98h floor. guard-382 names this exact failure and prescribes
the fix in the opposite direction: "write the streak counter FIRST ... Reverse
order risks stuck-at-level-0 infinite loops on partial-failure." guard-3117: a
field written by phase N proves phase N ran, never that the job finished.

The second half of the defect was the FALL-THROUGH. A failed interval write
dropped into the floor-hit Idea path, whose description template says the
proposed value is "below the floor" — so g-326-533 was filed claiming a 4.0h
proposal was below a 1.98h floor. That report is arithmetically impossible and
it cost a full investigation to unwind. A write failure is not a floor event.

Pinned here:
  - reset is called BEFORE the interval write (order, not just presence)
  - reset failure aborts with rc=1 and never touches the interval
  - interval-write failure returns rc=1 and files NO Idea
  - the happy path still contracts and still resets
  - a GENUINE floor hit still files the floor-hit Idea (fix did not narrow it)

Run: STORAGE_BACKEND=local py -3 -m pytest \
       core/scripts/tests/test_cargo_cult_contract_write_order.py -v
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DETECTOR_PY = SCRIPT_DIR.parent / "cargo-cult-detector.py"

CONTRACT_CFG = {
    "deep_streak_contract_threshold": 3,
    "deep_streak_contract_divisor": 1.5,
    "contract_floor_ratio": 0.33,
    "contract_suppress_window": 5,
    "contract_suppress_min_samples": 3,
}
DETECTOR_CFG = {"multiplier": 1.5, "cap_ratio": 3.0}


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "cargo_cult_detector_write_order", DETECTOR_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_source(path: Path, goal: dict):
    asp = {"id": "asp-t", "status": "active", "goals": [goal]}
    path.write_text(json.dumps(asp) + "\n", encoding="utf-8")


def _mk_goal(interval, original, deep=3, goal_id="g-t-01"):
    return {
        "id": goal_id, "title": "Test recurring goal", "recurring": True,
        "status": "pending", "interval_hours": interval,
        "original_interval_hours": original, "consecutive_deep": deep,
    }


def _args(goal_id="g-t-01", dry_run=False):
    return argparse.Namespace(goal_id=goal_id, source="world", dry_run=dry_run)


def _instrument(mod, tmp_src, *, cadence, reset_ok=True, interval_ok=True):
    """Monkeypatch the write seams. `order` records the interleaving, which is
    the whole point — presence-only assertions cannot catch a reordering."""
    rec = {"order": [], "contract": [], "reset": [], "filed": []}

    def _reset(gid, src):
        rec["order"].append("reset")
        rec["reset"].append(gid)
        return reset_ok

    def _contract(gid, src, new, orig, had_original):
        rec["order"].append("interval")
        rec["contract"].append((gid, new, orig))
        return interval_ok

    mod.source_path = lambda source, agent_override=None: tmp_src
    mod.reset_consecutive_deep = _reset
    mod.update_interval_hours = _contract
    mod.file_idea = lambda asp_id, source, idea: (
        rec["order"].append("idea") or rec["filed"].append(idea) or "g-t-99")
    mod._load_streak_mult = lambda: 2.0
    mod._recent_actual_cadence = (
        lambda gid, window, min_samples, log_path=None: cadence)
    return rec


# ------------------------------------------------------- the ordering itself


def test_reset_precedes_interval_write():
    """THE regression guard. guard-382: counter first, always."""
    mod = _load_module()
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "aspirations.jsonl"
        _write_source(src, _mk_goal(interval=6.0, original=6.0))
        rec = _instrument(mod, src, cadence=(8.0, "ok", 5))
        rc = mod.cmd_contract_per_goal(_args(), DETECTOR_CFG, CONTRACT_CFG)
    assert rc == 0
    assert rec["order"] == ["reset", "interval"], rec["order"]


def test_happy_path_still_contracts_and_resets(tmp_path):
    mod = _load_module()
    src = tmp_path / "aspirations.jsonl"
    _write_source(src, _mk_goal(interval=6.0, original=6.0))
    rec = _instrument(mod, src, cadence=(8.0, "ok", 5))
    rc = mod.cmd_contract_per_goal(_args(), DETECTOR_CFG, CONTRACT_CFG)
    assert rc == 0
    # 6.0 / 1.5 = 4.0 — the exact value that landed on .
    assert rec["contract"] == [("g-t-01", 4.0, 6.0)]
    assert rec["reset"] == ["g-t-01"]
    assert rec["filed"] == []


# ------------------------------------------------------ partial-failure paths


def test_reset_failure_aborts_before_touching_interval(tmp_path):
    """Fail closed. If the counter cannot be reset, contracting would recreate
    the exact stuck state this fix removes."""
    mod = _load_module()
    src = tmp_path / "aspirations.jsonl"
    _write_source(src, _mk_goal(interval=6.0, original=6.0))
    rec = _instrument(mod, src, cadence=(8.0, "ok", 5), reset_ok=False)
    rc = mod.cmd_contract_per_goal(_args(), DETECTOR_CFG, CONTRACT_CFG)
    assert rc == 1
    assert rec["contract"] == [], "interval must not move when the reset failed"
    assert rec["filed"] == []
    assert rec["order"] == ["reset"]


def test_interval_write_failure_files_no_idea(tmp_path, capsys):
    """The false-narrative path. Before the fix this fell through and filed a
    floor-hit Idea asserting that 4.0h was below a 1.98h floor."""
    mod = _load_module()
    src = tmp_path / "aspirations.jsonl"
    _write_source(src, _mk_goal(interval=6.0, original=6.0))
    rec = _instrument(mod, src, cadence=(8.0, "ok", 5), interval_ok=False)
    rc = mod.cmd_contract_per_goal(_args(), DETECTOR_CFG, CONTRACT_CFG)
    err = capsys.readouterr().err
    assert rc == 1
    assert rec["filed"] == [], "a write failure is not a floor event"
    assert rec["order"] == ["reset", "interval"]
    # The diagnostic must say which of the two it was, or the next reader
    # repeats the  investigation.
    assert "write failure, not a floor hit" in err
    assert "ABOVE" in err


def test_interval_write_failure_leaves_counter_and_interval_consistent(tmp_path):
    """The post-state is what makes counter-first safe: counter 0, interval
    unchanged. The streak rebuilds and retries — no stuck state either way."""
    mod = _load_module()
    src = tmp_path / "aspirations.jsonl"
    _write_source(src, _mk_goal(interval=6.0, original=6.0))
    rec = _instrument(mod, src, cadence=(8.0, "ok", 5), interval_ok=False)
    mod.cmd_contract_per_goal(_args(), DETECTOR_CFG, CONTRACT_CFG)
    assert rec["reset"] == ["g-t-01"]          # counter zeroed
    assert rec["contract"] == [("g-t-01", 4.0, 6.0)]   # attempted, reported fail
    # Nothing else was written, so the goal keeps its old interval.


# --------------------------------------------- the floor path must be intact


def test_genuine_floor_hit_still_files_the_idea(tmp_path, capsys):
    """The fix must not narrow the real escalation. 1.78 / 1.5 = 1.19 which is
    below floor 0.33 x 4.0 = 1.32, so this is a TRUE floor hit."""
    mod = _load_module()
    src = tmp_path / "aspirations.jsonl"
    _write_source(src, _mk_goal(interval=1.78, original=4.0))
    # cadence 2.0 <= 2.0 x 1.78 = 3.56 -> suppression does not fire.
    rec = _instrument(mod, src, cadence=(2.0, "ok", 5))
    mod.cmd_contract_per_goal(_args(), DETECTOR_CFG, CONTRACT_CFG)
    out = capsys.readouterr().out
    assert "floor HIT" in out
    assert len(rec["filed"]) == 1
    assert rec["filed"][0]["title"] == "Idea: Rebase original interval for g-t-01"
    # The floor path has its OWN reset, and it runs AFTER filing (so the same
    # Idea is not re-filed on every subsequent close). That is pre-existing
    # behaviour this fix deliberately did not touch — the counter-first block
    # lives on the `above_floor` side only, and a genuine floor hit never
    # enters it. The discriminator is that no interval write is attempted.
    assert rec["order"] == ["idea", "reset"], rec["order"]
    assert rec["contract"] == []


def test_dry_run_writes_nothing(tmp_path, capsys):
    mod = _load_module()
    src = tmp_path / "aspirations.jsonl"
    _write_source(src, _mk_goal(interval=6.0, original=6.0))
    rec = _instrument(mod, src, cadence=(8.0, "ok", 5))
    rc = mod.cmd_contract_per_goal(_args(dry_run=True), DETECTOR_CFG,
                                   CONTRACT_CFG)
    assert rc == 0
    assert "DRY-RUN" in capsys.readouterr().out
    assert rec["order"] == [], "dry-run must not reset the counter either"
