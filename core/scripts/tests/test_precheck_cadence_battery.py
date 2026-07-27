"""Tests for precheck-cadence-battery.py + _cadence_registry.py (,
fix for g-115-2982).

Covers: registry shape + on-disk script existence, all-noop summary, single/
multiple FIRE dispatch, per-check error fail-open (rc=0 always), and JSON emit
shape. The engine's injectable `check_runner` keeps every case hermetic — no
subprocess, no filesystem writes, no world/S3 access.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import _cadence_registry as reg  # underscore module — direct import

spec = importlib.util.spec_from_file_location(
    "precheck_cadence_battery", SCRIPTS / "precheck-cadence-battery.py"
)
battery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(battery)


# ---------------------------------------------------------------- registry ----

def test_registry_has_six_skill_invocation_cadences():
    cads = reg.cadences()
    assert len(cads) == 6
    assert reg.cadence_names() == [
        "fresh-eyes-review", "fresh-eyes-program", "fresh-eyes-tree",
        "felt-sense", "curriculum", "evolution",
    ]
    assert [c["phase"] for c in cads] == [
        "0.5e", "0.5e.5", "0.5e.7", "0.5f", "0.5i", "0.5j",
    ]


def test_registry_excludes_self_acting_and_dormant_cadences():
    # l1-skew (0.5g, self-acting) and health-regression (0.5h, dormant) are
    # deliberately OUT — principled scope, not a silent cap.
    phases = {c["phase"] for c in reg.cadences()}
    assert "0.5g" not in phases  # l1-skew
    assert "0.5h" not in phases  # health-regression


def test_registry_entries_are_well_formed():
    for c in reg.cadences():
        assert c["check_cmd"] and isinstance(c["check_cmd"], list)
        assert c["check_cmd"][0].endswith(".sh")
        assert c["meter_name"] and isinstance(c["meter_name"], str)
        assert c["fire_dispatch"] and isinstance(c["fire_dispatch"], str)


def test_registry_check_scripts_exist_on_disk():
    for c in reg.cadences():
        script = SCRIPTS / c["check_cmd"][0]
        assert script.exists(), f"missing cadence-check script: {script}"


# ---------------------------------------------------- engine (injected) ------

def _runner_from(fire_names=(), errors=()):
    """Build a fake (argv, timeout)->(rc|None, err|None) keyed on cadence name.

    Matches each argv back to its registry entry by check_cmd equality.
    """
    by_cmd = {tuple(c["check_cmd"]): c["name"] for c in reg.cadences()}

    def runner(argv, timeout):
        name = by_cmd.get(tuple(argv))
        if name in errors:
            return None, f"{argv[0]}: boom"
        if name in fire_names:
            return 0, None
        return 1, None

    return runner


def _run_json(capsys, runner):
    rc = battery.run(True, check_runner=runner)
    out = capsys.readouterr().out
    assert rc == 0
    return json.loads(out.splitlines()[0])


def test_all_noop(capsys):
    rep = _run_json(capsys, _runner_from())
    assert rep["registered"] == 6
    assert rep["fired"] == []
    assert "error" not in rep


def test_all_noop_default_summary(capsys):
    rc = battery.run(False, check_runner=_runner_from())
    out = capsys.readouterr().out
    assert rc == 0
    assert "all 6 cadence gates noop" in out


def test_single_fire_carries_dispatch_and_meter(capsys):
    rep = _run_json(capsys, _runner_from(fire_names={"felt-sense"}))
    assert len(rep["fired"]) == 1
    fired = rep["fired"][0]
    assert fired["name"] == "felt-sense"
    assert fired["phase"] == "0.5f"
    assert fired["meter_name"] == "felt-sense-cadence"
    assert "felt-sense-checkin" in fired["dispatch"]


def test_single_fire_human_output(capsys):
    battery.run(False, check_runner=_runner_from(fire_names={"evolution"}))
    out = capsys.readouterr().out
    assert "CADENCE FIRE: evolution (phase 0.5j)" in out
    assert "aspirations-evolve" in out
    assert "1 fire / 6 checked" in out


def test_multiple_fire(capsys):
    rep = _run_json(
        capsys, _runner_from(fire_names={"fresh-eyes-review", "curriculum"})
    )
    names = {f["name"] for f in rep["fired"]}
    assert names == {"fresh-eyes-review", "curriculum"}


def test_check_error_is_fail_open_and_surfaced(capsys):
    # felt-sense check errors; evolution still fires; rc stays 0.
    rep = _run_json(
        capsys, _runner_from(fire_names={"evolution"}, errors={"felt-sense"})
    )
    assert [f["name"] for f in rep["fired"]] == ["evolution"]  # error one skipped
    assert "check_errors" in rep["error"]
    assert "felt-sense-cadence-check.sh" in rep["error"]


def test_check_error_prints_to_stderr(capsys):
    # guard-424: fail LOUD with stderr, never silent.
    battery.run(True, check_runner=_runner_from(errors={"curriculum"}))
    err = capsys.readouterr().err
    assert "[cadence-battery]" in err
    assert "check_errors" in err


def test_json_shape(capsys):
    rep = _run_json(capsys, _runner_from(fire_names={"felt-sense"}))
    assert set(rep.keys()) >= {"checked_at", "registered", "fired"}
    for f in rep["fired"]:
        assert set(f.keys()) == {"name", "phase", "meter_name", "dispatch"}
