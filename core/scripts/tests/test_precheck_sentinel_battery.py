"""Tests for precheck-sentinel-battery.py + _sentinel_registry.py (3).

Pins:
  1. Battery lists a set sentinel and omits null/absent ones.
  2. fired_key dicts with fired!=true are NOT listed (matches the consumer
     phases' `IF signal.fired == true` gate and the canary's _is_set).
  3. Registry parity with stale-sentinel-canary: same tracked slot set, same
     consumption-aware map, same is_set semantics (the canary derives from the
     registry; these tests fail if either side re-hardcodes).
  4. Fail-open: missing WM file still exits 0 with structured JSON.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import yaml

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
BATTERY = SCRIPTS_DIR / "precheck-sentinel-battery.py"

sys.path.insert(0, str(SCRIPTS_DIR))

import _sentinel_registry as registry  # noqa: E402


def _load_canary():
    spec = importlib.util.spec_from_file_location(
        "stale_sentinel_canary", SCRIPTS_DIR / "stale-sentinel-canary.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_battery(wm_path: Path) -> dict:
    result = subprocess.run(
        [sys.executable, str(BATTERY), "--wm-path", str(wm_path), "--json"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_set_sentinel_listed_null_omitted(tmp_path):
    wm = {
        "slots": {
            "force_tree_maintain": {
                "triggered_at": "2026-07-16T00:00:00",
                "source": "encoding-drift",
                "threshold": 3,
            },
            "force_experience_archival": None,
            "fresh_eyes_dispatch_pending": "null",
            "pipeline_reconcile_pending": {
                "fired": True,
                "skill": "/domain-reconcile",
                "goals": ["g-1"],
            },
        }
    }
    wm_path = tmp_path / "working-memory.yaml"
    wm_path.write_text(yaml.safe_dump(wm), encoding="utf-8")

    report = _run_battery(wm_path)
    assert report["registered"] == len(registry.battery_slots())
    listed = {e["slot"] for e in report["set"]}
    assert listed == {"force_tree_maintain", "pipeline_reconcile_pending"}
    by_slot = {e["slot"]: e for e in report["set"]}
    assert by_slot["force_tree_maintain"]["phase"] == "0-pre"
    assert by_slot["force_tree_maintain"]["payload"]["source"] == "encoding-drift"
    assert "0-pre5" in by_slot["pipeline_reconcile_pending"]["phase"]
    assert all(e["dispatch"] for e in report["set"])


def test_fired_false_dict_not_listed(tmp_path):
    wm = {"slots": {"fresh_eyes_dispatch_pending": {"fired": False, "core_count": 0}}}
    wm_path = tmp_path / "working-memory.yaml"
    wm_path.write_text(yaml.safe_dump(wm), encoding="utf-8")

    report = _run_battery(wm_path)
    assert report["set"] == []


def test_all_null_reports_zero(tmp_path):
    wm = {"slots": {s["slot"]: None for s in registry.battery_slots()}}
    wm_path = tmp_path / "working-memory.yaml"
    wm_path.write_text(yaml.safe_dump(wm), encoding="utf-8")

    report = _run_battery(wm_path)
    assert report["set"] == []
    assert "error" not in report


def test_missing_wm_file_fails_open(tmp_path):
    report = _run_battery(tmp_path / "nope.yaml")
    assert report["error"] == "no_working_memory_file"
    assert report["set"] == []


def test_registry_parity_with_canary():
    canary = _load_canary()
    assert canary.TRACKED_SENTINELS == registry.canary_tracked_slots()
    assert canary.CONSUMPTION_AWARE == registry.consumption_aware_map()
    # The historical canary contract, pinned explicitly so a registry edit
    # that silently drops a tracked slot fails here (not just in behavior).
    assert set(canary.TRACKED_SENTINELS) == {
        "force_tree_encoding",
        "force_tree_maintain",
        "fresh_eyes_dispatch_pending",
        "force_metric_encoding_pending",
    }
    assert canary.CONSUMPTION_AWARE == {
        "fresh_eyes_dispatch_pending": "fresh_eyes_last_dispatch",
        "force_tree_maintain": "force_tree_maintain_last_dispatch",
        "force_metric_encoding_pending": "force_metric_encoding_last_dispatch",
    }


def test_is_set_semantics_shared():
    canary = _load_canary()
    cases = [
        (None, False),
        (False, False),
        ("null", False),
        ("", False),
        ("false", False),
        ("2026-07-16T00:00:00", True),
        ({"fired": False, "x": 1}, False),
        ({"fired": True}, True),
        ({"any": "keys"}, True),
        ({}, False),
        ([], False),
        ([1], True),
        (0, True),  # numeric zero is a meaningful value (defensive default)
    ]
    for value, expected in cases:
        assert registry.is_set(value) is expected, value
        assert canary._is_set(value) is expected, value


def test_battery_covers_all_precheck_phases():
    phases = {s["phase"] for s in registry.battery_slots()}
    assert phases == {"0-pre", "0-pre2", "0-pre3", "0-pre4", "0-pre5", "0-pre6"}
