"""Regression tests for the  utilization-score shrinkage denominator.

rb/guardrail entries take DIRECT times_helpful bumps for context-carried
citations that never pass a tracked retrieve.sh scan, so th > retrieval_count
is legitimate data. Under the old ``max(rc, 1)`` denominator an untested
h=5/rc=0 entry scored 5.0 and permanently outranked every scan-tested entry
in sort_universal_rbs (221 rb + 1 guardrail live violations, 2026-07-11).

New denominator: ``max(rc, credited_usages) + 1`` — caps context-only entries
below 1.0 with an n-confidence gradient, order-preserving where rc dominates.
The tree-node utility_ratio formula deliberately KEEPS ``max(rc, 1)`` (its
th<=rc precondition holds: 0 violations across 1175 nodes).

Pins BOTH implementations (CLI reasoning-bank.py + daemon store_registry.py)
to identical outputs — the verbatim-twin doctrine.
"""
import importlib.util
import sys
from pathlib import Path

import pytest  # noqa: F401 — harness parity with sibling suites

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "core" / "scripts"))
sys.path.insert(0, str(_ROOT / "mind_api" / "src"))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses resolve types via sys.modules[cls.__module__]
    spec.loader.exec_module(mod)
    return mod


rb_mod = _load("rb_cli", _ROOT / "core" / "scripts" / "reasoning-bank.py")
import store_registry as sr_mod  # noqa: E402 — mind_api/src on sys.path above


def _rec(rc=0, th=0, tih=0, ta=0, tc=0):
    return {"utilization": {
        "retrieval_count": rc, "times_helpful": th,
        "times_inferred_helpful": tih, "times_active": ta,
        "times_cited": tc, "times_noise": 0, "times_skipped": 0,
        "last_retrieved": None, "utilization_score": 0.0,
        "utilization_score_v2": 0.0,
    }}


def _both(rc=0, th=0, tih=0, ta=0, tc=0):
    """Run both implementations; assert byte-identical scores; return one."""
    a, b = _rec(rc, th, tih, ta, tc), _rec(rc, th, tih, ta, tc)
    rb_mod.recompute_utilization_score(a)
    sr_mod._recompute_utilization_score(b)
    assert a["utilization"]["utilization_score"] == b["utilization"]["utilization_score"], \
        "CLI/daemon twin divergence (v1)"
    assert a["utilization"]["utilization_score_v2"] == b["utilization"]["utilization_score_v2"], \
        "CLI/daemon twin divergence (v2)"
    return a["utilization"]


def test_context_only_entry_capped_below_one():
    """The headline inflation case: h=1/rc=0 scored 1.0 (== a perfect tested
    entry); h=5/rc=0 scored 5.0. Both must now sit below 1.0."""
    assert _both(rc=0, th=1)["utilization_score"] == 0.5
    assert _both(rc=0, th=5)["utilization_score"] == round(5 / 6, 4)


def test_n_confidence_gradient():
    """More confirmations -> higher score, approaching 1.0 asymptotically."""
    s1 = _both(rc=0, th=1)["utilization_score"]
    s3 = _both(rc=0, th=3)["utilization_score"]
    s9 = _both(rc=0, th=9)["utilization_score"]
    assert s1 < s3 < s9 < 1.0


def test_tested_entry_outranks_thin_context_entry():
    """A well-tested mostly-helpful entry must outrank a single context bump —
    the ordering the old formula inverted."""
    tested = _both(rc=10, th=8)["utilization_score"]     # 8/11 ≈ 0.727
    context = _both(rc=0, th=1)["utilization_score"]     # 0.5
    assert tested > context


def test_rc_dominant_ordering_preserved():
    """Among entries where rc >= usages (the scan-tested population), the +1
    shift is uniform — relative ordering unchanged from the old formula."""
    lo = _both(rc=10, th=5)["utilization_score"]   # 5/11
    hi = _both(rc=10, th=8)["utilization_score"]   # 8/11
    assert hi > lo


def test_v2_context_citation_inflation_fixed():
    """v2's old denominator (rc+1) ignored usage: tc=3/rc=0 scored 3.0.
    Usage-aware denominator caps it below 1.0 gradient-style."""
    u = _both(rc=0, tc=3)
    assert u["utilization_score_v2"] == 0.75  # 3.0 / (3 + 1)


def test_v2_rc_dominant_identical_to_old_formula():
    """When rc dominates all counters, v2's denominator is rc+1 exactly as
    before — the tested population's v2 scores do not move at all."""
    u = _both(rc=10, th=1, tc=2)
    assert u["utilization_score_v2"] == round((1 + 2.0) / 11, 4)


def test_zero_everything_scores_zero():
    u = _both()
    assert u["utilization_score"] == 0.0
    assert u["utilization_score_v2"] == 0.0
