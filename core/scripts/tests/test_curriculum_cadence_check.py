"""Unit tests for curriculum-cadence-check.py ().

The cadence gate is time-based: exit 0 (fire) when >= interval_hours have
elapsed since the last curriculum evaluation (or it was never evaluated),
exit 1 (noop) otherwise. Fail-open: any config/state error → noop.

Tests monkeypatch _load_yaml() (config) and wm_slot_value() (the last-eval WM
slot) so no live state is touched. Slot timestamps are computed relative to the
REAL datetime.now() so the script's own clock read compares correctly — the
tests are stable as long as they run in well under an hour of wall-clock.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_DIR = PROJECT_ROOT / "core" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))


def _load_module():
    """Load curriculum-cadence-check.py as a module despite the hyphenated name."""
    spec = importlib.util.spec_from_file_location(
        "curriculum_cadence_check",
        str(SCRIPT_DIR / "curriculum-cadence-check.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_module()


def _cfg(*, enabled=True, interval_hours=24, wm_slot="last_curriculum_eval", present=True):
    """Build the config dict the script's _load_yaml() will see."""
    if not present:
        return {}
    block = {"wm_slot": wm_slot, "interval_hours": interval_hours}
    if enabled is not None:
        block["enabled"] = enabled
    return {"curriculum_cadence": block}


def _iso_hours_ago(hours: float) -> str:
    return (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")


def _run(mod, monkeypatch, *, config, slot_value):
    monkeypatch.setattr(mod, "_load_yaml", lambda _p: config)
    monkeypatch.setattr(mod, "wm_slot_value", lambda _slot: slot_value)
    monkeypatch.setattr(sys, "argv", ["curriculum-cadence-check.py"])
    return mod.main()


def test_fire_when_slot_unset(mod, monkeypatch, capsys):
    """Slot never stamped (None) → fire (first evaluation)."""
    rc = _run(mod, monkeypatch, config=_cfg(), slot_value=None)
    out = capsys.readouterr().out
    assert rc == 0, f"expected fire; out={out!r}"
    assert "fire" in out


def test_noop_when_recent(mod, monkeypatch, capsys):
    """Evaluated just now → noop (interval not elapsed)."""
    rc = _run(mod, monkeypatch, config=_cfg(interval_hours=24), slot_value=_iso_hours_ago(0.01))
    out = capsys.readouterr().out
    assert rc == 1, f"expected noop; out={out!r}"
    assert "noop" in out


def test_fire_when_stale(mod, monkeypatch, capsys):
    """30h since last eval, 24h interval → fire."""
    rc = _run(mod, monkeypatch, config=_cfg(interval_hours=24), slot_value=_iso_hours_ago(30))
    out = capsys.readouterr().out
    assert rc == 0, f"expected fire; out={out!r}"
    assert "fire" in out


def test_fire_at_exact_interval(mod, monkeypatch):
    """Exactly at the interval boundary (>=) → fire."""
    rc = _run(mod, monkeypatch, config=_cfg(interval_hours=24), slot_value=_iso_hours_ago(24.5))
    assert rc == 0


def test_custom_interval_not_yet_elapsed(mod, monkeypatch):
    """30h since last eval but interval widened to 48h → noop."""
    rc = _run(mod, monkeypatch, config=_cfg(interval_hours=48), slot_value=_iso_hours_ago(30))
    assert rc == 1


def test_noop_when_disabled(mod, monkeypatch, capsys):
    """enabled: false → noop regardless of elapsed time."""
    rc = _run(mod, monkeypatch, config=_cfg(enabled=False), slot_value=_iso_hours_ago(999))
    out = capsys.readouterr().out
    assert rc == 1, f"expected noop; out={out!r}"


def test_noop_when_no_config_block(mod, monkeypatch):
    """No curriculum_cadence block → noop (backward-compatible / unconfigured)."""
    rc = _run(mod, monkeypatch, config=_cfg(present=False), slot_value=None)
    assert rc == 1


def test_dict_shape_stamp(mod, monkeypatch):
    """Slot stamped as {'timestamp': <now>} dict → parsed, noop when recent."""
    rc = _run(mod, monkeypatch, config=_cfg(), slot_value={"timestamp": _iso_hours_ago(0.01)})
    assert rc == 1


def test_dict_shape_stamp_stale(mod, monkeypatch):
    """Dict-shape stamp that is stale → fire."""
    rc = _run(mod, monkeypatch, config=_cfg(interval_hours=24), slot_value={"timestamp": _iso_hours_ago(50)})
    assert rc == 0


def test_fail_open_on_config_read_error(mod, monkeypatch):
    """_load_yaml returns None (parse failure) → noop (fail-open, never blocks)."""
    rc = _run(mod, monkeypatch, config=None, slot_value=None)
    assert rc == 1


def test_unparseable_slot_fires(mod, monkeypatch):
    """Garbage slot timestamp → treated as never-evaluated → fire."""
    rc = _run(mod, monkeypatch, config=_cfg(), slot_value="not-a-timestamp")
    assert rc == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
