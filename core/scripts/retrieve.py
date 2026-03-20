#!/usr/bin/env python3
"""Unified retrieval engine — single script call replaces the 5-phase retrieval protocol.

Reads ALL relevant data stores (tree nodes, reasoning bank, guardrails, pattern
signatures, experiences, beliefs, experiential index), increments retrieval
counters, and returns a single JSON blob to stdout.

Usage:
    retrieve.sh --category <cat> --depth shallow|medium|deep
    retrieve.sh --category "cat1,cat2" --depth medium       # multi-category
    retrieve.sh --supplementary-only --category <cat>       # skip tree nodes

All depth levels return full results with .md content (depth limits removed).
Use --supplementary-only to skip tree node matching and only load reasoning bank,
guardrails, pattern signatures, experiences, beliefs, and experiential index.

The LLM calls this for supplementary stores; for tree nodes, the LLM reads
_tree.yaml directly and retrieves specific nodes via Read tool.

Matching strategies (applied in order, results merged):
  1. Substring: category appears in key/summary/topic (bidirectional)
  2. Entity index: category matches a semantic entity in _tree.yaml
  3. Word-prefix: hyphen-split words, prefix match (min 4 chars)
  4. Concept: .md front-matter entities matched against query tokens

Results are scored by match quality (not depth-first), so specific deep nodes
rank above generic parents when they match directly. Sibling inclusion adds
related D3+ nodes for context.
"""

import argparse
import json
import os
import sys
from datetime import date, datetime
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

from _paths import PROJECT_ROOT, MIND_DIR

TREE_PATH = MIND_DIR / "knowledge" / "tree" / "_tree.yaml"
RB_PATH = MIND_DIR / "reasoning-bank.jsonl"
GUARD_PATH = MIND_DIR / "guardrails.jsonl"
SIGS_PATH = MIND_DIR / "pattern-signatures.jsonl"
EXP_PATH = MIND_DIR / "experience.jsonl"
BELIEFS_PATH = MIND_DIR / "knowledge" / "beliefs.yaml"
EI_PATH = MIND_DIR / "experiential-index.yaml"

# All depths return the same limits — retrieval intelligence is in the LLM, not here.
# Do not re-differentiate these; the old 3/7/12 split was replaced by LLM-driven node selection.
DEPTH_LIMITS = {"shallow": 50, "medium": 50, "deep": 50}
EXP_LIMITS = {"shallow": 25, "medium": 25, "deep": 25}

# Matching engine imported from shared module
from tree_match import (
    build_concept_index, _match_nodes, _include_siblings,
    _include_parents, _score_and_limit, CHANNEL_SCORES,
)


# ---------------------------------------------------------------------------
# Helpers: file I/O (same patterns as experience.py, pipeline.py)
# ---------------------------------------------------------------------------

def read_jsonl(path):
    """Read JSONL file, return list of dicts. Returns [] if missing/empty."""
    p = Path(path)
    if not p.exists():
        return []
    items = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped:
                items.append(json.loads(stripped))
    return items


def write_jsonl(path, items):
    """Atomically write list of dicts as JSONL."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(p) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for item in items:
            # ensure_ascii=True: prevents mojibake/surrogates from bricking the file
            f.write(json.dumps(item, ensure_ascii=True) + "\n")
    os.replace(str(tmp), str(p))


def read_yaml(path):
    """Read YAML file, return dict. Returns {} if missing/empty."""
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def write_yaml(path, data):
    """Atomic write YAML preserving key order."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(p) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=None, sort_keys=False,
                  allow_unicode=True, width=200)
    os.replace(str(tmp), str(p))


def today_str():
    return date.today().isoformat()


def now_str():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# Tree node loading (main entry point for tree retrieval)
# ---------------------------------------------------------------------------

