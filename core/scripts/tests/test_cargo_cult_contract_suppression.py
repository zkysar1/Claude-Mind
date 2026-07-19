"""0: cadence-aware auto-contract suppression in cargo-cult-detector.

For selection-gated recurring goals on a busy loop, the ACTUAL firing cadence
is set by selector competition, not the timer — contracting interval_hours is
INERT (cannot raise the firing rate) and only manufactures streak-break noise
plus floor-hit "Idea: Rebase original interval" treadmills (g-001-01: 25/25
recorded fires "broke"; 15+ rebase Ideas fleet-wide). The fix: before
contracting, compare the recent actual cadence (median of the last N
streak-breaks.jsonl actual_elapsed_hours) against streak_mult x interval; when
actual far exceeds interval, SKIP contraction and file/refresh ONE deduped
rebase-UP Idea instead.

Load-bearing regressions pinned here:
  - suppress path: actual >> interval skips contraction, logs SUPPRESSED,
    files the rebase-UP Idea, resets consecutive_deep
  - contract-as-today: actual ~= interval (or insufficient data) contracts
    exactly as before
  - treadmill regression: a floor-pinned selection-gated goal gets the
    rebase-UP framing, NOT the floor-hit "rebase original DOWN" framing
  - guard-487: unreadable input fails CLOSED (suppress, no counter reset —
    retry next close)

Run: STORAGE_BACKEND=local py -3 -m pytest core/scripts/tests/test_cargo_cult_contract_suppression.py -v
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
DETECTOR_PY = CORE_SCRIPTS / "cargo-cult-detector.py"

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
        "cargo_cult_detector_suppression", DETECTOR_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_breaks(path: Path, goal_id: str, values, extra_lines=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for v in values:
            f.write(json.dumps({
                "timestamp": "2026-07-15T00:00:00", "goal_id": goal_id,
                "expected_interval_hours": 1.78,
                "actual_elapsed_hours": v, "processed": False,
            }) + "\n")
        for raw in extra_lines:
            f.write(raw + "\n")


def _write_source(path: Path, goal: dict, sibling_goals=()):
    asp = {"id": "asp-t", "status": "active",
           "goals": [goal, *sibling_goals]}
    path.write_text(json.dumps(asp) + "\n", encoding="utf-8")


def _mk_goal(interval=1.78, original=4.0, deep=3, goal_id="g-t-01"):
    return {
        "id": goal_id, "title": "Test recurring goal", "recurring": True,
        "status": "pending", "interval_hours": interval,
        "original_interval_hours": original, "consecutive_deep": deep,
    }


def _args(goal_id="g-t-01", dry_run=False):
    return argparse.Namespace(goal_id=goal_id, source="world",
                              dry_run=dry_run)


def _instrument(mod, tmp_src, cadence_result):
    """Monkeypatch the write/IO seams; return the call recorder."""
    calls = {"contract": [], "reset": [], "filed": []}
    mod.source_path = lambda source, agent_override=None: tmp_src
    mod.update_interval_hours = (
        lambda gid, src, new, orig, had_original:
        calls["contract"].append((gid, new, orig)) or True)
    mod.reset_consecutive_deep = (
        lambda gid, src: calls["reset"].append(gid) or True)
    mod.file_idea = (
        lambda asp_id, source, idea:
        calls["filed"].append(idea) or "g-t-99")
    mod._load_streak_mult = lambda: 2.0
    if cadence_result is not None:
        mod._recent_actual_cadence = (
            lambda gid, window, min_samples, log_path=None: cadence_result)
    return calls


# ---- _recent_actual_cadence unit tests (real implementation, tmp file) ----

def test_cadence_median_over_window(tmp_path):
    mod = _load_module()
    log = tmp_path / "streak-breaks.jsonl"
    # 6 entries for the goal — window 5 keeps the LAST 5 (8..18, median 12).
    _write_breaks(log, "g-t-01", [5.5, 8, 10, 12, 14, 18])
    _write_breaks(tmp_path / "other.jsonl", "g-other", [99])
    median, status, samples = mod._recent_actual_cadence(
        "g-t-01", window=5, min_samples=3, log_path=log)
    assert status == "ok" and samples == 5
    assert median == 12

def test_cadence_filters_other_goals(tmp_path):
    mod = _load_module()
    log = tmp_path / "streak-breaks.jsonl"
    _write_breaks(log, "g-other", [99, 99, 99, 99])
    median, status, samples = mod._recent_actual_cadence(
        "g-t-01", window=5, min_samples=3, log_path=log)
    assert median is None and status == "insufficient" and samples == 0

def test_cadence_insufficient_samples(tmp_path):
    mod = _load_module()
    log = tmp_path / "streak-breaks.jsonl"
    _write_breaks(log, "g-t-01", [8, 10])
    median, status, samples = mod._recent_actual_cadence(
        "g-t-01", window=5, min_samples=3, log_path=log)
    assert median is None and status == "insufficient" and samples == 2

def test_cadence_missing_file_is_no_signal(tmp_path):
    mod = _load_module()
    median, status, samples = mod._recent_actual_cadence(
        "g-t-01", window=5, min_samples=3,
        log_path=tmp_path / "absent.jsonl")
    assert median is None and status == "insufficient" and samples == 0

def test_cadence_corrupt_line_skipped_with_warn(tmp_path, capsys):
    mod = _load_module()
    log = tmp_path / "streak-breaks.jsonl"
    _write_breaks(log, "g-t-01", [8, 10, 12], extra_lines=["{corrupt"])
    median, status, samples = mod._recent_actual_cadence(
        "g-t-01", window=5, min_samples=3, log_path=log)
    assert status == "ok" and samples == 3 and median == 10
    assert "corrupt" in capsys.readouterr().err


# ---- cmd_contract_per_goal integration (seams monkeypatched) ----

def test_suppress_fires_when_actual_far_exceeds_interval(tmp_path, capsys):
    mod = _load_module()
    src = tmp_path / "aspirations.jsonl"
    _write_source(src, _mk_goal(interval=4.0, original=4.0))
    calls = _instrument(mod, src, cadence_result=(10.0, "ok", 5))
    rc = mod.cmd_contract_per_goal(_args(), DETECTOR_CFG, CONTRACT_CFG)
    out = capsys.readouterr().out
    assert rc == 0
    assert "SUPPRESSED" in out and "regime-c selection-gated" in out
    assert calls["contract"] == []          # contraction skipped
    assert len(calls["filed"]) == 1         # rebase-UP Idea filed
    assert calls["filed"][0]["title"] == (
        "Idea: Rebase interval UP for g-t-01 (selection-gated)")
    assert calls["reset"] == ["g-t-01"]     # counter reset (no re-trigger)

def test_contract_proceeds_when_actual_matches_interval(tmp_path, capsys):
    mod = _load_module()
    src = tmp_path / "aspirations.jsonl"
    _write_source(src, _mk_goal(interval=4.0, original=4.0))
    # actual 6h <= 2.0 x 4h boundary -> no suppression, contract as today.
    calls = _instrument(mod, src, cadence_result=(6.0, "ok", 5))
    rc = mod.cmd_contract_per_goal(_args(), DETECTOR_CFG, CONTRACT_CFG)
    assert rc == 0
    assert calls["contract"] == [("g-t-01", 2.67, 4.0)]
    assert calls["filed"] == []
    assert calls["reset"] == ["g-t-01"]

def test_contract_proceeds_on_insufficient_data(tmp_path):
    mod = _load_module()
    src = tmp_path / "aspirations.jsonl"
    _write_source(src, _mk_goal(interval=4.0, original=4.0))
    # No lateness evidence (goal fires on time) -> legitimate contraction.
    calls = _instrument(mod, src, cadence_result=(None, "insufficient", 1))
    rc = mod.cmd_contract_per_goal(_args(), DETECTOR_CFG, CONTRACT_CFG)
    assert rc == 0
    assert calls["contract"] == [("g-t-01", 2.67, 4.0)]
    assert calls["filed"] == []

def test_treadmill_regression_floor_pinned_gets_rebase_up(tmp_path, capsys):
    """A floor-pinned selection-gated goal (proposed < floor AND actual >>
    interval) must get the rebase-UP Idea, NOT the floor-hit 'Rebase original
    interval' framing — the exact g-001-01 treadmill shape."""
    mod = _load_module()
    src = tmp_path / "aspirations.jsonl"
    # 1.78/1.5 = 1.19 < floor 1.32 (0.33 x 4.0) -> floor-pinned.
    _write_source(src, _mk_goal(interval=1.78, original=4.0))
    calls = _instrument(mod, src, cadence_result=(10.9, "ok", 5))
    rc = mod.cmd_contract_per_goal(_args(), DETECTOR_CFG, CONTRACT_CFG)
    out = capsys.readouterr().out
    assert rc == 0
    assert "SUPPRESSED" in out
    assert "floor HIT" not in out           # old escalation never reached
    assert calls["contract"] == []
    assert len(calls["filed"]) == 1
    assert "Rebase interval UP" in calls["filed"][0]["title"]
    assert "Rebase original interval" not in calls["filed"][0]["title"]

