"""test_infra_health_retire.py -- .

Covers the two new infra-health capabilities:
  1. _discover_probe_components probe-stem aliasing -- a stem that maps to a
     canonical component via the world overlay is normalized, so no stale
     SHADOW component is derived (the bitnet-prod -> bitnet reconciliation).
  2. cmd_retire: the archive-first, locked component-removal primitive
     (.claude/rules/archive-before-delete.md) -- archives the entry to
     RETIRED_ARCHIVE_FILE, verifies the receipt, THEN removes the component;
     idempotent no-op on an absent component; warns when discovery would
     re-derive (rb-506); plus the _archive_has_receipt read-back verifier.

infra-health.py is hyphenated (not import-able as a module name), so it is
loaded by file path via importlib -- same indirection test_infra_health_staleness
uses.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# infra-health.py has a hyphen -> load it by file path.
_spec = importlib.util.spec_from_file_location(
    "infra_health_mod", CORE_SCRIPTS / "infra-health.py"
)
ih = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ih)


# --- probe-stem aliasing (_discover_probe_components) ------------------------

def _seed_probe_scripts(world_dir, stems):
    scripts = world_dir / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    for stem in stems:
        (scripts / f"probe-{stem}.sh").write_text("#!/usr/bin/env bash\nexit 0\n")


def test_discover_without_alias_uses_raw_stems(tmp_path, monkeypatch):
    monkeypatch.setattr(ih, "WORLD_DIR", tmp_path)
    monkeypatch.setattr(ih, "_load_probe_aliases", lambda: {})
    _seed_probe_scripts(tmp_path, ["bridge", "bitnet", "bitnet-prod"])
    assert ih._discover_probe_components() == {"bridge", "bitnet", "bitnet-prod"}


def test_discover_alias_normalizes_stem_to_canonical(tmp_path, monkeypatch):
    # bitnet-prod -> bitnet: the aliased stem dedupes into the canonical name,
    # so no bitnet-prod shadow is discovered ().
    monkeypatch.setattr(ih, "WORLD_DIR", tmp_path)
    monkeypatch.setattr(ih, "_load_probe_aliases", lambda: {"bitnet-prod": "bitnet"})
    _seed_probe_scripts(tmp_path, ["bridge", "bitnet", "bitnet-prod"])
    discovered = ih._discover_probe_components()
    assert "bitnet-prod" not in discovered
    assert discovered == {"bridge", "bitnet"}


def test_discover_alias_when_only_prod_script_exists(tmp_path, monkeypatch):
    # Even if only probe-bitnet-prod.sh exists, the alias yields 'bitnet'.
    monkeypatch.setattr(ih, "WORLD_DIR", tmp_path)
    monkeypatch.setattr(ih, "_load_probe_aliases", lambda: {"bitnet-prod": "bitnet"})
    _seed_probe_scripts(tmp_path, ["bitnet-prod"])
    assert ih._discover_probe_components() == {"bitnet"}


def test_load_probe_aliases_empty_by_default_is_dict(monkeypatch):
    # Missing/empty overlay -> empty dict (discovery unchanged). Fail-safe shape.
    monkeypatch.setattr(ih, "_load_world_config", lambda name, default=None: default or {})
    assert ih._load_probe_aliases() == {}


# --- cmd_retire (archive-first component removal) ---------------------------

def _seed_health(health_file, components):
    health_file.write_text(yaml.safe_dump({"components": components}))


def _read_health(health_file):
    return (yaml.safe_load(health_file.read_text()) or {}).get("components", {})


def _retire_args(component, reason=None):
    return SimpleNamespace(component=component, reason=reason)


def test_retire_archives_then_removes(tmp_path, monkeypatch, capsys):
    health = tmp_path / "infra-health.yaml"
    archive = tmp_path / "infra-health-retired.jsonl"
    monkeypatch.setattr(ih, "HEALTH_FILE", health)
    monkeypatch.setattr(ih, "RETIRED_ARCHIVE_FILE", archive)
    monkeypatch.setattr(ih, "WORLD_DIR", tmp_path)  # no scripts/ -> no re-derive warning
    monkeypatch.setenv("MIND_AGENT", "tester")
    entry = {"last_success": None, "consecutive_failures": 0}
    _seed_health(health, {"llm-bitnet-prod": entry,
                          "bitnet": {"last_success": "2026-07-23T11:15:55"}})

    ih.cmd_retire(_retire_args("llm-bitnet-prod", reason="orphan cruft"))
    out = json.loads(capsys.readouterr().out.strip())

    assert out["retired"] is True
    assert out["component"] == "llm-bitnet-prod"
    assert "warning" not in out  # no probe script -> not re-derived
    # component removed, sibling untouched
    comps = _read_health(health)
    assert "llm-bitnet-prod" not in comps
    assert "bitnet" in comps
    # archive receipt present + complete (the recovery record)
    lines = [json.loads(l) for l in archive.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    r = lines[0]
    assert r["component"] == "llm-bitnet-prod"
    assert r["entry"] == entry
    assert r["reason"] == "orphan cruft"
    assert r["retired_by"] == "tester"
    assert r["retired_at"]


def test_retire_absent_component_is_noop(tmp_path, monkeypatch, capsys):
    health = tmp_path / "infra-health.yaml"
    archive = tmp_path / "infra-health-retired.jsonl"
    monkeypatch.setattr(ih, "HEALTH_FILE", health)
    monkeypatch.setattr(ih, "RETIRED_ARCHIVE_FILE", archive)
    _seed_health(health, {"bitnet": {"last_success": "2026-07-23T11:15:55"}})

    ih.cmd_retire(_retire_args("nonexistent-xyz"))
    out = json.loads(capsys.readouterr().out.strip())

    assert out["retired"] is False
    assert out["reason"] == "absent"
    # nothing archived, health untouched (archive-before-delete: no delete, no archive)
    assert (not archive.exists()) or archive.read_text().strip() == ""
    assert set(_read_health(health)) == {"bitnet"}


def test_retire_warns_when_discovery_re_derives(tmp_path, monkeypatch, capsys):
    # A component still backed by a live probe-*.sh (no alias) -> retire warns
    # check-all discovery will re-add it (rb-506).
    health = tmp_path / "infra-health.yaml"
    archive = tmp_path / "infra-health-retired.jsonl"
    monkeypatch.setattr(ih, "HEALTH_FILE", health)
    monkeypatch.setattr(ih, "RETIRED_ARCHIVE_FILE", archive)
    monkeypatch.setattr(ih, "WORLD_DIR", tmp_path)
    monkeypatch.setattr(ih, "_load_probe_aliases", lambda: {})
    _seed_probe_scripts(tmp_path, ["bridge"])
    _seed_health(health, {"bridge": {"last_success": None}})

    ih.cmd_retire(_retire_args("bridge"))
    out = json.loads(capsys.readouterr().out.strip())
    assert out["retired"] is True
    assert "warning" in out and "bridge" in out["warning"]


# --- _archive_has_receipt (read-back verifier) ------------------------------

def test_archive_has_receipt_true_and_false(tmp_path, monkeypatch):
    archive = tmp_path / "infra-health-retired.jsonl"
    monkeypatch.setattr(ih, "RETIRED_ARCHIVE_FILE", archive)
    archive.write_text(
        json.dumps({"component": "foo", "retired_at": "2026-07-24T00:00:00"}) + "\n"
    )
    assert ih._archive_has_receipt("foo", "2026-07-24T00:00:00") is True
    assert ih._archive_has_receipt("foo", "2026-07-24T00:00:01") is False   # wrong ts
    assert ih._archive_has_receipt("bar", "2026-07-24T00:00:00") is False   # wrong comp


def test_archive_has_receipt_false_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(ih, "RETIRED_ARCHIVE_FILE", tmp_path / "does-not-exist.jsonl")
    assert ih._archive_has_receipt("foo", "2026-07-24T00:00:00") is False
