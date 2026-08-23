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


# Parsed-tree + concept-index cache.
#
# evaluate() re-read and re-parsed _tree.yaml AND rebuilt the concept index on
# EVERY call. Measured 2026-08-21 (echo, cc-03): 2.1s per call against a
# 1,531,241-byte tree, with module import at 0.03s — i.e. the cost is entirely
# per-call, not startup. That dominated goal-selector's category resolution
# (: 23.0s / 33.6% of every select) and is paid again by every daemon
# aspirations_write that suggests a category.
#
# Keyed on (path, mtime_ns, size) so an edited tree invalidates naturally — a
# bare path key would serve stale nodes after any /tree write, which on this
# corpus is frequent. Cheap: one stat() per call.
#
# SAFE TO SHARE (guard-1663 — never mutate a record from a shared cache):
# evaluate only iterates `nodes.items()` and appends to a LOCAL results list;
# neither `nodes` nor `concept_index` is mutated downstream. Verified before
# caching, not assumed. Any future caller that needs to mutate either MUST copy
# first.
_TREE_CACHE: dict = {}


def _load_tree_cached(tree_path: Path, world_root: Optional[Path] = None):
    """Return (nodes, concept_index, error_reason). error_reason is None on success.

    Mirrors the error branches evaluate() used inline, so telemetry reasons are
    unchanged.

    ``world_root`` is the world that OWNS this tree: node bodies are resolved
    under it when the concept index is built, and it is part of the cache key
    because the index depends on it. A long-lived daemon MUST pass it:
    tree_match's fallback is the import-bound ``_paths.WORLD_DIR``, which is
    None on a daemon started before its world existed (the add-goal request
    then 500s from assert_world_dir) and agent A's world on a daemon serving
    agent B (the entity channel silently empties). g-367-14 — the fourth
    daemon call site of the g-367-08 class.
    """
    try:
        st = tree_path.stat()
    except OSError:
        return None, None, "tree read/parse error"
    key = (str(tree_path), st.st_mtime_ns, st.st_size, str(world_root))
    hit = _TREE_CACHE.get(key)
    if hit is not None:
        return hit[0], hit[1], None

    try:
        with open(tree_path, "r", encoding="utf-8") as f:
            tree = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return None, None, "tree read/parse error"
    if not isinstance(tree, dict):
        return None, None, "tree root not a dict"
    nodes = tree.get("nodes", {})
    if not nodes:
        return None, None, "tree has no nodes"

    concept_index = build_concept_index(nodes, world_root=world_root)
    # Bound the cache: a long-running daemon would otherwise retain one entry
    # per tree revision for the life of the process. Only the current revision
    # is ever hit, so keeping the newest few is sufficient.
    if len(_TREE_CACHE) >= 3:
        _TREE_CACHE.clear()
    _TREE_CACHE[key] = (nodes, concept_index)
    return nodes, concept_index, None


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
        world_root = world_dir
    else:
        # An explicit tree_path leaves world_dir unused (documented above), so
        # the bodies' root is the world that owns THIS tree -- derivable only
        # when it sits at the canonical <world>/knowledge/tree/_tree.yaml.
        # Otherwise None: tree_match falls back to its module global, the
        # pre- behaviour, which is correct in a fresh CLI process.
        world_root = (tree_path.parents[2]
                      if tree_path.parts[-3:-1] == ("knowledge", "tree") else None)

    if not tree_path.exists():
        _emit_telemetry(text, [], reason="tree file missing")
        return []

    nodes, concept_index, err = _load_tree_cached(tree_path, world_root)
    if err:
        _emit_telemetry(text, [], reason=err)
        return []
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
