"""Category-suggest engine — daemon-safe extraction (PR 7c/4).

Given a free-text string (goal title, description, etc.), returns the
best-matching tree node key(s) by scoring against node keys, summaries,
and .md front matter entities. Used by `cmd_add_goal` to auto-assign
`goal.category` when the caller didn't pick one.

Public API:
    evaluate(text, *, top_n=3, world_dir, tree_path=None) -> list[dict]

Return shape (matches the legacy CLI's stdout JSON byte-for-byte):
    [
      {"key": "<tree-node-key>", "score": <float>, "summary": "<str>"},
      ...
    ]
    Empty list when:
      - tree file missing
      - tree parsed but `nodes` empty
      - no node scored > 0

Decision telemetry (`_gate_log`) is emitted INSIDE evaluate() so daemon
callers get parity with the CLI invocation. CLI shim must NOT emit again.

Scoring weights are tuned for category assignment, NOT retrieval — do not
unify with tree_match scoring.

Daemon safety:
  - Reads no env directly. world_dir is explicit.
  - tree_path defaults to <world_dir>/knowledge/tree/_tree.yaml; callers
    can override (tests, alternate tree layouts).
  - File read errors → empty list (fail-open / "no suggestion").
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional

import yaml  # type: ignore

from tree_match import build_concept_index  # type: ignore
from _gate_log import log as _gate_log  # type: ignore


# Structural nodes excluded from category suggestions (too broad)
STRUCTURAL_DEPTHS = {0, 1}


def tokenize(text: str) -> set[str]:
    """Split text into lowercase tokens, strip punctuation, min 3 chars."""
    words = re.findall(r'[a-zA-Z0-9]+', text.lower())
    return {w for w in words if len(w) >= 3}


def score_node(key: str, node: dict, text_tokens: set[str],
               text_lower: str, concept_index: dict) -> float:
    """Score a single tree node against input text. Higher = better match.

    Weights (intentionally different from tree_match channel scoring):
      1. Exact key substring in text:          +3.0
      2. Word overlap with key segments:       +1.0 per match
      3. Summary word overlap:                 +0.5 per match (cap 3.0)
      4. Front-matter entity overlap (exact):  +1.5 per match
      4'. Entity partial match:                +0.75 per match
    """
    score = 0.0
    key_lower = key.lower()

    if key_lower in text_lower:
        score += 3.0

    key_segments = {w for w in key_lower.split("-") if len(w) >= 3}
    key_overlap = key_segments & text_tokens
    score += len(key_overlap) * 1.0

    summary = str(node.get("summary", "")).lower()
    summary_tokens = tokenize(summary)
    summary_overlap = summary_tokens & text_tokens
    score += min(len(summary_overlap) * 0.5, 3.0)

    for term, node_keys in concept_index.items():
        if key not in node_keys:
            continue
        if term in text_tokens:
            score += 1.5
        elif any(term in t or t in term for t in text_tokens if len(t) >= 3):
            score += 0.75

    return score


def evaluate(text: str, *, top_n: int = 3,
             world_dir: Optional[Path] = None,
             tree_path: Optional[Path] = None) -> List[dict]:
    """Return top-N tree node key matches for the given text.

    Args:
        text: Free text to match.
        top_n: Number of top matches to return.
        world_dir: Required when tree_path is omitted — used to derive the
            default tree location <world_dir>/knowledge/tree/_tree.yaml.
        tree_path: Explicit override of the tree file location. When set,
            world_dir is unused.

    Returns: list of {key, score, summary} sorted by score desc, up to top_n.
        Empty list on any error / no matches (the gate never raises).
    """
    if tree_path is None:
        if world_dir is None:
            _emit_telemetry(text, [], reason="no world_dir")
            return []
        tree_path = world_dir / "knowledge" / "tree" / "_tree.yaml"

    if not tree_path.exists():
        _emit_telemetry(text, [], reason="tree file missing")
        return []

    try:
        with open(tree_path, "r", encoding="utf-8") as f:
            tree = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        _emit_telemetry(text, [], reason="tree read/parse error")
        return []

    if not isinstance(tree, dict):
        _emit_telemetry(text, [], reason="tree root not a dict")
        return []

    nodes = tree.get("nodes", {})
    if not nodes:
        _emit_telemetry(text, [], reason="tree has no nodes")
        return []

    concept_index = build_concept_index(nodes)
    text_lower = text.lower()
    text_tokens = tokenize(text)

    results = []
    for key, node in nodes.items():
        depth = node.get("depth", 0)
        if depth in STRUCTURAL_DEPTHS:
            continue
        score = score_node(key, node, text_tokens, text_lower, concept_index)
        if score > 0:
            results.append({
                "key": key,
                "score": round(score, 2),
                "summary": node.get("summary", ""),
            })

    results.sort(key=lambda x: -x["score"])
    matches = results[:top_n]
    _emit_telemetry(text, matches)
    return matches


def _emit_telemetry(text: str, matches: list, *, reason: Optional[str] = None) -> None:
    """Best-effort telemetry. Matches the legacy CLI's _gate_log shape."""
    try:
        _gate_log(
            "category-suggest",
            "pass" if matches else "noop",
            caller="gates.category_suggest:evaluate",
            trigger_matched=(matches[0]["key"] if matches else None),
            payload=text[:200],
            extra={
                "match_count": len(matches),
                "top_score": matches[0]["score"] if matches else None,
                "reason": reason,  # None for normal pass/noop paths
            },
        )
    except Exception:
        pass
