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


def _seed_temp(tmp_path, n_flat, n_drained=0, n_ephemera=0):
    """Create tmp_path/temp/ with n_flat working docs (.md) + n_drained in
    drained/ + n_ephemera pure-ephemera .log/.txt files in temp/ root."""
    temp = tmp_path / "temp"
    temp.mkdir(parents=True, exist_ok=True)
    for i in range(n_flat):
        (temp / f"design-2026-06-02T00-00-{i:02d}.md").write_text("doc", encoding="utf-8")
    if n_drained:
        (temp / "drained").mkdir(exist_ok=True)
        for i in range(n_drained):
            (temp / "drained" / f"old-{i:02d}.md").write_text("drained", encoding="utf-8")
    for i in range(n_ephemera):
        # alternate .log / .txt so both ephemera suffixes are exercised
        suffix = ".log" if i % 2 == 0 else ".txt"
        (temp / f"suite-{i:02d}{suffix}").write_text("ephemera", encoding="utf-8")
    return temp


def _compact(goals=None):
    return {"aspirations": [{"id": "asp-001", "status": "active", "goals": goals or []}]}


def _run(tmp_path, monkeypatch, n_flat, n_drained=0, goals=None, n_ephemera=0):
    _seed_temp(tmp_path, n_flat, n_drained, n_ephemera)
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


# ── Pure-ephemera (.log/.txt) counting (7) ───────────────────────
# Pre-fix, .log/.txt files were invisible to BOTH the drain glob and this
# metric, so ephemera-only accumulation emitted NO flag and grew unbounded.
# The metric now counts ephemera separately and folds it into the combined
# pressure that drives the threshold flags.

def test_temp_pressure_ephemera_counted_separately(tmp_path, monkeypatch):
    # 3 docs + 4 ephemera -> count=3, ephemera_count=4, pressure_count=7,
    # below warn(10) so no flag; the two counts are NOT conflated.
    r = _run(tmp_path, monkeypatch, n_flat=3, n_ephemera=4)
    assert r["count"] == 3
    assert r["ephemera_count"] == 4
    assert r["pressure_count"] == 7
    assert r["flags"] == []


def test_temp_pressure_ephemera_only_triggers_warn(tmp_path, monkeypatch):
    # 0 docs + 12 ephemera -> pressure_count=12 >= warn(10) -> temp_pressure_warn.
    # This is the exact 7 bug: pre-fix, 12 invisible ephemera emitted
    # NO flag; now they are seen.
    r = _run(tmp_path, monkeypatch, n_flat=0, n_ephemera=12)
    assert r["count"] == 0 and r["ephemera_count"] == 12
    assert r["flags"] == ["temp_pressure_warn"]
    assert r["suggested_goal"] is None


def test_temp_pressure_ephemera_only_triggers_drain(tmp_path, monkeypatch):
    # 0 docs + 20 ephemera -> pressure_count=20 >= drain(20) -> temp_drain_needed;
    # the suggested goal names the ephemera purge.
    r = _run(tmp_path, monkeypatch, n_flat=0, n_ephemera=20)
    assert r["count"] == 0 and r["ephemera_count"] == 20
    assert r["flags"] == ["temp_drain_needed"]
    g = r["suggested_goal"]
    assert g is not None and g["priority"] == "HIGH"
    assert g["participants"] == ["agent"]          # capability-routing: agent, not user
    assert "purge" in g["title"].lower() and "20" in g["title"]


def test_temp_pressure_docs_plus_ephemera_combined(tmp_path, monkeypatch):
    # 15 docs + 6 ephemera: neither alone crosses drain(20), combined
    # pressure_count=21 does -> temp_drain_needed. The goal names both the
    # drain (15 docs) and the purge (6 ephemera).
    r = _run(tmp_path, monkeypatch, n_flat=15, n_ephemera=6)
    assert r["count"] == 15 and r["ephemera_count"] == 6 and r["pressure_count"] == 21
    assert r["flags"] == ["temp_drain_needed"]
    g = r["suggested_goal"]
    assert "drain 15" in g["title"] and "purge 6" in g["title"].lower()


def test_temp_pressure_ephemera_clean_when_zero(tmp_path, monkeypatch):
    # No docs, no ephemera -> clean.
    r = _run(tmp_path, monkeypatch, n_flat=0, n_ephemera=0)
    assert r["count"] == 0 and r["ephemera_count"] == 0 and r["pressure_count"] == 0
    assert r["summary"] == "temp-pressure: clean"
    assert r["flags"] == []


def test_temp_pressure_ephemera_dedup_existing_goal(tmp_path, monkeypatch):
    # ephemera pushes combined pressure over drain BUT an open drain goal exists
    # -> temp_drain_pending, no second goal filed.
    goals = [{"id": "g-001-99", "status": "pending",
              "title": "Maintain: drain accumulated temp/ working docs"}]
    r = _run(tmp_path, monkeypatch, n_flat=10, n_ephemera=12, goals=goals)
    assert r["pressure_count"] == 22
    assert r["flags"] == ["temp_drain_pending"]
    assert r["existing_drain_goal"] == "g-001-99"
    assert r["suggested_goal"] is None


# ── One-shot scratch-script ephemera (.py/.sh/.err) counting (7) ──
# Pre-fix, one-shot scratch scripts (build-*.py, orphan-*.py, restart-poller.sh,
# gs.err) in temp/ root were invisible to BOTH the drain glob and this metric,
# so scratch-only accumulation emitted NO flag and grew unbounded — the exact
# 7 gap for a different file class. EPHEMERA_SUFFIXES now includes
# .py/.sh/.err so they count as ephemera alongside .log/.txt.

def test_temp_pressure_scratch_scripts_counted_as_ephemera(tmp_path, monkeypatch):
    # 4 scratch scripts (.py/.sh/.err) + 1 legacy .log = 5 ephemera, 0 docs.
    temp = tmp_path / "temp"
    temp.mkdir(parents=True)
    (temp / "build-fix.py").write_text("x", encoding="utf-8")
    (temp / "orphan-scan.py").write_text("x", encoding="utf-8")
    (temp / "restart-poller.sh").write_text("x", encoding="utf-8")
    (temp / "gs.err").write_text("x", encoding="utf-8")
    (temp / "suite.log").write_text("x", encoding="utf-8")  # legacy class still counts
    monkeypatch.setattr(pe, "AGENT_DIR", tmp_path)
    r = pe.cmd_temp_pressure(_Args(), CONFIG, _compact())
    assert r["count"] == 0
    assert r["ephemera_count"] == 5
    assert r["pressure_count"] == 5
    assert r["flags"] == []  # below warn(10)


def test_temp_pressure_scratch_scripts_not_conflated_with_docs(tmp_path, monkeypatch):
    # A .py/.sh/.err in temp/ root is ephemera, NOT a drainable working doc
    # (.md/.json). The two classes must stay distinct: 2 docs + 3 scratch.
    temp = tmp_path / "temp"
    temp.mkdir(parents=True)
    (temp / "design.md").write_text("doc", encoding="utf-8")
    (temp / "plan.json").write_text("{}", encoding="utf-8")
    (temp / "a.py").write_text("x", encoding="utf-8")
    (temp / "b.sh").write_text("x", encoding="utf-8")
    (temp / "c.err").write_text("x", encoding="utf-8")
    monkeypatch.setattr(pe, "AGENT_DIR", tmp_path)
    r = pe.cmd_temp_pressure(_Args(), CONFIG, _compact())
    assert r["count"] == 2           # .md + .json only
    assert r["ephemera_count"] == 3  # .py + .sh + .err
    assert r["pressure_count"] == 5


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
