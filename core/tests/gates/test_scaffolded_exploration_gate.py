"""Behavior tests for scaffolded_exploration gate (PR 7c/2).

No CLI subprocess — this gate has no standalone script (it was inline in
aspirations.py:cmd_add_goal). Tests exercise the pure evaluate() module.

Decision branches:
  Skip:    title does NOT start with "Apply:" → pass
  Skip:    Apply: title but category not in product_category_prefixes → pass
  Skip:    Apply: + product category + discovered_by → pass (precursor present)
  Block:   Apply: + product category + no discovered_by + no override
  Override: Apply: + product category + no discovered_by + override
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "core" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from gates.scaffolded_exploration import evaluate, DEFAULT_PRODUCT_PREFIXES


# ---------------------------------------------------------------------------
# Skip paths
# ---------------------------------------------------------------------------

def test_non_apply_title_skips():
    """Investigate / Idea / Maintain / Unblock titles → gate doesn't apply."""
    for title in ("Investigate: foo", "Idea: bar", "Maintain: baz",
                  "Unblock: qux", "Random title with no prefix"):
        out = evaluate({"title": title, "category": "npc-build"})
        assert out["would_block"] is False, f"title {title!r} should skip"
        assert "gate does not apply" in out["reason"]
        assert out["matched_category_prefix"] is None


def test_apply_non_product_category_skips():
    """Apply: in a framework-loop or system category → skip."""
    for category in ("framework-loop", "system-constraints", "uncategorized",
                     "knowledge-tree", ""):
        out = evaluate({"title": "Apply: improve framework",
                        "category": category})
        assert out["would_block"] is False, f"category {category!r} should skip"
        assert out["matched_category_prefix"] is None


def test_apply_product_with_discovered_by_passes():
    """Apply + product category + discovered_by → Investigate precursor
    cited → pass without block."""
    out = evaluate({
        "title": "Apply: NPC behavior fix",
        "category": "npc-build",
        "discovered_by": "g-115-001",
    })
    assert out["would_block"] is False
    assert "Investigate precursor present" in out["reason"]
    assert out["matched_category_prefix"] == "npc-"


@pytest.mark.parametrize("prefix", DEFAULT_PRODUCT_PREFIXES)
def test_all_default_prefixes_match(prefix):
    """Every default product prefix must trigger the gate when used."""
    out = evaluate({
        "title": "Apply: something",
        "category": prefix + "foo",
    })
    assert out["would_block"] is True
    assert out["matched_category_prefix"] == prefix


# ---------------------------------------------------------------------------
# Block path
# ---------------------------------------------------------------------------

def test_apply_product_without_discovered_by_blocks():
    out = evaluate({
        "title": "Apply: implement skill",
        "category": "ayoai-feature",
    })
    assert out["would_block"] is True
    assert out["matched_category_prefix"] == "ayoai-"
    assert "Investigate precursor" in out["reason"]
    assert "world/conventions/scaffolded-exploration.md" in out["reason"]
    assert out["override_applied"] is None


def test_empty_discovered_by_blocks():
    """discovered_by=None and discovered_by='' both treat as 'no precursor'."""
    for empty_val in (None, "", 0, False):
        out = evaluate({
            "title": "Apply: foo",
            "category": "processor-bar",
            "discovered_by": empty_val,
        })
        assert out["would_block"] is True, f"discovered_by={empty_val!r} should block"


# ---------------------------------------------------------------------------
# Override
# ---------------------------------------------------------------------------

def test_override_when_blocking():
    out = evaluate(
        {"title": "Apply: something", "category": "intelligence-foo"},
        override_no_investigate="emergency scaffolding for hotfix",
    )
    assert out["would_block"] is False
    assert out["override_applied"] == "emergency scaffolding for hotfix"
    assert out["matched_category_prefix"] == "intelligence-"


def test_override_when_not_blocking_unused():
    """Override flag on a passing goal → override_applied stays None.

    Matches sibling-gate contract: override only applies when it would
    have blocked. (Same as origin-signal-gate's --override-signal.)"""
    out = evaluate(
        {"title": "Apply: something", "category": "framework-loop"},
        override_no_investigate="unused override",
    )
    assert out["would_block"] is False
    assert out["override_applied"] is None


# ---------------------------------------------------------------------------
# Custom prefix list (the parameterization that enables domain-decoupling)
# ---------------------------------------------------------------------------

def test_custom_prefix_list():
    """Caller-provided prefix list — proves the gate is domain-decoupled."""
    out = evaluate(
        {"title": "Apply: something", "category": "custom-prefix-foo"},
        product_category_prefixes=("custom-prefix-",),
    )
    assert out["would_block"] is True
    assert out["matched_category_prefix"] == "custom-prefix-"


def test_empty_prefix_list_disables_gate():
    """Empty prefix list → no category matches → gate always skips."""
    out = evaluate(
        {"title": "Apply: something", "category": "npc-build"},
        product_category_prefixes=(),
    )
    assert out["would_block"] is False
    assert out["matched_category_prefix"] is None


# ---------------------------------------------------------------------------
# Edge cases on goal shape
# ---------------------------------------------------------------------------

def test_missing_title_skips():
    out = evaluate({"category": "npc-build"})
    assert out["would_block"] is False
    assert "not an Apply: goal" in out["reason"]


def test_missing_category_skips():
    out = evaluate({"title": "Apply: foo"})
    assert out["would_block"] is False
    assert "not in product prefixes" in out["reason"]


def test_empty_goal_dict_skips():
    out = evaluate({})
    assert out["would_block"] is False


def test_none_goal_does_not_crash():
    """Pathological input — module should not raise."""
    out = evaluate(None)
    assert out["would_block"] is False
