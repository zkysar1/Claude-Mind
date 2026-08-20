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

def test_registry_pins_the_skill_invocation_cadence_roster():
    # The roster is pinned EXPLICITLY on purpose — this is the SSOT, so an
    # accidental add/remove/reorder must redden here. The engine tests below
    # deliberately do the opposite and derive their counts from the registry;
    # only this test asserts membership. (Renamed from ...has_six... in
    # : a count in the test NAME goes stale the same way a count in
    # an assertion does, and a stale name is worse — it survives the fix.)
    cads = reg.cadences()
    assert reg.cadence_names() == [
        "fresh-eyes-review", "fresh-eyes-program", "fresh-eyes-tree",
        "strategic-scan", "felt-sense", "curriculum", "evolution",
    ]
    assert [c["phase"] for c in cads] == [
        "0.5e", "0.5e.5", "0.5e.7", "0.5e.9", "0.5f", "0.5i", "0.5j",
    ]
    assert len(cads) == len(reg.cadence_names())


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


class _MemCounterStore:
    """In-memory injectable counter store () — keeps the engine tests
    hermetic (no filesystem write, no MIND_AGENT dependence). `data` is
    inspectable after a run to assert increment/reset behavior. `modify` mirrors
    the file store: mutate a copy, then persist it (the file store holds the lock
    across the same read-mutate-write)."""

    def __init__(self, data=None):
        self.data = dict(data or {})

    def modify(self, mutate_fn):
        counters = dict(self.data)
        mutate_fn(counters)
        self.data = dict(counters)
        return counters


def _run_json(capsys, runner, store=None):
    rc = battery.run(
        True, check_runner=runner,
        counter_store=store if store is not None else _MemCounterStore(),
    )
    out = capsys.readouterr().out
    assert rc == 0
    return json.loads(out.splitlines()[0])


def test_all_noop(capsys):
    rep = _run_json(capsys, _runner_from())
    # Derived from the registry, never a literal: these assert the ENGINE
    # reports what it enumerated, which stays true at any roster size. A
    # hardcoded count here reddens on every legitimate registry growth and
    # teaches the reader to bump the number rather than check the behaviour.
    assert rep["registered"] == len(reg.cadences())
    assert rep["fired"] == []
    assert "error" not in rep


def test_all_noop_default_summary(capsys):
    rc = battery.run(False, check_runner=_runner_from(),
                     counter_store=_MemCounterStore())
    out = capsys.readouterr().out
    assert rc == 0
    assert f"all {len(reg.cadences())} cadence gates noop" in out


def test_single_fire_carries_dispatch_and_meter(capsys):
    rep = _run_json(capsys, _runner_from(fire_names={"felt-sense"}))
    assert len(rep["fired"]) == 1
    fired = rep["fired"][0]
    assert fired["name"] == "felt-sense"
    assert fired["phase"] == "0.5f"
    assert fired["meter_name"] == "felt-sense-cadence"
    assert "felt-sense-checkin" in fired["dispatch"]


def test_single_fire_human_output(capsys):
    battery.run(False, check_runner=_runner_from(fire_names={"evolution"}),
                counter_store=_MemCounterStore())
    out = capsys.readouterr().out
    assert "CADENCE FIRE: evolution (phase 0.5j)" in out
    assert "aspirations-evolve" in out
    assert f"1 fire / {len(reg.cadences())} checked" in out


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
    battery.run(True, check_runner=_runner_from(errors={"curriculum"}),
                counter_store=_MemCounterStore())
    err = capsys.readouterr().err
    assert "[cadence-battery]" in err
    assert "check_errors" in err


def test_json_shape(capsys):
    rep = _run_json(capsys, _runner_from(fire_names={"felt-sense"}))
    assert set(rep.keys()) >= {"checked_at", "registered", "fired", "escalation"}
    for f in rep["fired"]:
        assert set(f.keys()) == {"name", "phase", "meter_name", "dispatch"}


# ------------------------------------------- starvation escalation ()
# The battery is now STATEFUL: a per-cadence consecutive-FIRE counter escalates
# SUSTAINED starvation (due-and-skipped N prechecks running), distinct from the
# momentary co-firing of 1-3 cadences in one iteration. Every case injects an
# in-memory _MemCounterStore, so the suite never touches disk.


def test_counter_increments_on_consecutive_fire(capsys):
    # A cadence that FIREs every run (due, never dispatched) accumulates count.
    store = _MemCounterStore()
    for _ in range(3):
        _run_json(capsys, _runner_from(fire_names={"felt-sense"}), store)
    assert store.data["felt-sense"] == 3


def test_counter_resets_to_zero_on_noop(capsys):
    # A noop (rc!=0 = dispatched OR not-due) resets the counter — no longer starved.
    store = _MemCounterStore({"felt-sense": 4})
    _run_json(capsys, _runner_from(fire_names=set()), store)  # felt-sense noops
    assert store.data.get("felt-sense", 0) == 0


def test_no_escalation_below_threshold(capsys):
    # Default threshold 5 — four consecutive fires must NOT escalate.
    store = _MemCounterStore()
    rep = None
    for _ in range(4):
        rep = _run_json(capsys, _runner_from(fire_names={"felt-sense"}), store)
    assert rep["escalation"] is None
    assert store.data["felt-sense"] == 4


