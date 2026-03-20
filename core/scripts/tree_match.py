#!/usr/bin/env python3
"""Shared matching module for knowledge tree node lookup.

Extracted from retrieve.py so that both retrieve.py (full retrieval) and
tree.py (lightweight find) can reuse the same matching logic.

Functions:
    find_nodes(text, nodes, entity_index, top, leaf_only)
        - High-level: match + score + limit. No sibling/parent inclusion.
    build_concept_index(nodes)
        - Build entity-term -> [node_keys] from .md front matter.
    _match_nodes(category, nodes, entity_index, concept_index)
        - Run 4 matching strategies for a single category.
    _include_siblings / _include_parents / _compute_match_score / _score_and_limit
        - Retrieval helpers (used by retrieve.py for full retrieval).
    parse_front_matter(md_path)
        - Extract YAML front matter from a .md file.
"""

import re
import sys
from pathlib import Path

# Ensure stdout/stderr handle unicode on all platforms (Windows cp1252 fix)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

from _paths import PROJECT_ROOT

# Match channel scores for ranking (higher = more relevant)
CHANNEL_SCORES = {
    "exact_key": 4.0,
    "substring": 3.0,
    "entity_index": 2.5,
    "concept": 2.0,
    "word_prefix": 1.5,
    "sibling": 1.0,
    "parent": 0.5,
}

CAPABILITY_BONUS = {
    "MASTER": 0.5,
    "EXPLOIT": 0.3,
    "CALIBRATE": 0.1,
}


# ---------------------------------------------------------------------------
# Front matter parsing
# ---------------------------------------------------------------------------

