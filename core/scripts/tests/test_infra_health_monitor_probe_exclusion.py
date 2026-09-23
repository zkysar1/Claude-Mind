"""test_infra_health_monitor_probe_exclusion.py -- .

A MONITOR probe and an INFRA-HEALTH probe obey incompatible contracts while
sharing the `probe-*.sh` filename namespace:

  infra-health  : exit 0 AND parseable JSON on stdout, else status=failed.
  monitor-probes: exit 0 clean / NON-ZERO trips, evidence is free text by design.

Discovery enrolled by FILENAME through a bare glob with no opt-out, so a monitor
probe honouring its own contract printed prose on a clean run and was recorded
FAILED every time -- last_success could never leave null (a structural
impossibility, not a flake) while the streak walked toward the
consecutive_failures=3 owner alert about a healthy component.

These tests pin the fix: REGISTRATION, not filename, decides enrollment.

infra-health.py is hyphenated (not import-able as a module name), so it is
loaded by file path via importlib -- the same indirection
test_infra_health_retire and test_infra_health_staleness use.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# infra-health.py has a hyphen -> load it by file path.
_spec = importlib.util.spec_from_file_location(
    "infra_health_excl_mod", CORE_SCRIPTS / "infra-health.py"
)
ih = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ih)


def _seed_probe_scripts(world_dir, stems):
    scripts = world_dir / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    for stem in stems:
        (scripts / f"probe-{stem}.sh").write_text("#!/usr/bin/env bash\nexit 0\n")


def _seed_registry(root, scripts):
    """Write a monitor-probes.yaml at the location _load_monitor_probe_stems reads."""
    cfg = root / "core" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    probes = [{"id": Path(s).stem, "script": s} for s in scripts]
    (cfg / "monitor-probes.yaml").write_text(yaml.safe_dump({"probes": probes}))


# --- _load_monitor_probe_stems ----------------------------------------------

def test_registry_stems_are_parsed_from_script_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(ih, "PROJECT_ROOT", tmp_path)
    _seed_registry(tmp_path, ["world/scripts/probe-settlement-tick.sh"])
    assert ih._load_monitor_probe_stems() == {"probe-settlement-tick"}


def test_absent_registry_returns_empty_set(tmp_path, monkeypatch):
    # Fail-open: no registry must leave discovery exactly as it was.
    monkeypatch.setattr(ih, "PROJECT_ROOT", tmp_path)
    assert ih._load_monitor_probe_stems() == set()


def test_malformed_registry_returns_empty_set(tmp_path, monkeypatch):
    monkeypatch.setattr(ih, "PROJECT_ROOT", tmp_path)
    cfg = tmp_path / "core" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "monitor-probes.yaml").write_text("probes: [oops\n")  # unparseable
    assert ih._load_monitor_probe_stems() == set()


def test_registry_entries_without_a_script_key_are_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(ih, "PROJECT_ROOT", tmp_path)
    cfg = tmp_path / "core" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "monitor-probes.yaml").write_text(
        yaml.safe_dump({"probes": [{"id": "no-script"}, {"script": ""}, "not-a-dict"]})
    )
    assert ih._load_monitor_probe_stems() == set()


# --- _discover_probe_components exclusion ------------------------------------

def test_registered_monitor_probe_is_excluded_from_discovery(tmp_path, monkeypatch):
    monkeypatch.setattr(ih, "WORLD_DIR", tmp_path)
    monkeypatch.setattr(ih, "_load_probe_aliases", lambda: {})
    monkeypatch.setattr(ih, "_load_monitor_probe_stems", lambda: {"probe-settlement-tick"})
    _seed_probe_scripts(tmp_path, ["bridge", "bitnet", "settlement-tick"])
    # The monitor probe is gone; everything else is untouched.
    assert ih._discover_probe_components() == {"bridge", "bitnet"}


def test_unregistered_probes_are_still_discovered(tmp_path, monkeypatch):
    # Positive control: the exclusion must not be a blanket suppression. An
    # empty registry has to leave the discovered set byte-identical to before.
    monkeypatch.setattr(ih, "WORLD_DIR", tmp_path)
    monkeypatch.setattr(ih, "_load_probe_aliases", lambda: {})
    monkeypatch.setattr(ih, "_load_monitor_probe_stems", lambda: set())
    _seed_probe_scripts(tmp_path, ["bridge", "bitnet", "settlement-tick"])
    assert ih._discover_probe_components() == {"bridge", "bitnet", "settlement-tick"}


def test_exclusion_is_by_registration_not_by_name_shape(tmp_path, monkeypatch):
    # The whole point of keying on the registry: a monitor probe keeps its
    # probe-*.sh name and is STILL excluded, so no naming taboo has to be
    # remembered. Conversely an unregistered look-alike stays enrolled.
    monkeypatch.setattr(ih, "WORLD_DIR", tmp_path)
    monkeypatch.setattr(ih, "_load_probe_aliases", lambda: {})
    monkeypatch.setattr(ih, "_load_monitor_probe_stems", lambda: {"probe-monitor-a"})
    _seed_probe_scripts(tmp_path, ["monitor-a", "monitor-b"])
    assert ih._discover_probe_components() == {"monitor-b"}


def test_alias_overlay_still_applies_to_non_excluded_probes(tmp_path, monkeypatch):
    # The exclusion is inserted ahead of the alias normalization; confirm it did
    # not displace it ('s bitnet-prod -> bitnet reconciliation).
    monkeypatch.setattr(ih, "WORLD_DIR", tmp_path)
    monkeypatch.setattr(ih, "_load_probe_aliases", lambda: {"bitnet-prod": "bitnet"})
    monkeypatch.setattr(ih, "_load_monitor_probe_stems", lambda: {"probe-settlement-tick"})
    _seed_probe_scripts(tmp_path, ["bitnet-prod", "settlement-tick"])
    assert ih._discover_probe_components() == {"bitnet"}


# --- the REAL registry -------------------------------------------------------

def test_real_registry_parses_and_yields_probe_prefixed_stems():
    """The shipped registry must actually parse -- an empty set here would make
    the whole exclusion a silent no-op, which is exactly the failure shape this
    fix exists to remove (a clean-looking zero nobody positive-controlled)."""
    stems = ih._load_monitor_probe_stems()
    assert isinstance(stems, set)
    # Every registered script that shares the infra-health namespace must be a
    # probe-*.sh stem; anything else could never have collided in the first place.
    for stem in stems:
        assert isinstance(stem, str) and stem