def test_rebase_up_dedups_against_pending_rebase_up(tmp_path, capsys):
    mod = _load_module()
    src = tmp_path / "aspirations.jsonl"
    pending = {"id": "g-t-77", "status": "pending",
               "title": "Idea: Rebase interval UP for g-t-01 (selection-gated)"}
    _write_source(src, _mk_goal(interval=4.0, original=4.0),
                  sibling_goals=[pending])
    calls = _instrument(mod, src, cadence_result=(10.0, "ok", 5))
    rc = mod.cmd_contract_per_goal(_args(), DETECTOR_CFG, CONTRACT_CFG)
    out = capsys.readouterr().out
    assert rc == 0
    assert "dedup hit" in out and "g-t-77" in out
    assert calls["filed"] == []
    assert calls["reset"] == ["g-t-01"]     # reset still happens on dedup

def test_rebase_up_dedups_against_pending_floor_hit_idea(tmp_path, capsys):
    mod = _load_module()
    src = tmp_path / "aspirations.jsonl"
    pending = {"id": "g-t-88", "status": "in-progress",
               "title": "Idea: Rebase original interval for g-t-01"}
    _write_source(src, _mk_goal(interval=4.0, original=4.0),
                  sibling_goals=[pending])
    calls = _instrument(mod, src, cadence_result=(10.0, "ok", 5))
    rc = mod.cmd_contract_per_goal(_args(), DETECTOR_CFG, CONTRACT_CFG)
    out = capsys.readouterr().out
    assert rc == 0
    assert "dedup hit" in out and "g-t-88" in out
    assert calls["filed"] == []