def parse_front_matter(md_path):
    """Extract YAML front matter from a .md file. Returns dict or {}."""
    p = Path(md_path)
    if not p.exists():
        return {}
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return {}
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        fm = yaml.safe_load(parts[1])
        return fm if isinstance(fm, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Concept index from .md front matter entities
# ---------------------------------------------------------------------------

def build_concept_index(nodes):
    """Build entity-term -> [node_keys] index from .md front matter entities.

    Reads the `entities` list from each node's .md front matter.
    Returns dict mapping lowercase entity terms to lists of node keys.
    """
    index = {}
    for key, node in nodes.items():
        file_path = node.get("file")
        if not file_path:
            continue
        md_path = PROJECT_ROOT / file_path
        fm = parse_front_matter(md_path)

        # confidence/capability_level are NOT read from .md — they live
        # exclusively in _tree.yaml (split-by-nature schema). Do not re-add
        # a backfill here; it reintroduces the dual-source drift bug.
        entities = fm.get("entities", [])
        if not isinstance(entities, list):
            continue
        for entity in entities:
            term = str(entity).lower().strip()
            if term:
                index.setdefault(term, []).append(key)
    return index


# ---------------------------------------------------------------------------
# Matching engine (composable helpers)
# ---------------------------------------------------------------------------

def _match_nodes(category, nodes, entity_index, concept_index):
    """Run all 4 matching strategies for a single category.

    Returns (matched_list, match_channels_dict).
    matched_list: [(key, node), ...]
    match_channels_dict: {key: channel_name}
    """
    cat_lower = category.lower()
    matched = []
    matched_keys = set()
    channels = {}

    # Strategy 1: Substring — category in key/summary/topic (bidirectional)
    for key, node in nodes.items():
        key_lower = key.lower()
        summary = str(node.get("summary", "")).lower()
        topic = str(node.get("topic", key)).lower()

        if key_lower == cat_lower:
            # Exact key match (strongest)
            matched.append((key, node))
            matched_keys.add(key)
            channels[key] = "exact_key"
        elif (cat_lower in key_lower or cat_lower in summary or
              cat_lower in topic or key_lower in cat_lower):
            matched.append((key, node))
            matched_keys.add(key)
            channels[key] = "substring"

    # Strategy 2: Entity index — _tree.yaml entity_index lookup
    if cat_lower in entity_index:
        for en in entity_index[cat_lower].get("tree_nodes", []):
            if en in nodes and en not in matched_keys:
                matched.append((en, nodes[en]))
                matched_keys.add(en)
                channels[en] = "entity_index"

    # Strategy 3: Word-prefix — split on hyphens, prefix match (min 4 chars)
    cat_words_4 = {w for w in cat_lower.split("-") if len(w) >= 4}
    if cat_words_4:
        for key, node in nodes.items():
            if key in matched_keys:
                continue
            key_words = {w for w in key.lower().split("-") if len(w) >= 4}
            if any(cw.startswith(kw) or kw.startswith(cw)
                   for cw in cat_words_4 for kw in key_words):
                matched.append((key, node))
                matched_keys.add(key)
                channels[key] = "word_prefix"

    # Strategy 4: Concept — match query tokens against .md front-matter entities
    cat_tokens = {w for w in re.findall(r'[a-z0-9]+', cat_lower) if len(w) >= 3}
    if cat_tokens:
        for term, term_nodes in concept_index.items():
            # Check if any query token matches this entity term
            hit = False
            for token in cat_tokens:
                if token == term or token.startswith(term) or term.startswith(token):
                    hit = True
                    break
            if hit:
                for nk in term_nodes:
                    if nk in nodes and nk not in matched_keys:
                        matched.append((nk, nodes[nk]))
                        matched_keys.add(nk)
                        channels[nk] = "concept"

    return matched, matched_keys, channels


def _include_siblings(matched, matched_keys, channels, nodes):
    """Add siblings of D3+ direct matches for related context.

    Only applies to direct matches (not parent/sibling-included) at depth >= 3.
    Avoids pulling all 21 L2 children of intelligence when a single L2 matches.
    """
    direct_channels = {"exact_key", "substring", "entity_index", "concept", "word_prefix"}
    sibling_keys = set()
    for key, node in list(matched):
        depth = node.get("depth", 0)
        if depth >= 3 and channels.get(key) in direct_channels:
            parent_key = node.get("parent")
            if parent_key and parent_key in nodes:
                for sibling in nodes[parent_key].get("children", []):
                    if sibling != key and sibling not in matched_keys:
                        sibling_keys.add(sibling)

    for sk in sibling_keys:
        if sk in nodes:
            matched.append((sk, nodes[sk]))
            matched_keys.add(sk)
            channels[sk] = "sibling"

    return matched, matched_keys, channels


def _include_parents(matched, matched_keys, channels, nodes):
    """Add parent nodes for context when we matched L2+ nodes."""
    parent_keys = set()
    for key, node in matched:
        parent = node.get("parent")
        if parent and parent not in matched_keys:
            parent_keys.add(parent)

    for pk in parent_keys:
        if pk in nodes:
            matched.append((pk, nodes[pk]))
            matched_keys.add(pk)
            channels[pk] = "parent"

    return matched, matched_keys, channels


def _compute_match_score(key, node, channel):
    """Score a matched node by match quality. Higher = more relevant."""
    score = CHANNEL_SCORES.get(channel, 0.5)

    # Depth bonus: deeper = more specific knowledge (inverted from old depth-first)
    depth = node.get("depth", 0)
    if depth >= 3:
        score += 1.5
    elif depth == 2:
        score += 1.0
    elif depth == 1:
        score += 0.3

    # Confidence (0-1 range). YAML null → Python None; guard against it.
    score += node.get("confidence") or 0

    # Capability bonus
    score += CAPABILITY_BONUS.get(node.get("capability_level", ""), 0)

    return score


def _score_and_limit(matched, channels, limit):
    """Score all matched nodes, sort by match quality, apply limit."""
    scored = []
    for key, node in matched:
        channel = channels.get(key, "parent")
        ms = _compute_match_score(key, node, channel)
        scored.append((key, node, ms, channel))

    scored.sort(key=lambda x: -x[2])
    return scored[:limit]


# ---------------------------------------------------------------------------
# High-level find function (no sibling/parent inclusion)
# ---------------------------------------------------------------------------

def find_nodes(text, nodes, entity_index, top=3, leaf_only=False):
    """Find best-matching tree nodes for a text query.

    Unlike full retrieval, this does NOT include siblings/parents and does NOT
    read .md content or update retrieval counters. Designed for lightweight
    node lookup (e.g., tree-find-node.sh, tree.py read --find).

    Args:
        text: query string
        nodes: dict of node_key -> node_dict from _tree.yaml
        entity_index: entity_index dict from _tree.yaml
        top: max results to return (default 3)
        leaf_only: if True, filter to leaf nodes (empty children) before scoring

    Returns:
        list of dicts: [{key, score, file, depth, summary, node_type}, ...]
    """
    # Optionally filter to leaves
    if leaf_only:
        nodes = {k: v for k, v in nodes.items() if not v.get("children")}

    # Build concept index and run matching
    concept_index = build_concept_index(nodes)
    matched, matched_keys, channels = _match_nodes(
        text, nodes, entity_index, concept_index
    )

    # Score and limit (no sibling/parent inclusion)
    scored = _score_and_limit(matched, channels, top)

    # Build lightweight result dicts
    results = []
    for key, node, match_score, channel in scored:
        children = node.get("children", [])
        results.append({
            "key": key,
            "score": round(match_score, 2),
            "file": node.get("file", ""),
            "depth": node.get("depth", 0),
            "summary": node.get("summary", ""),
            "node_type": node.get("node_type", "leaf" if not children else "interior"),
        })

    return results
