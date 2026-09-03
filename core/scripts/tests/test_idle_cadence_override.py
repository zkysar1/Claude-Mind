"""The idle cadence is deployment-tunable through meta/config-overrides.yaml ().

User directive 2026-09-03: a single-agent deployment sleeps in flat 2-hour blocks
and never dies. _dry_idle.load_config() and quiescence-gate's _load_config()
used to read core/config/aspirations.yaml directly, so an override such as
`aspirations.dry_idle_backoff.base_seconds: 7200` was silently ignored.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

BASE_YAML = """\
quiescence_gate:
  sleep_seconds_min: 1800
  sleep_seconds_max: 3600
dry_idle_backoff:
  enabled: true
  base_seconds: 120
  multiplier: 2.0
  max_seconds: 7200
  budget_pct: 0.90
  reset_on_executable: true
  stop_after_cap_cycles: null
"""

OVERRIDES_YAML = """\
overrides:
  aspirations.dry_idle_backoff.base_seconds:
    value: 7200
    previous: 120
    changed_date: "2026-09-03"
    reason: "user directive: flat 2-hour idle blocks"
  aspirations.quiescence_gate.sleep_seconds_max: 7200
"""


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    os.environ.setdefault("MIND_AGENT", "alpha")
    cfg_dir = tmp_path / "config"
    meta_dir = tmp_path / "meta"
    cfg_dir.mkdir()
    meta_dir.mkdir()
    (cfg_dir / "aspirations.yaml").write_text(BASE_YAML, encoding="utf-8")
    (meta_dir / "config-overrides.yaml").write_text(OVERRIDES_YAML, encoding="utf-8")
    ov = importlib.import_module("_config_overlay")
    monkeypatch.setattr(ov, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(ov, "META_DIR", meta_dir)
    return cfg_dir, meta_dir


def test_dry_idle_live_read_honors_the_override(overlay):
    dry = importlib.import_module("_dry_idle")
    cfg = dry.load_config()
    assert cfg["base_seconds"] == 7200          # overridden
    assert cfg["max_seconds"] == 7200 and cfg["stop_after_cap_cycles"] is None  # never-die default intact
    assert dry.sleep_seconds_for(1, cfg) == 7200 if hasattr(dry, "sleep_seconds_for") else True


def test_dry_idle_explicit_path_reads_raw(overlay):
    cfg_dir, _ = overlay
    dry = importlib.import_module("_dry_idle")
    cfg = dry.load_config(config_path=cfg_dir / "aspirations.yaml")
    assert cfg["base_seconds"] == 120           # tests pass an explicit path: raw framework values


def test_dry_idle_falls_back_when_no_override_file(overlay):
    _, meta_dir = overlay
    (meta_dir / "config-overrides.yaml").unlink()
    dry = importlib.import_module("_dry_idle")
    assert dry.load_config()["base_seconds"] == 120


def test_quiescence_gate_loader_honors_the_override(overlay):
    spec = importlib.util.spec_from_file_location("quiescence_gate_mod", CORE_SCRIPTS / "quiescence-gate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    qg = mod._load_config()
    assert qg["sleep_seconds_max"] == 7200 and qg["sleep_seconds_min"] == 1800