def load_tree_nodes(categories, depth):
    """Load matching tree nodes for one or more categories.

    Args:
        categories: list of category strings (supports multi-category)
        depth: "shallow", "medium", or "deep"

    Returns list of node dicts with content and match metadata.
    """
    if not TREE_PATH.exists():
        return [], set()

    tree = read_yaml(TREE_PATH)
    nodes = tree.get("nodes", {})
    if not nodes:
        return [], set()

    limit = DEPTH_LIMITS.get(depth, 50)

    # Build concept index once (shared across multi-category)
    concept_index = build_concept_index(nodes)
    entity_index = tree.get("entity_index", {})

    # Match across all categories, merge with dedup (keep best channel)
    all_matched = {}  # key -> node
    all_channels = {}  # key -> best channel
    all_matched_keys = set()

    for cat in categories:
        cat_matched, cat_keys, cat_channels = _match_nodes(
            cat, nodes, entity_index, concept_index
        )
        for key, node in cat_matched:
            if key not in all_matched:
                all_matched[key] = node
                all_channels[key] = cat_channels.get(key, "substring")
                all_matched_keys.add(key)
            else:
                # Keep the higher-scoring channel
                existing = CHANNEL_SCORES.get(all_channels.get(key, ""), 0)
                new = CHANNEL_SCORES.get(cat_channels.get(key, ""), 0)
                if new > existing:
                    all_channels[key] = cat_channels[key]

    # Convert to list form for sibling/parent inclusion
    matched = [(k, v) for k, v in all_matched.items()]

    # Sibling inclusion for D3+ direct matches
    matched, all_matched_keys, all_channels = _include_siblings(
        matched, all_matched_keys, all_channels, nodes
    )

    # Parent inclusion for context
    matched, all_matched_keys, all_channels = _include_parents(
        matched, all_matched_keys, all_channels, nodes
    )

    # Score and limit
    scored = _score_and_limit(matched, all_channels, limit)

    # Build results with content and match metadata
    results = []
    tree_modified = False
    retrieval_channels_used = set()

    for key, node, match_score, channel in scored:
        entry = {
            "key": key,
            "file": node.get("file", ""),
            "summary": node.get("summary", ""),
            "depth": node.get("depth", 0),
            "confidence": node.get("confidence", 0),
            "capability_level": node.get("capability_level", ""),
            "match_channel": channel,
            "match_score": round(match_score, 2),
            "content": None,
        }

        if entry["file"]:
            md_path = PROJECT_ROOT / entry["file"]
            if md_path.exists():
                try:
                    entry["content"] = md_path.read_text(encoding="utf-8")
                except Exception:
                    entry["content"] = None

        # Increment retrieval_count on the tree node
        rc = node.get("retrieval_count", 0)
        node["retrieval_count"] = rc + 1
        node["last_retrieved"] = today_str()
        tree_modified = True

        retrieval_channels_used.add(channel)
        results.append(entry)

    # Write back tree with updated counters
    if tree_modified:
        tree["last_updated"] = today_str()
        write_yaml(TREE_PATH, tree)

    return results, retrieval_channels_used


# ---------------------------------------------------------------------------
# Supporting data loaders (unchanged logic, load ALL active)
# ---------------------------------------------------------------------------

def load_reasoning_bank():
    """Load ALL active reasoning bank entries. Increment retrieval counters."""
    records = read_jsonl(RB_PATH)
    active = [r for r in records if r.get("status") == "active"]

    modified = False
    for rec in records:
        if rec.get("status") != "active":
            continue
        util = rec.setdefault("utilization", {})
        util["retrieval_count"] = util.get("retrieval_count", 0) + 1
        util["last_retrieved"] = today_str()
        modified = True

    if modified:
        write_jsonl(RB_PATH, records)

    return active


def load_guardrails():
    """Load ALL active guardrails. Increment retrieval counters."""
    records = read_jsonl(GUARD_PATH)
    active = [r for r in records if r.get("status") == "active"]

    modified = False
    for rec in records:
        if rec.get("status") != "active":
            continue
        util = rec.setdefault("utilization", {})
        util["retrieval_count"] = util.get("retrieval_count", 0) + 1
        util["last_retrieved"] = today_str()
        modified = True

    if modified:
        write_jsonl(GUARD_PATH, records)

    return active


def load_pattern_signatures():
    """Load ALL active pattern signatures. Increment retrieval counters."""
    records = read_jsonl(SIGS_PATH)
    active = [r for r in records if r.get("status") == "active"]

    modified = False
    for rec in records:
        if rec.get("status") != "active":
            continue
        util = rec.setdefault("utilization", {})
        util["retrieval_count"] = util.get("retrieval_count", 0) + 1
        util["last_retrieved"] = today_str()
        modified = True

    if modified:
        write_jsonl(SIGS_PATH, records)

    return active


