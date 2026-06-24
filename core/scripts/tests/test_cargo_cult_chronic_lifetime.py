""": cargo-cult-detector chronic-low lifetime-hit-rate detection.

The consecutive_routine trigger flags a recurring goal only when it returns
routine N times IN A ROW. A goal that is chronically-but-INTERMITTENTLY
low-signal (e.g. 1 genuine deep in 20 runs, but never 3 routine in a row)
slips through. g-317-02 adds a lifetime substantive-hit tally
(substantive_hits / substantive_runs, written by recurring-close.sh) and a
"chronic" flag in --audit-all that surfaces such goals even when
consecutive_routine == 0.

Critically, the rate uses substantive_runs (TRACKED closes counted from
field-introduction), NOT achievedCount (lifetime run history) -- so a legacy
goal with a large achievedCount but few tracked closes is NOT falsely flagged
as chronic on day 1. That baseline-poisoning guard is the load-bearing test.

Run: py -3 -m pytest core/scripts/tests/test_cargo_cult_chronic_lifetime.py -v
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

# A real chronic ROW tag carries digits ("[chronic 0/3]"); the explanatory
# legend line carries letters ("[chronic hits/runs]"). This pattern matches
# only an actual tagged row, so assertions don't false-match the legend.
_CHRONIC_ROW_TAG = re.compile(r"\[chronic \d")

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
DETECTOR_PY = CORE_SCRIPTS / "cargo-cult-detector.py"

CHRONIC_CFG = {
    "chronic_hit_rate_threshold": 0.15,
    "chronic_min_runs": 8,
    "chronic_score_weight": 2.0,
}


def _load_module():
    spec = importlib.util.spec_from_file_location("cargo_cult_detector", DETECTOR_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- Test 1: _score_recurring lifetime computation (pure unit) ----

def test_score_recurring_lifetime_rate():
    mod = _load_module()
    # 1 genuine deep in 20 tracked runs -> rate 0.05.
    s = mod._score_recurring({
        "id": "g-x", "title": "t", "interval_hours": 24,
        "achievedCount": 20, "consecutive_routine": 0,
        "substantive_hits": 1, "substantive_runs": 20,
    })
    assert s["substantive_hits"] == 1
    assert s["substantive_runs"] == 20
    assert s["lifetime_hit_rate"] == 0.05, s

    # No tracked data -> rate 1.0 (assume productive, never a false flag).
    s2 = mod._score_recurring({
        "id": "g-y", "title": "t", "interval_hours": 24,
        "achievedCount": 5, "consecutive_routine": 0,
    })
    assert s2["substantive_runs"] == 0
    assert s2["lifetime_hit_rate"] == 1.0, s2

    # hits cannot exceed runs -> rate clamped to 1.0 (defensive).
    s3 = mod._score_recurring({
        "id": "g-z", "title": "t", "interval_hours": 24,
        "achievedCount": 9, "consecutive_routine": 0,
        "substantive_hits": 9, "substantive_runs": 9,
    })
    assert s3["lifetime_hit_rate"] == 1.0, s3
    print("PASS (lifetime_hit_rate = substantive_hits / substantive_runs)")


# ---- Helper: drive cmd_audit_all with controlled goals + the REAL scorer ----

def _run_audit(monkeypatch, goals, cfg=CHRONIC_CFG):
    mod = _load_module()
    captured: dict = {}
    monkeypatch.setattr(mod, "_recent_audit_all_batch", lambda hours: False)
    monkeypatch.setattr(mod, "discover_agents", lambda: [])
    monkeypatch.setattr(mod, "is_artifact_producing", lambda g: (False, None))
    monkeypatch.setattr(mod, "_gate_log", lambda *a, **k: None)
    monkeypatch.setattr(
        mod, "_iter_recurring_goals",
        lambda source, agent_override=None: (
            iter([({"id": "asp-001"}, g) for g in goals])
            if source == "world" else iter([])))
    # REAL _score_recurring + REAL _propose_new_interval (interval present).
    monkeypatch.setattr(mod, "source_path", lambda s: Path("nonexistent-zzz-test"))
    monkeypatch.setattr(mod, "file_idea",
                        lambda asp, src, idea: captured.update(idea) or "g-NEW")

    class Args:
        dry_run = False

    rc = mod.cmd_audit_all(Args(), cfg)
    return rc, captured


# ---- Test 2: chronic goal with cons_routine == 0 surfaces + is tagged ----

def test_chronic_goal_with_zero_streak_is_flagged(monkeypatch):
    chronic = {
        "id": "g-chronic", "title": "Chronic low-signal",
        "interval_hours": 24, "achievedCount": 20, "consecutive_routine": 0,
        "substantive_hits": 1, "substantive_runs": 20,  # rate 0.05 < 0.15
    }
    rc, captured = _run_audit(monkeypatch, [chronic])
    assert rc in (0, None), f"expected file success, rc={rc}"
    desc = captured.get("description", "")
    assert desc, "no description -- file_idea not called; chronic goal was not surfaced"
    assert "g-chronic" in desc, f"chronic goal missing from batch:\n{desc}"
    assert "[chronic 1/20]" in desc, f"chronic tag missing:\n{desc}"
    assert "| lifetime_rate |" in desc, "lifetime_rate column header missing"
    print("PASS (chronic goal, consecutive_routine==0, surfaced + [chronic] tagged)")


# ---- Test 3: baseline guard -- low TRACKED runs not flagged despite high achievedCount ----

def test_low_tracked_runs_not_flagged_despite_high_achievedcount(monkeypatch):
    # Legacy goal: large achievedCount history, but only 3 TRACKED closes since
    # substantive tracking began. rate would be 0.0, yet data is insufficient
    # (3 < chronic_min_runs 8) -> MUST NOT flag chronic. This is the
    # baseline-poisoning guard: the rate uses substantive_runs, not achievedCount.
    legacy = {
        "id": "g-legacy", "title": "Legacy high-history low-tracked",
        "interval_hours": 24, "achievedCount": 50, "consecutive_routine": 0,
        "substantive_hits": 0, "substantive_runs": 3,
    }
    # A streak goal to keep the batch non-empty (cons_routine >= 1).
    streak = {
        "id": "g-streak", "title": "Streak goal",
        "interval_hours": 24, "achievedCount": 10, "consecutive_routine": 2,
        "substantive_hits": 5, "substantive_runs": 10,
    }
    rc, captured = _run_audit(monkeypatch, [legacy, streak])
    desc = captured.get("description", "")
    assert "g-streak" in desc, f"streak goal should be present:\n{desc}"
    assert "g-legacy" not in desc, (
        "legacy goal with only 3 tracked runs was wrongly surfaced "
        f"(baseline-poisoning guard failed):\n{desc}")
    assert not _CHRONIC_ROW_TAG.search(desc), (
        f"no ROW should be chronic-tagged here (legend mention is ok):\n{desc}")
    print("PASS (low tracked-runs goal NOT flagged despite high achievedCount)")


# ---- Test 4: healthy goal (high lifetime rate) not flagged ----

def test_healthy_goal_not_flagged(monkeypatch):
    # Plenty of tracked runs, strong lifetime rate (0.6), zero streak.
    healthy = {
        "id": "g-healthy", "title": "Healthy productive goal",
        "interval_hours": 24, "achievedCount": 20, "consecutive_routine": 0,
        "substantive_hits": 12, "substantive_runs": 20,  # rate 0.6 > 0.15
    }
    rc, captured = _run_audit(monkeypatch, [healthy])
    desc = captured.get("description", "")
    # cons_routine 0 AND not chronic -> filtered out -> empty batch, no filing.
    assert "g-healthy" not in desc, (
        f"healthy goal (rate 0.6) wrongly flagged chronic:\n{desc}")
    print("PASS (healthy high-lifetime-rate goal not flagged)")