def test_unreadable_input_fails_closed_without_reset(tmp_path, capsys):
    """guard-487: suppression-gate input unparseable -> treat as suppressed.
    No contraction, no Idea, and NO counter reset (transient IO error retries
    on the next close via the persistent consecutive_deep counter)."""
    mod = _load_module()
    src = tmp_path / "aspirations.jsonl"
    _write_source(src, _mk_goal(interval=4.0, original=4.0))
    calls = _instrument(mod, src, cadence_result=(None, "unreadable", 0))
    rc = mod.cmd_contract_per_goal(_args(), DETECTOR_CFG, CONTRACT_CFG)
    out = capsys.readouterr().out
    assert rc == 0
    assert "SUPPRESSED (input unreadable)" in out and "guard-487" in out
    assert calls["contract"] == []
    assert calls["filed"] == []
    assert calls["reset"] == []             # retry semantics: counter kept

def test_suppress_dry_run_files_nothing(tmp_path, capsys):
    mod = _load_module()
    src = tmp_path / "aspirations.jsonl"
    _write_source(src, _mk_goal(interval=4.0, original=4.0))
    calls = _instrument(mod, src, cadence_result=(10.0, "ok", 5))
    rc = mod.cmd_contract_per_goal(_args(dry_run=True),
                                   DETECTOR_CFG, CONTRACT_CFG)
    out = capsys.readouterr().out
    assert rc == 0
    assert "SUPPRESSED" in out and "DRY-RUN" in out
    assert calls["contract"] == [] and calls["filed"] == []
    assert calls["reset"] == []
