#!/usr/bin/env python3
"""test_precheck_temp_pressure.py — precheck-eval.py cmd_temp_pressure contract
(file-model normalization Phase 5).

Pins the temp/ accumulation-pressure check that keeps temp/ from becoming the
new slush directory: it counts UNDRAINED working docs directly under the bound
agent's temp/ (excluding the drained/ audit subdir) and emits

  - no flag                  below warn_threshold
  - temp_pressure_warn       at >= warn_threshold (visible nudge, no goal)
  - temp_drain_needed        at >= drain_goal_threshold (+ suggested HIGH goal)
  - temp_drain_pending       at >= drain_goal_threshold when an open drain goal
                             already exists (deduped — no second goal filed)

AGENT_DIR is a module global imported from _paths; the tests monkeypatch it to a
tmp dir so the count targets a controlled temp/ rather than the live agent.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

spec = importlib.util.spec_from_file_location("precheck_eval", SCRIPT_DIR / "precheck-eval.py")
pe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pe)

CONFIG = {"temp_pressure": {"warn_threshold": 10, "drain_goal_threshold": 20}}


class _Args:
    pass


def _seed_temp(tmp_path, n_flat, n_drained=0):
    """Create tmp_path/temp/ with n_flat working docs + n_drained in drained/."""
    temp = tmp_path / "temp"
    temp.mkdir(parents=True, exist_ok=True)
    for i in range(n_flat):
        (temp / f"design-2026-06-02T00-00-{i:02d}.md").write_text("doc", encoding="utf-8")
    if n_drained:
        (temp / "drained").mkdir(exist_ok=True)
        for i in range(n_drained):
            (temp / "drained" / f"old-{i:02d}.md").write_text("drained", encoding="utf-8")
    return temp


def _compact(goals=None):
    return {"aspirations": [{"id": "asp-001", "status": "active", "goals": goals or []}]}


def _run(tmp_path, monkeypatch, n_flat, n_drained=0, goals=None):
    _seed_temp(tmp_path, n_flat, n_drained)
    monkeypatch.setattr(pe, "AGENT_DIR", tmp_path)
    return pe.cmd_temp_pressure(_Args(), CONFIG, _compact(goals))


def test_temp_pressure_clean(tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, n_flat=0)
    assert r["count"] == 0 and r["flags"] == []
    assert r["suggested_goal"] is None


def test_temp_pressure_below_warn_no_flag(tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, n_flat=9)
    assert r["count"] == 9 and r["flags"] == []


def test_temp_pressure_warn_at_threshold(tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, n_flat=10)
    assert r["count"] == 10 and r["flags"] == ["temp_pressure_warn"]
    assert r["suggested_goal"] is None  # warn never files a goal


def test_temp_pressure_drain_needed_at_threshold(tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, n_flat=20)
    assert r["count"] == 20 and r["flags"] == ["temp_drain_needed"]
    g = r["suggested_goal"]
    assert g is not None and g["priority"] == "HIGH"
    assert g["participants"] == ["agent"]          # capability-routing: agent, not user
    assert "drain" in g["title"].lower() and "temp" in g["title"].lower()


def test_temp_pressure_drained_subdir_excluded(tmp_path, monkeypatch):
    # 5 live + 50 already-drained -> only the 5 live count (drained/ is the
    # audit archive, already encoded into the tree).
    r = _run(tmp_path, monkeypatch, n_flat=5, n_drained=50)
    assert r["count"] == 5 and r["flags"] == []


def test_temp_pressure_dedup_existing_drain_goal(tmp_path, monkeypatch):
    # 25 undrained docs BUT an open drain-temp goal already exists -> no second
    # goal filed; emits temp_drain_pending instead.
    goals = [{"id": "g-001-99", "status": "pending",
              "title": "Maintain: drain accumulated temp/ working docs"}]
    r = _run(tmp_path, monkeypatch, n_flat=25, goals=goals)
    assert r["count"] == 25
    assert r["flags"] == ["temp_drain_pending"]
    assert r["existing_drain_goal"] == "g-001-99"
    assert r["suggested_goal"] is None


def test_temp_pressure_warn_range_ignores_existing_drain_goal(tmp_path, monkeypatch):
    # In the warn range (10-19) an existing drain goal is irrelevant — dedup only
    # gates the drain-threshold goal-filing, so this still emits temp_pressure_warn
    # (NOT temp_drain_pending, which is a drain-threshold-only signal).
    goals = [{"id": "g-001-99", "status": "pending",
              "title": "Maintain: drain accumulated temp/ working docs"}]
    r = _run(tmp_path, monkeypatch, n_flat=15, goals=goals)
    assert r["count"] == 15
    assert r["flags"] == ["temp_pressure_warn"]
    assert r["suggested_goal"] is None


def test_temp_pressure_json_files_count(tmp_path, monkeypatch):
    # Working docs may be .md or .json; both count toward pressure.
    temp = tmp_path / "temp"
    temp.mkdir(parents=True)
    for i in range(6):
        (temp / f"a-{i}.md").write_text("x", encoding="utf-8")
    for i in range(6):
        (temp / f"b-{i}.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(pe, "AGENT_DIR", tmp_path)
    r = pe.cmd_temp_pressure(_Args(), CONFIG, _compact())
    assert r["count"] == 12 and r["flags"] == ["temp_pressure_warn"]


def test_temp_pressure_missing_config_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "AGENT_DIR", tmp_path)
    with pytest.raises(KeyError):
        pe.cmd_temp_pressure(_Args(), {}, _compact())


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