def test_escalation_fires_at_threshold(capsys):
    # The fifth consecutive fire crosses the default threshold and escalates.
    store = _MemCounterStore()
    rep = None
    for _ in range(5):
        rep = _run_json(capsys, _runner_from(fire_names={"felt-sense"}), store)
    assert rep["escalation"] is not None
    assert rep["escalation"]["threshold"] == 5
    assert rep["escalation"]["dispatch_one"]["name"] == "felt-sense"
    assert rep["escalation"]["dispatch_one"]["phase"] == "0.5f"


def test_escalation_respects_injected_threshold(capsys):
    # threshold= override: two fires escalate at threshold 2.
    store = _MemCounterStore()
    rep = None
    for _ in range(2):
        battery.run(True, check_runner=_runner_from(fire_names={"evolution"}),
                    counter_store=store, threshold=2)
        rep = json.loads(capsys.readouterr().out.splitlines()[0])
    assert rep["escalation"] is not None
    assert rep["escalation"]["dispatch_one"]["name"] == "evolution"


def test_escalation_picks_highest_count_oldest_starved(capsys):
    # Two starved cadences: fresh-eyes-review at 7 beats felt-sense at 6.
    store = _MemCounterStore({"fresh-eyes-review": 6, "felt-sense": 5})
    rep = _run_json(
        capsys,
        _runner_from(fire_names={"fresh-eyes-review", "felt-sense"}),
        store,
    )  # -> 7 and 6
    assert rep["escalation"]["dispatch_one"]["name"] == "fresh-eyes-review"
    starved = {s["name"]: s["count"] for s in rep["escalation"]["starved"]}
    assert starved == {"fresh-eyes-review": 7, "felt-sense": 6}


def test_escalation_tiebreak_prefers_felt_sense(capsys):
    # Equal counts -> felt-sense wins the tie (its starvation is the origin class).
    store = _MemCounterStore({"fresh-eyes-review": 5, "felt-sense": 5})
    rep = _run_json(
        capsys,
        _runner_from(fire_names={"fresh-eyes-review", "felt-sense"}),
        store,
    )  # -> both 6, tie
    assert rep["escalation"]["dispatch_one"]["name"] == "felt-sense"


def test_escalation_human_output_is_loud(capsys):
    # The default (human) emit prints a LOUD, actionable line naming exactly one.
    store = _MemCounterStore({"felt-sense": 4})
    battery.run(False, check_runner=_runner_from(fire_names={"felt-sense"}),
                counter_store=store)  # -> 5, escalates
    out = capsys.readouterr().out
    assert "CADENCE STARVATION" in out
    assert "felt-sense" in out
    assert "EXACTLY" in out and "ONE" in out


def test_dispatched_cadence_stops_escalating(capsys):
    # felt-sense starved to 5, then IS dispatched (noops) -> reset, no escalation.
    store = _MemCounterStore({"felt-sense": 5})
    rep = _run_json(capsys, _runner_from(fire_names=set()), store)  # noop -> reset
    assert store.data["felt-sense"] == 0
    assert rep["escalation"] is None


def test_stale_counter_key_is_pruned_no_phantom_escalation(capsys):
    # A counter key for a cadence NOT in the current registry (removed/renamed)
    # would otherwise keep a >= N count forever (never fires or noops => never
    # resets) and escalate a phantom. It must be PRUNED, not escalated.
    store = _MemCounterStore({"a-removed-cadence": 99, "felt-sense": 0})
    rep = _run_json(capsys, _runner_from(fire_names=set()), store)  # all noop
    assert "a-removed-cadence" not in store.data  # pruned from the persisted map
    assert rep["escalation"] is None              # no phantom escalation


# --- pure helpers (fastest, no engine) ---------------------------------------


def test_update_counters_increments_and_resets():
    counters = {"a": 3, "b": 1}
    battery._update_counters(counters, fired_names=["a"], noop_names=["b"])
    assert counters == {"a": 4, "b": 0}


def test_update_counters_leaves_errored_untouched():
    # A name in NEITHER list (errored check this run) keeps its prior count.
    counters = {"a": 3}
    battery._update_counters(counters, fired_names=[], noop_names=[])
    assert counters == {"a": 3}


def test_pick_oldest_starved_none_below_threshold():
    assert battery._pick_oldest_starved({"a": 4, "b": 2}, 5, ["a", "b"]) is None


def test_pick_oldest_starved_highest_then_felt_sense_then_order():
    order = ["fresh-eyes-review", "felt-sense", "evolution"]
    # highest count wins
    assert battery._pick_oldest_starved(
        {"fresh-eyes-review": 7, "felt-sense": 6}, 5, order) == "fresh-eyes-review"
    # tie -> felt-sense
    assert battery._pick_oldest_starved(
        {"fresh-eyes-review": 6, "felt-sense": 6}, 5, order) == "felt-sense"
    # tie, no felt-sense -> registry order
    assert battery._pick_oldest_starved(
        {"evolution": 6, "fresh-eyes-review": 6}, 5, order) == "fresh-eyes-review"
