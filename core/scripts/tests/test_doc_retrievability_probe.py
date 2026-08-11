"""Tests for doc-retrievability-probe.py score_presence ( fresh-eyes).

The scorer decides HIT/MISS by comparing the target's path against each
returned candidate's `framework_rules[].path` / `tree_nodes[].file`. Both are
read defensively (`entry.get(...) or ""`), so an absent or blank field
normalises to the empty string — and `want.endswith("")` is unconditionally
True. One field-less candidate therefore scored HIT for EVERY target, at rank
1, ahead of any real match: the degenerate mode of a findability instrument was
"everything is findable", which is the exact direction the skill this script
backs exists to prevent.

Measured on the live retriever before the fix: 15/15 tree_nodes carry `file`,
and `load_framework_rules` sorts on `e["path"]` so that producer cannot emit a
field-less row — the false HIT was unreachable from retrieve.sh but reachable
today through `--response-fixture`, and a future schema rename would flip every
probe to HIT silently.

Both directions are pinned deliberately (guard-1220): a scorer that returned
the same verdict on the must-MISS and must-HIT fixtures would have no
discriminating power, and the must-HIT cases are also the guard-1901
counter-check — narrowing the predicate must not cost the suffix tolerance the
predicate exists for.

Pattern: same importlib + sys.path shape as test_defer_drift_check.py (the
script name has hyphens, so it cannot be a plain `import`).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "doc-retrievability-probe.py"

TARGET = "core/config/conventions/temp-store.md"


def _import():
    spec = importlib.util.spec_from_file_location("doc_retrievability_probe", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["doc_retrievability_probe"] = mod
    spec.loader.exec_module(mod)
    return mod


# ── must MISS: a candidate with no usable path is not evidence of a match ──

def test_framework_rule_missing_path_key_does_not_score_hit():
    m = _import()
    r = m.score_presence({"framework_rules": [{"title": "unrelated rule"}]}, TARGET)
    assert r["verdict"] == "MISS", r
    assert r["found_at"] is None


def test_framework_rule_blank_path_value_does_not_score_hit():
    m = _import()
    r = m.score_presence({"framework_rules": [{"path": ""}]}, TARGET)
    assert r["verdict"] == "MISS", r


def test_tree_node_missing_file_key_does_not_score_hit():
    m = _import()
    r = m.score_presence({"tree_nodes": [{"key": "system/foo", "summary": "x"}]}, TARGET)
    assert r["verdict"] == "MISS", r


def test_fieldless_entry_does_not_steal_rank_from_a_real_match():
    """The pre-fix bug reported rank 1 / path "" even when the target WAS returned."""
    m = _import()
    r = m.score_presence(
        {"framework_rules": [{"path": ""}, {"path": TARGET}]}, TARGET)
    assert r["verdict"] == "HIT", r
    assert r["found_at"]["path"] == TARGET
    assert r["found_at"]["rank"] == 2, "rank must count the real match's position"


def test_fieldless_entry_is_still_counted_as_a_returned_candidate():
    """It cannot evidence a match, but the retriever did return it.

    `candidates_returned` is quoted alongside every MISS to keep the verdict
    depth-bounded, so under-counting here would overstate how narrow the
    candidate list was.
    """
    m = _import()
    r = m.score_presence({"framework_rules": [{"path": ""}, {"path": ""}]}, TARGET)
    assert r["verdict"] == "MISS", r
    assert r["candidates_returned"] == 2


# ── must HIT: the suffix tolerance the predicate exists for is intact ──

def test_exact_repo_relative_path_scores_hit():
    m = _import()
    r = m.score_presence({"framework_rules": [{"path": TARGET}]}, TARGET)
    assert r["verdict"] == "HIT", r
    assert r["found_at"]["bucket"] == "framework_rules"


def test_dot_prefixed_and_backslash_forms_still_match():
    m = _import()
    for variant in ("./" + TARGET, TARGET.replace("/", "\\")):
        r = m.score_presence({"framework_rules": [{"path": variant}]}, TARGET)
        assert r["verdict"] == "HIT", (variant, r)


def test_absolute_target_still_matches_a_relative_candidate():
    m = _import()
    absolute = "/opt/ayoai-mind/" + TARGET
    r = m.score_presence({"framework_rules": [{"path": TARGET}]}, absolute)
    assert r["verdict"] == "HIT", r


def test_unrelated_path_scores_miss():
    """Negative control — without this the must-HIT cases prove nothing."""
    m = _import()
    r = m.score_presence({"framework_rules": [{"path": ".claude/rules/self.md"}]}, TARGET)
    assert r["verdict"] == "MISS", r
    assert r["candidates_returned"] == 1
