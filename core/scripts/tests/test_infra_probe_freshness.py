"""test_infra_probe_freshness.py --  / rb-4013 probe-store freshness gate.

Verifies infra-health.py `cmd_probe_freshness`: the gate infra-streak-notify.sh
uses so a STALE probe store cannot masquerade as healthy. streak-alert filters
to failures within window_hours; if the NEWEST probe across every component is
older than window_hours, the recency filter has no in-window data and a 0-alert
result is a false-healthy artifact of staleness -- the 2026-07-18 g-249-06
incident (12:08 run reported alert_count=0 while CI had been failing 10 days;
15:00 run WITH a fresh check-all surfaced 5 streaks).

Cases: fresh store (not stale), the 10-day incident shape (stale), empty store
(stale), the mixed newest-wins scoping decision (a single fresh probe means the
store WAS refreshed -> not stale), --window-hours override, and never-probed
components excluded from consideration.

infra-health.py is hyphenated (not import-able as a module name), so it is
loaded by file path via importlib -- the same indirection its sibling unit test
(test_infra_health_staleness.py) uses.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "infra_health_mod", CORE_SCRIPTS / "infra-health.py"
)
ih = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ih)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


class _Args:
    def __init__(self, window_hours=None):
        self.window_hours = window_hours


def _run(monkeypatch, capsys, components, window_hours=None):
    """Run cmd_probe_freshness against a controlled component map, return parsed JSON.

    Pins _load_streak_config so the default window (6.0) does not depend on the
    live aspirations.yaml, and monkeypatches load_health so no real store is read.
    """
    monkeypatch.setattr(ih, "load_health", lambda: {"components": components})
    monkeypatch.setattr(ih, "_load_streak_config", lambda: (3, 6.0))
    ih.cmd_probe_freshness(_Args(window_hours))
    return json.loads(capsys.readouterr().out)


def test_fresh_store_not_stale(monkeypatch, capsys):
    now = datetime.now()
    comps = {"ci": {"last_success": _iso(now - timedelta(hours=1))}}
    r = _run(monkeypatch, capsys, comps)
    assert r["stale"] is False
    assert r["components_considered"] == 1
    assert r["window_hours"] == 6.0
    assert r["newest_age_hours"] is not None and r["newest_age_hours"] <= 1.2


def test_stale_store_the_false_healthy_incident_shape(monkeypatch, capsys):
    # : newest probe 9-10 days old -> whole store stale -> a 0-alert
    # streak result would be false-healthy. This is the exact failure the gate
    # exists to prevent.
    now = datetime.now()
    comps = {
        "ci": {"last_failure": _iso(now - timedelta(days=10)), "consecutive_failures": 5},
        "cloud-place": {"last_success": _iso(now - timedelta(days=9))},
    }
    r = _run(monkeypatch, capsys, comps)
    assert r["stale"] is True
    assert r["components_considered"] == 2
    assert r["newest_age_hours"] > 6.0  # newest is the 9-day success


def test_empty_store_is_stale(monkeypatch, capsys):
    r = _run(monkeypatch, capsys, {})
    assert r["stale"] is True
    assert r["components_considered"] == 0
    assert r["newest_probe"] is None
    assert r["newest_age_hours"] is None


def test_mixed_newest_wins_not_stale(monkeypatch, capsys):
    # The load-bearing scoping decision: freshness answers "was ANYTHING probed
    # within the window", so ONE fresh component (the store was refreshed) makes
    # the whole result trustworthy even alongside a long-stale component. Matches
    # _component_staleness's max-across-results semantics applied store-wide.
    now = datetime.now()
    comps = {
        "old": {"last_failure": _iso(now - timedelta(days=10))},
        "fresh": {"last_success": _iso(now - timedelta(hours=2))},
    }
    r = _run(monkeypatch, capsys, comps)
    assert r["stale"] is False
    assert r["newest_age_hours"] <= 2.2


def test_window_override_tightens_freshness(monkeypatch, capsys):
    # --window-hours override: a 2h-old probe is fresh under the default 6h
    # window but stale under a tightened 1h window.
    now = datetime.now()
    comps = {"ci": {"last_success": _iso(now - timedelta(hours=2))}}
    r_default = _run(monkeypatch, capsys, comps)
    assert r_default["stale"] is False
    r_tight = _run(monkeypatch, capsys, comps, window_hours=1.0)
    assert r_tight["stale"] is True
    assert r_tight["window_hours"] == 1.0


def test_never_probed_component_excluded(monkeypatch, capsys):
    # A component that never recorded a result is neither fresh nor stale
    # evidence -- it must not count toward considered nor affect newest.
    now = datetime.now()
    comps = {
        "probed": {"last_success": _iso(now - timedelta(hours=1))},
        "never": {"last_success": None, "last_failure": None},
    }
    r = _run(monkeypatch, capsys, comps)
    assert r["components_considered"] == 1
    assert r["stale"] is False


def test_boundary_exactly_at_window_not_stale(monkeypatch, capsys):
    # Mirrors _component_staleness's strict > comparison: age == window is fresh.
    now = datetime.now()
    comps = {"ci": {"last_success": _iso(now - timedelta(hours=6))}}
    r = _run(monkeypatch, capsys, comps, window_hours=6.0)
    # newest_age_hours rounds to ~6.0; stale is (age > 6.0), so exactly-6.0 is False.
    assert r["stale"] is False
