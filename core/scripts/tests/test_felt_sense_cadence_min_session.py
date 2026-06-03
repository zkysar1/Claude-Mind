"""Unit tests for 4 min_session_goals gate in felt-sense-cadence-check.py.

Three cases mirror the fresh-eyes-cadence-check.py test (same gate shape):
1. no-gate (min_session_goals=0): behaves as before — fire when diff >= cadence
2. above-threshold (session_done >= min_session_goals): fire
3. below-threshold (session_done < min_session_goals): noop (gated)

Tests use monkeypatch to override count_completed_goals(), wm_slot_value(),
and _load_yaml() so no live state is touched.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_DIR = PROJECT_ROOT / "core" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))


def _load_cadence_module():
    """Load felt-sense-cadence-check.py as a module despite the hyphenated name."""
    spec = importlib.util.spec_from_file_location(
        "felt_sense_cadence_check",
        str(SCRIPT_DIR / "felt-sense-cadence-check.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod(monkeypatch):
    """Provide the loaded module."""
    return _load_cadence_module()


def _stub_config(min_session_goals: int, goal_cadence: int = 75):
    """Return a config dict the script's _load_yaml() will see."""
    return {
        "felt_sense": {
            "enabled": True,
            "goal_cadence": goal_cadence,
            "wm_slot": "last_felt_sense_checkin",
            "min_session_goals": min_session_goals,
        }
    }


def _patch_helpers(monkeypatch, mod, *, config, current_count, last_slot_value, loop_state_value):
    """Patch the four helpers the gate depends on."""
    monkeypatch.setattr(mod, "_load_yaml", lambda _p: config)
    monkeypatch.setattr(mod, "count_completed_goals", lambda: current_count)

    def fake_wm_slot_value(slot_name=mod.SLOT_NAME):
        if slot_name == "loop_state":
            return loop_state_value
        if slot_name == mod.SLOT_NAME:
            return last_slot_value
        return None

    monkeypatch.setattr(mod, "wm_slot_value", fake_wm_slot_value)


def test_no_gate_when_min_session_goals_is_zero(mod, monkeypatch, capsys):
    """min_session_goals=0 → legacy fire-when-diff>=cadence behavior preserved."""
    _patch_helpers(
        monkeypatch, mod,
        config=_stub_config(min_session_goals=0, goal_cadence=75),
        current_count=200,
        last_slot_value={"goals_count_at_last_fire": 120},  # diff=80 >= cadence=75
        loop_state_value={"goals_completed_this_session": 0},
    )
    monkeypatch.setattr(sys, "argv", ["felt-sense-cadence-check.py"])
    rc = mod.main()
    captured = capsys.readouterr()
    assert rc == 0, f"expected fire (rc=0) but got rc={rc}; out={captured.out!r}"
    assert "fire" in captured.out


def test_fires_when_session_done_above_threshold(mod, monkeypatch, capsys):
    """min_session_goals=3, session_done=5 → fire (above threshold)."""
    _patch_helpers(
        monkeypatch, mod,
        config=_stub_config(min_session_goals=3, goal_cadence=75),
        current_count=200,
        last_slot_value={"goals_count_at_last_fire": 120},
        loop_state_value={"goals_completed_this_session": 5},
    )
    monkeypatch.setattr(sys, "argv", ["felt-sense-cadence-check.py"])
    rc = mod.main()
    captured = capsys.readouterr()
    assert rc == 0, f"expected fire (rc=0) but got rc={rc}; out={captured.out!r}"
    assert "fire" in captured.out


def test_noop_when_session_done_below_threshold(mod, monkeypatch, capsys):
    """min_session_goals=3, session_done=1 → noop (gated)."""
    _patch_helpers(
        monkeypatch, mod,
        config=_stub_config(min_session_goals=3, goal_cadence=75),
        current_count=200,
        last_slot_value={"goals_count_at_last_fire": 120},
        loop_state_value={"goals_completed_this_session": 1},
    )
    monkeypatch.setattr(sys, "argv", ["felt-sense-cadence-check.py"])
    rc = mod.main()
    captured = capsys.readouterr()
    assert rc == 1, f"expected noop (rc=1) but got rc={rc}; out={captured.out!r}"
    assert "min_session_goals gate" in captured.out
    assert "session_done=1" in captured.out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
