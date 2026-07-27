"""Tests for cadence-stale-canary.py () — the cadence-battery
defense-in-depth sibling of stale-sentinel-canary.

Two layers:
  1. Registry integration — the canary tracks exactly the six _cadence_registry
     cadences and every check_cmd script exists on disk.
  2. run() counter logic, driven by an INJECTED check_runner + a monkeypatched
     tmp working-memory.yaml so every case is hermetic (no real WM, no
     aspirations.jsonl mutation, no subprocess, no S3):
       - all-noop -> no fire, counters 0
       - a cadence FIRING for `threshold` consecutive runs -> fires, resets
       - firing then noop -> resets to 0, never fires
       - a check ERROR -> fail-open noop, never fires
       - threshold honored (4 does not fire at stuck=3)
       - cadences tracked independently

The injectable check_runner is the design seam that makes the FIRE path testable
in-process (the sibling sentinel canary must go through subprocess because it has
no such seam). The 1-iteration precheck/close lag that keeps a HEALTHY loop's
count oscillating 0->1->0 is a live-timing property, not modelled here — these
tests drive the counter math directly.
"""

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import _cadence_registry as reg  # underscore module — direct import

spec = importlib.util.spec_from_file_location(
    "cadence_stale_canary", SCRIPTS / "cadence-stale-canary.py"
)
canary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(canary)


# ----------------------------------------------------------- helpers ----------

def _runner(firing_names=(), error_names=()):
    """Injected (check_cmd)->(firing|None, err|None), keyed on cadence name via
    check_cmd equality against the registry."""
    by_cmd = {tuple(c["check_cmd"]): c["name"] for c in reg.cadences()}

    def runner(check_cmd):
        name = by_cmd.get(tuple(check_cmd))
        if name in error_names:
            return None, f"{check_cmd[0]}: boom"
        if name in firing_names:
            return True, None
        return False, None

    return runner


@pytest.fixture
def hermetic(tmp_path, monkeypatch):
    """Point run() at a tmp WM; stub filing + dedup so no real side effects.

    Returns {"wm": <tmp working-memory.yaml>, "filed": [(cadence, stuck), ...]}.
    """
    wm = tmp_path / "working-memory.yaml"
    wm.write_text(yaml.dump({"slots": {}}), encoding="utf-8")

    import wm as wm_mod  # run() does `from wm import wm_path` at call time
    monkeypatch.setattr(wm_mod, "wm_path", lambda: wm, raising=False)
    monkeypatch.setattr(canary, "AGENT_DIR", str(tmp_path), raising=False)

    filed = []
    monkeypatch.setattr(
        canary, "_file_investigate",
        lambda name, stuck, cd, dry: (filed.append((name, stuck)), {"ok": True})[1],
    )
    # Dedup is a live-queue concern; force "no duplicate" so the fire path files.
    monkeypatch.setattr(canary, "_recent_investigate_exists", lambda name: False)
    return {"wm": wm, "filed": filed}


def _counters(wm):
    data = yaml.safe_load(wm.read_text()) or {}
    return (data.get("slots") or {}).get(canary.CANARY_SLOT) or {}


# --------------------------------------------------------- registry -----------

def test_registry_tracks_six_cadences():
    assert len(reg.cadences()) == 6


def test_every_check_cmd_script_exists():
    for c in reg.cadences():
        script = SCRIPTS / c["check_cmd"][0]
        assert script.exists(), f"missing cadence-check script: {script}"


# ------------------------------------------------- run() counter logic --------

def test_all_noop_no_fire(hermetic):
    rep = canary.run(threshold=3, dry_run=False, check_runner=_runner())
    assert rep["investigate_goals_filed"] == []
    assert all(e["new_stuck_count"] == 0 for e in rep["cadences"].values())
    assert hermetic["filed"] == []


def test_persistent_fire_climbs_then_fires_and_resets(hermetic):
    r = _runner(firing_names={"felt-sense"})
    rep1 = canary.run(3, False, check_runner=r)
    assert rep1["cadences"]["felt-sense"]["new_stuck_count"] == 1
    assert rep1["cadences"]["felt-sense"]["fired"] is False
    rep2 = canary.run(3, False, check_runner=r)
    assert rep2["cadences"]["felt-sense"]["new_stuck_count"] == 2
    rep3 = canary.run(3, False, check_runner=r)
    assert rep3["cadences"]["felt-sense"]["fired"] is True
    assert ("felt-sense", 3) in hermetic["filed"]
    # post-fire reset persisted to WM
    assert _counters(hermetic["wm"]).get("felt-sense") == 0


def test_fire_then_noop_resets(hermetic):
    r_fire = _runner(firing_names={"evolution"})
    r_noop = _runner()
    canary.run(3, False, check_runner=r_fire)   # count -> 1
    canary.run(3, False, check_runner=r_fire)   # count -> 2
    rep = canary.run(3, False, check_runner=r_noop)  # noop -> reset
    assert rep["cadences"]["evolution"]["new_stuck_count"] == 0
    assert hermetic["filed"] == []


def test_check_error_is_fail_open_noop(hermetic):
    r = _runner(error_names={"curriculum"})
    rep = None
    for _ in range(4):  # 4 error runs — must never accrue a stuck count
        rep = canary.run(3, False, check_runner=r)
    assert rep["cadences"]["curriculum"]["new_stuck_count"] == 0
    assert rep["cadences"]["curriculum"]["check_error"] is not None
    assert hermetic["filed"] == []


def test_threshold_honored(hermetic):
    r = _runner(firing_names={"fresh-eyes-review"})
    rep = None
    for _ in range(3):  # threshold 4 — three firing runs must NOT fire
        rep = canary.run(4, False, check_runner=r)
    assert rep["cadences"]["fresh-eyes-review"]["new_stuck_count"] == 3
    assert rep["cadences"]["fresh-eyes-review"]["fired"] is False
    assert hermetic["filed"] == []


def test_cadences_tracked_independently(hermetic):
    r = _runner(firing_names={"felt-sense"})  # only felt-sense fires
    rep = None
    for _ in range(3):
        rep = canary.run(3, False, check_runner=r)
    assert rep["cadences"]["felt-sense"]["fired"] is True
    assert rep["cadences"]["evolution"]["new_stuck_count"] == 0
    assert rep["cadences"]["curriculum"]["new_stuck_count"] == 0