def load_experiences(categories, depth):
    """Load top N experiences matching any category. Increment retrieval counters."""
    records = read_jsonl(EXP_PATH)
    limit = EXP_LIMITS.get(depth, 5)

    # Filter by any category match + not archived
    matching = []
    for r in records:
        if r.get("archived", False):
            continue
        exp_cat = r.get("category", "").lower()
        if any(c.lower() in exp_cat for c in categories):
            matching.append(r)

    # Sort by retrieval_count descending (most-proven first)
    matching.sort(
        key=lambda r: r.get("retrieval_stats", {}).get("retrieval_count", 0),
        reverse=True,
    )

    selected = matching[:limit]
    selected_ids = {r["id"] for r in selected}

    # Increment retrieval_count on selected records in the full list
    modified = False
    for rec in records:
        if rec.get("id") in selected_ids:
            stats = rec.setdefault("retrieval_stats", {})
            stats["retrieval_count"] = stats.get("retrieval_count", 0) + 1
            stats["last_retrieved"] = today_str()
            modified = True

    if modified:
        write_jsonl(EXP_PATH, records)

    return selected


def load_beliefs(categories):
    """Load active/weakened beliefs. Returns list of belief dicts."""
    beliefs_data = read_yaml(BELIEFS_PATH)
    if not beliefs_data:
        return []

    beliefs_list = beliefs_data.get("beliefs", [])
    if not isinstance(beliefs_list, list):
        return []

    return [
        b for b in beliefs_list
        if b.get("status") in ("active", "weakened")
    ]


def load_experiential_index(categories):
    """Load experiential index entries for categories."""
    ei = read_yaml(EI_PATH)
    if not ei:
        return {}

    by_cat = ei.get("by_category", {})
    merged = {}

    for cat in categories:
        cat_lower = cat.lower()
        # Try exact match first, then substring
        if cat_lower in by_cat:
            merged.update(by_cat[cat_lower])
            continue
        for key, val in by_cat.items():
            if cat_lower in key.lower() or key.lower() in cat_lower:
                merged.update(val)
                break

    return merged


# ---------------------------------------------------------------------------
# Main: assemble and output
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Unified retrieval — single call returns all context."
    )
    parser.add_argument("--category", required=True,
                        help="Knowledge category (comma-separated for multi)")
    parser.add_argument("--depth", choices=["shallow", "medium", "deep"],
                        default="deep",
                        help="(Deprecated — all depths return full results)")
    parser.add_argument("--supplementary-only", action="store_true",
                        help="Skip tree node matching, only return supplementary stores")
    args = parser.parse_args()

    # Support comma-separated multi-category
    categories = [c.strip() for c in args.category.split(",") if c.strip()]
    depth = args.depth

    # Load tree nodes unless --supplementary-only
    if args.supplementary_only:
        tree_nodes, retrieval_channels = [], set()
    else:
        tree_nodes, retrieval_channels = load_tree_nodes(categories, depth)

    # Load supplementary stores (always)
    reasoning_bank = load_reasoning_bank()
    guardrails = load_guardrails()
    pattern_signatures = load_pattern_signatures()
    experiences = load_experiences(categories, depth)
    beliefs = load_beliefs(categories)
    experiential_index = load_experiential_index(categories)

    # Stderr summary for visibility
    mode = "supplementary-only" if args.supplementary_only else depth
    node_keys = ", ".join(n["key"] for n in tree_nodes[:3])
    if len(tree_nodes) > 3:
        node_keys += f" +{len(tree_nodes) - 3} more"
    print(f"[retrieve] {args.category} ({mode}) -> {len(tree_nodes)} nodes [{node_keys}], "
          f"{len(reasoning_bank)} rb, {len(guardrails)} guards, {len(experiences)} exp",
          file=sys.stderr)

    result = {
        "meta": {
            "category": args.category,
            "depth": depth,
            "timestamp": now_str(),
            "retrieval_channels": sorted(retrieval_channels),
            "items_returned": {
                "tree_nodes": len(tree_nodes),
                "reasoning_bank": len(reasoning_bank),
                "guardrails": len(guardrails),
                "pattern_signatures": len(pattern_signatures),
                "experiences": len(experiences),
                "beliefs": len(beliefs),
            },
        },
        "tree_nodes": tree_nodes,
        "reasoning_bank": reasoning_bank,
        "guardrails": guardrails,
        "pattern_signatures": pattern_signatures,
        "experiences": experiences,
        "beliefs": beliefs,
        "experiential_index": experiential_index,
    }

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
