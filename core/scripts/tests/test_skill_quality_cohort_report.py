"""test_skill_quality_cohort_report.py --  (asp-304, Layer 5).

Pins the skill-quality x usage cohort report's documented boundary rules
(verification check: "Cohort assignment matches expected boundary rules
documented in script"):

    quality_high := aggregate.overall >= quality_threshold (default 0.50)
    usage_high   := total_invocations  >= use_threshold    (default 5)

    high q + high u -> winners
    high q + low  u -> silent_gems
    low  q + high u -> problem_hotspots
    low  q + low  u -> deprecation_candidates

Plus: skill-key canonicalization, used-but-unevaluated separation, the
>= (inclusive) boundary behavior, and the report schema. Pure-Python, no
daemon, no real data files (monkeypatched data sources).

Run: py -3 -m pytest core/scripts/tests/test_skill_quality_cohort_report.py -v
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
REPORT = CORE_SCRIPTS / "skill-quality-cohort-report.py"


def _load_module():
    """Import the hyphenated report script by path for unit-level testing."""
    spec = importlib.util.spec_from_file_location("skill_quality_cohort_report", REPORT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load_module()

Q = 0.50
U = 5


def _cohorts(quality, usage, q=Q, u=U):
    return M.build_cohorts(quality, usage, q, u)


# ---- canonicalization -----------------------------------------------------

def test_canon_strips_leading_slash():
    assert M.canon("/replay") == "replay"
    assert M.canon("replay") == "replay"


def test_canon_takes_first_token():
    assert M.canon("tree maintain") == "tree"
    assert M.canon("reflect --full-cycle") == "reflect"


def test_canon_empty():
    assert M.canon("") == ""
    assert M.canon(None) is None


# ---- the four cohorts (default thresholds 0.50 / 5) -----------------------

def test_winners_high_quality_high_use():
    cohorts, _ = _cohorts({"a": 0.9}, {"a": 10})
    assert [r["skill"] for r in cohorts["winners"]] == ["a"]
    assert cohorts["silent_gems"] == []


def test_silent_gems_high_quality_low_use():
    cohorts, _ = _cohorts({"a": 0.9}, {"a": 1})
    assert [r["skill"] for r in cohorts["silent_gems"]] == ["a"]
    assert cohorts["winners"] == []


def test_silent_gems_high_quality_absent_use():
    # absent usage -> 0 invocations -> low use
    cohorts, _ = _cohorts({"a": 0.9}, {})
    assert [r["skill"] for r in cohorts["silent_gems"]] == ["a"]
    assert cohorts["silent_gems"][0]["total_invocations"] == 0


def test_problem_hotspots_low_quality_high_use():
    cohorts, _ = _cohorts({"a": 0.3}, {"a": 10})
    assert [r["skill"] for r in cohorts["problem_hotspots"]] == ["a"]


def test_deprecation_low_quality_low_use():
    cohorts, _ = _cohorts({"a": 0.2}, {"a": 0})
    assert [r["skill"] for r in cohorts["deprecation_candidates"]] == ["a"]


# ---- boundary inclusivity (>=) --------------------------------------------

def test_quality_boundary_inclusive():
    # overall == threshold counts as HIGH quality
    cohorts, _ = _cohorts({"a": 0.50}, {"a": 0})
    assert [r["skill"] for r in cohorts["silent_gems"]] == ["a"]
    # just below -> low quality
    cohorts, _ = _cohorts({"b": 0.49}, {"b": 0})
    assert [r["skill"] for r in cohorts["deprecation_candidates"]] == ["b"]


def test_usage_boundary_inclusive():
    # invocations == threshold counts as HIGH use
    cohorts, _ = _cohorts({"a": 0.9}, {"a": 5})
    assert [r["skill"] for r in cohorts["winners"]] == ["a"]
    # just below -> low use
    cohorts, _ = _cohorts({"b": 0.9}, {"b": 4})
    assert [r["skill"] for r in cohorts["silent_gems"]] == ["b"]


# ---- used-but-unevaluated -------------------------------------------------

def test_unevaluated_in_use_separated():
    # 'x' has usage but no quality eval -> unevaluated_in_use, not in any cohort
    cohorts, uneval = _cohorts({"a": 0.9}, {"a": 1, "x": 7})
    all_cohort_skills = {r["skill"] for recs in cohorts.values() for r in recs}
    assert "x" not in all_cohort_skills
    assert [r["skill"] for r in uneval] == ["x"]
    assert uneval[0]["total_invocations"] == 7


# ---- empty inputs ---------------------------------------------------------

def test_empty_inputs():
    cohorts, uneval = _cohorts({}, {})
    assert all(cohorts[k] == [] for k in M.COHORT_KEYS)
    assert uneval == []


# ---- report schema (integration of build_cohorts into build_report) -------

def test_build_report_schema(monkeypatch):
    monkeypatch.setattr(M, "load_quality", lambda: {"a": 0.9, "b": 0.2})
    monkeypatch.setattr(M, "load_usage", lambda since="": ({"a": 10, "z": 3}, False))
    rep = M.build_report(0.5, 5, "")
    assert set(rep.keys()) >= {
        "quality_threshold", "use_threshold", "window_since", "skills_evaluated",
        "usage_ledger_empty", "cohort_counts", "cohorts", "unevaluated_in_use",
    }
    assert rep["skills_evaluated"] == 2
    assert rep["usage_ledger_empty"] is False
    assert rep["cohort_counts"]["winners"] == 1            # a: 0.9 / 10
    assert rep["cohort_counts"]["deprecation_candidates"] == 1  # b: 0.2 / 0
    assert [r["skill"] for r in rep["unevaluated_in_use"]] == ["z"]


def test_build_report_flags_empty_ledger(monkeypatch):
    monkeypatch.setattr(M, "load_quality", lambda: {"a": 0.9})
    monkeypatch.setattr(M, "load_usage", lambda since="": ({}, True))
    rep = M.build_report(0.5, 5, "")
    assert rep["usage_ledger_empty"] is True
    assert rep["cohort_counts"]["silent_gems"] == 1  # high quality, absent usage
