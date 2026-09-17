"""Predicate tests for bulk-retire-tree-leaves.py ().

This script proposes DESTRUCTIVE tree-leaf retirement and carried no test
coverage at all. Its dead-leaf predicate is the literal guard-731 subject —
`retrieval_count == 0` means "never retrieved, safe to retire" — which is
exactly the value the g-358-173 retrieval spool defers. If a bump is sitting
un-flushed, an index-only read hands this predicate a FALSE ZERO on a node that
is demonstrably alive, so `main()` now folds pending spool deltas before
classifying. These tests pin the predicate itself: the retrieval counter is
what protects a node, and every other clause is an independent veto.
"""

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "bulk-retire-tree-leaves.py"
sys.path.insert(0, str(SCRIPT.parent))

_SPEC = importlib.util.spec_from_file_location("bulk_retire_tree_leaves", SCRIPT)
brtl = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(brtl)

TODAY = date(2026, 9, 17)
MIN_AGE = 30


def _leaf(**over):
    """A node that IS a dead candidate; each test negates one clause."""
    node = {
        "node_type": "leaf",
        "retrieval_count": 0,
        "depth": 3,
        "growth_state": "stable",
        "last_updated": "2026-01-01",       # ~259 days old
    }
    node.update(over)
    return node


# ------------------------------------------------------------ dead leaves --

def test_baseline_leaf_is_a_dead_candidate():
    assert brtl._is_dead_candidate("k", _leaf(), MIN_AGE, TODAY) is True


def test_any_retrieval_protects_the_node():
    """THE guard-731 clause, and the reason the spool needs a read-merge: a
    single retrieval is enough to disqualify retirement."""
    assert brtl._is_dead_candidate(
        "k", _leaf(retrieval_count=1), MIN_AGE, TODAY) is False


def test_a_null_retrieval_count_is_treated_as_zero():
    assert brtl._is_dead_candidate(
        "k", _leaf(retrieval_count=None), MIN_AGE, TODAY) is True


@pytest.mark.parametrize("over", [
    {"node_type": "interior"},
    {"node_type": "archived"},
    {"depth": 1},
    {"depth": 0},
    {"growth_state": "growing"},
])
def test_each_clause_is_an_independent_veto(over):
    assert brtl._is_dead_candidate("k", _leaf(**over), MIN_AGE, TODAY) is False


def test_a_young_leaf_is_not_a_candidate():
    assert brtl._is_dead_candidate(
        "k", _leaf(last_updated="2026-09-10"), MIN_AGE, TODAY) is False


def test_a_leaf_with_no_parseable_date_is_never_a_candidate():
    """Default-to-keep: no age clock means no eligibility."""
    node = _leaf()
    node.pop("last_updated")
    assert brtl._is_dead_candidate("k", node, MIN_AGE, TODAY) is False


def test_last_retrieved_is_the_age_fallback():
    node = _leaf()
    node.pop("last_updated")
    node["last_retrieved"] = "2026-01-01"
    assert brtl._node_age_days(node, TODAY) == 259


# ----------------------------------------------------------- noisy leaves --

def _noisy(**over):
    node = {
        "node_type": "leaf",
        "retrieval_count": 50,
        "utility_ratio": 0.01,
        "depth": 3,
        "growth_state": "stable",
    }
    node.update(over)
    return node


def test_baseline_leaf_is_a_noisy_candidate():
    assert brtl._is_noisy_candidate("k", _noisy(), 10, 0.1) is True


def test_too_few_retrievals_is_not_yet_noise():
    """Under-exposed nodes must not be judged on a thin sample."""
    assert brtl._is_noisy_candidate("k", _noisy(retrieval_count=9), 10, 0.1) is False


def test_a_useful_node_is_not_noise():
    assert brtl._is_noisy_candidate("k", _noisy(utility_ratio=0.5), 10, 0.1) is False


def test_utility_exactly_at_threshold_is_not_noise():
    assert brtl._is_noisy_candidate("k", _noisy(utility_ratio=0.1), 10, 0.1) is False
