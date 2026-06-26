"""Unit tests for the precheck retrospection-budget reservation ().

Synthetic tests for the decision unit that aspirations-precheck-budget-meter.sh
imports: in the tight zone a STALE retrospection-class sweep must WIN (run)
over the zone-drop, while a non-retrospection deferrable still drops and a
non-stale retrospection sweep still drops. (bravo session-66 #4.)
"""
import importlib.util
from pathlib import Path

import pytest


def _load():
    p = Path(__file__).resolve().parents[1] / "_precheck_budget_reserve.py"
    spec = importlib.util.spec_from_file_location("_precheck_budget_reserve", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


M = _load()


def test_stale_retrospection_reserved_in_tight():
    # tight-zone base decision = drop; retrospection sweep; staleness 8 >= 8
    d, r, last = M.reserve_decision(
        "drop", "zone-drop:tight", "pending-questions-sweep",
        cur_iter=10, last_retro_iter=2)
    assert d == "run", "stale retrospection must win over the tight-zone drop"
    assert r.startswith("retrospection-reserved"), r
    assert last == 10, "running a retrospection sweep stamps last_retro=cur_iter"


def test_fresh_retrospection_still_drops_in_tight():
    # retrospection sweep but NOT stale (staleness 1 < 8) -> stays dropped
    d, r, last = M.reserve_decision(
        "drop", "zone-drop:tight", "felt-sense-cadence",
        cur_iter=10, last_retro_iter=9)
    assert d == "drop"
    assert last == 9, "a dropped sweep does not advance last_retro"


def test_non_retrospection_deferrable_unchanged():
    # a non-retrospection deferrable drops regardless of staleness
    d, r, last = M.reserve_decision(
        "drop", "zone-drop:tight", "recurring-precondition-sweep",
        cur_iter=100, last_retro_iter=0)
    assert d == "drop", "only retrospection-class sweeps are reserved"
    assert last == 0


def test_running_retrospection_stamps_last():
    # retrospection sweep already running (e.g. fresh zone) -> stamp last_retro
    d, r, last = M.reserve_decision(
        "run", "within-budget", "parent-supersession-sweep",
        cur_iter=5, last_retro_iter=0)
    assert d == "run"
    assert last == 5


def test_running_non_retrospection_does_not_stamp():
    # an always-run non-retrospection sweep running must NOT stamp last_retro
    d, r, last = M.reserve_decision(
        "run", "always-run-tier", "tree-debt-gate",
        cur_iter=5, last_retro_iter=0)
    assert d == "run"
    assert last == 0


def test_exactly_at_threshold_reserves():
    # boundary: staleness == threshold -> reserve
    d, r, last = M.reserve_decision(
        "drop", "zone-drop:tight", "pending-questions-sweep",
        cur_iter=8, last_retro_iter=0, threshold=8)
    assert d == "run"


def test_below_threshold_does_not_reserve():
    # boundary: staleness == threshold-1 -> NOT reserved
    d, r, last = M.reserve_decision(
        "drop", "zone-drop:tight", "pending-questions-sweep",
        cur_iter=7, last_retro_iter=0, threshold=8)
    assert d == "drop"


@pytest.mark.parametrize("sweep", sorted(M.RETROSPECTION_SWEEPS))
def test_all_named_retrospection_classes_recognized(sweep):
    # every retrospection class named in  is reservable when stale
    d, r, _ = M.reserve_decision(
        "drop", "zone-drop:tight", sweep, cur_iter=20, last_retro_iter=0)
    assert d == "run", f"{sweep} should be reservable when stale"
    assert M.is_retrospection(sweep)
