"""Unit tests for the  negative-diff self-heal in fresh-eyes-cadence-check.py.

A DOWNWARD count-basis correction (census double-count repair, store surgery)
leaves the stamped WM slot ABOVE the live completed-goal count. Without the
self-heal, diff stays negative and the ritual silently starves until the count
regrows past the stale stamp. The guard re-stamps the slot to the current
count and noops.

Three cases:
1. negative diff → rc=1, wm re-stamp payload carries current count +
   rebaselined_from + preserved timestamp
2. negative diff + wm write failure → rc=1 (noop, fail-open, no re-stamp)
3. positive diff below cadence → rc=1 with NO re-stamp write (guard scoped
   to diff < 0 only)

Tests use monkeypatch to override count_completed_goals(), wm_slot_value(),
_load_yaml(), and subprocess.run so no live state is touched.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
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
    m = _load_cadence_module()
    return m


def _stub_config(goal_cadence: int = 25):
    return {
        "fresh_eyes_review": {
            "goal_cadence": goal_cadence,
            "wm_slot": "last_fresh_eyes_review",
        }
    }


def _patch_helpers(monkeypatch, mod, *, config, current_count, last_slot_value):
    monkeypatch.setattr(mod, "_load_yaml", lambda _p: config)
    monkeypatch.setattr(mod, "count_completed_goals", lambda **_kw: current_count)

    def fake_wm_slot_value(slot_name):
        if slot_name == "last_fresh_eyes_review":
            return last_slot_value
        return None

    monkeypatch.setattr(mod, "wm_slot_value", fake_wm_slot_value)


def test_negative_diff_rebaselines_and_noops(mod, monkeypatch, capsys):
    """last=900 > current=600 → re-stamp slot to 600, rc=1."""
    _patch_helpers(
        monkeypatch, mod,
        config=_stub_config(goal_cadence=25),
        current_count=600,
        last_slot_value={
            "timestamp": "2026-07-01T12:00:00",
            "goals_count_at_last_fire": 900,
        },
    )
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, "input": kwargs.get("input")})
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["fresh-eyes-cadence-check.py"])
    rc = mod.main()
    captured = capsys.readouterr()
    assert rc == 1, f"expected noop (rc=1) but got rc={rc}; out={captured.out!r}"
    assert "re-baselined" in captured.out
    assert len(calls) == 1, f"expected exactly one wm re-stamp write, got {len(calls)}"
    payload = json.loads(calls[0]["input"])
    assert payload["goals_count_at_last_fire"] == 600
    assert payload["rebaselined_from"] == 900
    assert payload["timestamp"] == "2026-07-01T12:00:00", "last real fire timestamp must be preserved"
    assert "last_fresh_eyes_review" in calls[0]["cmd"]


def test_negative_diff_write_failure_still_noops(mod, monkeypatch, capsys):
    """Re-stamp write raising → fail-open noop (rc=1), stderr warning, no crash."""
    _patch_helpers(
        monkeypatch, mod,
        config=_stub_config(goal_cadence=25),
        current_count=600,
        last_slot_value={"goals_count_at_last_fire": 900},
    )

    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["fresh-eyes-cadence-check.py"])
    rc = mod.main()
    captured = capsys.readouterr()
    assert rc == 1, f"expected noop (rc=1) but got rc={rc}; out={captured.out!r}"
    assert "re-baseline write" in captured.err


def test_positive_diff_below_cadence_takes_no_rebaseline(mod, monkeypatch, capsys):
    """diff=10 (>=0, <cadence) → normal noop path, NO wm write."""
    _patch_helpers(
        monkeypatch, mod,
        config=_stub_config(goal_cadence=25),
        current_count=610,
        last_slot_value={"goals_count_at_last_fire": 600},
    )
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["fresh-eyes-cadence-check.py"])
    rc = mod.main()
    captured = capsys.readouterr()
    assert rc == 1, f"expected noop (rc=1) but got rc={rc}; out={captured.out!r}"
    assert "re-baselined" not in captured.out
    assert calls == [], "guard must not write on non-negative diff"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
