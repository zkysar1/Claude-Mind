"""Unit tests for 4 min_session_goals gate in fresh-eyes-cadence-check.py.

Three cases:
1. no-gate (min_session_goals not set / 0): behaves as before — fire when diff >= cadence
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
    """Load fresh-eyes-cadence-check.py as a module despite the hyphenated name."""
    spec = importlib.util.spec_from_file_location(
        "fresh_eyes_cadence_check",
        str(SCRIPT_DIR / "fresh-eyes-cadence-check.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod(monkeypatch):
    """Provide the loaded module with safe defaults; tests override per-case."""
    m = _load_cadence_module()
    return m


def _stub_config(min_session_goals: int, goal_cadence: int = 25):
    """Return a config dict the script's _load_yaml() will see."""
    return {
        "fresh_eyes_review": {
            "goal_cadence": goal_cadence,
            "wm_slot": "last_fresh_eyes_review",
            "min_session_goals": min_session_goals,
        }
    }


def _patch_helpers(monkeypatch, mod, *, config, current_count, last_slot_value, loop_state_value):
    """Patch the four helpers the gate depends on."""
    monkeypatch.setattr(mod, "_load_yaml", lambda _p: config)
    monkeypatch.setattr(mod, "count_completed_goals", lambda: current_count)

    def fake_wm_slot_value(slot_name):
        if slot_name == "loop_state":
            return loop_state_value
        if slot_name == "last_fresh_eyes_review":
            return last_slot_value
        return None

    monkeypatch.setattr(mod, "wm_slot_value", fake_wm_slot_value)


def test_no_gate_when_min_session_goals_is_zero(mod, monkeypatch, capsys):
    """min_session_goals=0 → legacy fire-when-diff>=cadence behavior preserved."""
    _patch_helpers(
        monkeypatch, mod,
        config=_stub_config(min_session_goals=0, goal_cadence=25),
        current_count=100,
        last_slot_value={"goals_count_at_last_fire": 70},  # diff=30 >= cadence=25
        loop_state_value={"goals_completed_this_session": 0},
    )
    # Simulate argv: bare invocation (no --verbose, no --print-current)
    monkeypatch.setattr(sys, "argv", ["fresh-eyes-cadence-check.py"])
    rc = mod.main()
    captured = capsys.readouterr()
    assert rc == 0, f"expected fire (rc=0) but got rc={rc}; out={captured.out!r}"
    assert "fire" in captured.out


def test_fires_when_session_done_above_threshold(mod, monkeypatch, capsys):
    """min_session_goals=1, session_done=3 → fire (above threshold)."""
    _patch_helpers(
        monkeypatch, mod,
        config=_stub_config(min_session_goals=1, goal_cadence=25),
        current_count=100,
        last_slot_value={"goals_count_at_last_fire": 70},
        loop_state_value={"goals_completed_this_session": 3},
    )
    monkeypatch.setattr(sys, "argv", ["fresh-eyes-cadence-check.py"])
    rc = mod.main()
    captured = capsys.readouterr()
    assert rc == 0, f"expected fire (rc=0) but got rc={rc}; out={captured.out!r}"
    assert "fire" in captured.out


def test_noop_when_session_done_below_threshold(mod, monkeypatch, capsys):
    """min_session_goals=3, session_done=1 → noop (gated)."""
    _patch_helpers(
        monkeypatch, mod,
        config=_stub_config(min_session_goals=3, goal_cadence=25),
        current_count=100,
        last_slot_value={"goals_count_at_last_fire": 70},
        loop_state_value={"goals_completed_this_session": 1},
    )
    monkeypatch.setattr(sys, "argv", ["fresh-eyes-cadence-check.py"])
    rc = mod.main()
    captured = capsys.readouterr()
    assert rc == 1, f"expected noop (rc=1) but got rc={rc}; out={captured.out!r}"
    assert "min_session_goals gate" in captured.out
    assert "session_done=1" in captured.out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
