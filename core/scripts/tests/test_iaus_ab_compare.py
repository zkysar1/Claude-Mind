# domain-leak-exempt: tests for the IAUS A/B harness (g-306-33, BRD Gap 8).
# IAUS is the framework feature name; companion to iaus-ab-compare.py.
"""Unit tests for the IAUS A/B comparison harness (g-306-33).

Covers the harness's pure metric math: noise-zeroed additive base, Spearman
rho (identity/reverse anchors), the veto-demo set (infeasible goals must be
vetoed, the feasible control must not), and rank_metrics top-1 + wrongful-veto
detection. The cutover DECISION (keep flag off) is a judgment recorded in
core/config/iaus-selector-design.md section 7 — not a test assertion.
"""
import importlib.util
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import _iaus_scorer as I  # noqa: E402


def _load_ab():
    """iaus-ab-compare.py has hyphens — import via importlib by file path."""
    spec = importlib.util.spec_from_file_location(
        "iaus_ab_compare", os.path.join(_SCRIPTS, "iaus-ab-compare.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


AB = _load_ab()
_AXES = I.VETO_AXES + I.PRIMARY_AXES + I.MAKEUP_AXES
_WEIGHTS = {k: 1.0 for k in _AXES}
_CFG = {"primary_floor": 0.1, "watermark": 0.0, "bonus_scale": 4.0, "urgency_max": 4.0}


def _raw(**ov):
    d = {a: 0.0 for a in _AXES}
    d.update(ov)
    return d


def _goal(gid, breakdown, raw):
    return {"goal_id": gid, "title": gid, "recurring": False,
            "breakdown": breakdown, "raw": raw}


def test_additive_base_excludes_noise():
    g = _goal("g1", {"priority": 2.0, "role_affinity": 1.0, "exploration_noise": 5.0}, {})
    # 2.0 + 1.0; the noise term is dropped for the deterministic comparison.
    assert AB.additive_base(g) == pytest.approx(3.0)


def test_additive_base_empty_breakdown():
    assert AB.additive_base(_goal("g", {}, {})) == 0.0


def test_spearman_identity_and_reverse():
    a = {"x": 1, "y": 2, "z": 3}
    assert AB.spearman_rho(a, a) == pytest.approx(1.0)
    rev = {"x": 3, "y": 2, "z": 1}
    assert AB.spearman_rho(a, rev) == pytest.approx(-1.0)


def test_spearman_single_element_is_one():
    assert AB.spearman_rho({"x": 1}, {"x": 1}) == 1.0


def test_veto_demo_infeasible_vetoed_control_not():
    rows = AB.score_rows(AB._veto_demo_goals(), _WEIGHTS, _CFG)
    infeasible = [r for r in rows if r["agent_executable_raw"] == 0]
    feasible = [r for r in rows if r["agent_executable_raw"] == 2]
    assert infeasible and feasible          # both classes present
    assert all(r["iaus"] == 0.0 for r in infeasible)   # veto-by-zero fires
    assert all(r["iaus"] > 0.0 for r in feasible)      # control survives


def test_rank_metrics_top1_agreement_and_no_wrongful_veto():
    rows = AB.score_rows([
        _goal("g-hi", {"priority": 3.0}, _raw(agent_executable=2, priority=3)),
        _goal("g-lo", {"priority": 1.0}, _raw(agent_executable=2, priority=1)),
    ], _WEIGHTS, _CFG)
    m = AB.rank_metrics(rows, top_k=2)
    assert m["top1_additive"] == "g-hi"
    assert m["top1_iaus"] == "g-hi"            # priority 3 wins under both
    assert m["top1_agreement"] is True
    assert m["wrongly_vetoed_feasible"] == []  # both feasible, none vetoed


def test_rank_metrics_detects_correct_veto_in_topk():
    # An infeasible goal additive ranks #1 (high breakdown) but IAUS vetoes it.
    rows = AB.score_rows([
        _goal("g-infeasible", {"priority": 3.0, "completion_pressure": 9.0},
              _raw(agent_executable=0, priority=3, completion_pressure=2.5)),
        _goal("g-feasible", {"priority": 1.0}, _raw(agent_executable=2, priority=1)),
    ], _WEIGHTS, _CFG)
    m = AB.rank_metrics(rows, top_k=2)
    assert m["top1_additive"] == "g-infeasible"   # additive ranks it top
    veto_ids = [v["goal_id"] for v in m["veto_in_topk"]]
    assert "g-infeasible" in veto_ids             # IAUS vetoed it from the top-K
    assert all(v["correct_veto"] for v in m["veto_in_topk"])  # agent_executable==0
    assert m["wrongly_vetoed_feasible"] == []      # the feasible one survived
