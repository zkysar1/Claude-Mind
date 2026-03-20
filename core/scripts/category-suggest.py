#!/usr/bin/env python3
"""Category suggestion engine — maps free text to tree node keys.

Given a text string (goal title, description, etc.), returns the best-matching
tree node key(s) by scoring against node keys, summaries, and .md front matter
entities.

Uses tree_match.py for shared matching primitives (front matter parsing,
concept index building). Scoring weights here are tuned for category
assignment, NOT retrieval — do not unify with tree_match scoring.

Usage:
    category-suggest.sh --text "Fix authentication retry logic" [--top 3]

Output: JSON array of matches sorted by score descending:
    [{"key": "api-auth", "score": 4.2, "summary": "..."}]
"""

import argparse
import json
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

# Shared matching primitives — single source of truth for front matter
# parsing and concept index building. See tree_match.py.
from tree_match import build_concept_index  # noqa: E402
from _paths import MIND_DIR

TREE_PATH = MIND_DIR / "knowledge" / "tree" / "_tree.yaml"

# Structural nodes excluded from category suggestions (too broad)
STRUCTURAL_DEPTHS = {0, 1}


def tokenize(text):
    """Split text into lowercase tokens, strip punctuation, min 3 chars."""
    words = re.findall(r'[a-zA-Z0-9]+', text.lower())
    return {w for w in words if len(w) >= 3}


# Category-assignment scoring weights — intentionally different from
# tree_match channel-based scoring. These are tuned for "which category
# tag best fits this goal title?" not "which node has the most relevant
# knowledge for retrieval?"
def score_node(key, node, text_tokens, text_lower, concept_index):
    """Score a single tree node against input text. Higher = better match."""
    score = 0.0
    key_lower = key.lower()

    # 1. Exact key substring in text (+3)
    if key_lower in text_lower:
        score += 3.0

    # 2. Word overlap: key segments vs text tokens (+1 per match)
    key_segments = {w for w in key_lower.split("-") if len(w) >= 3}
    key_overlap = key_segments & text_tokens
    score += len(key_overlap) * 1.0

    # 3. Summary word overlap (+0.5 per match, capped at 3)
    summary = str(node.get("summary", "")).lower()
    summary_tokens = tokenize(summary)
    summary_overlap = summary_tokens & text_tokens
    score += min(len(summary_overlap) * 0.5, 3.0)

    # 4. Front-matter entity overlap (+1.5 per match)
    for term, node_keys in concept_index.items():
        if key not in node_keys:
            continue
        if term in text_tokens:
            score += 1.5
        elif any(term in t or t in term for t in text_tokens if len(t) >= 3):
            score += 0.75

    return score


def suggest(text, top_n=3):
    """Return top-N tree node key matches for the given text."""
    if not TREE_PATH.exists():
        return []

    with open(TREE_PATH, "r", encoding="utf-8") as f:
        tree = yaml.safe_load(f)
    if not isinstance(tree, dict):
        return []
    nodes = tree.get("nodes", {})
    if not nodes:
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
    return results[:top_n]


def main():
    parser = argparse.ArgumentParser(
        description="Suggest tree node categories for free text."
    )
    parser.add_argument("--text", required=True,
                        help="Free text to match against tree nodes")
    parser.add_argument("--top", type=int, default=3,
                        help="Number of top matches to return (default: 3)")
    args = parser.parse_args()

    matches = suggest(args.text, args.top)
    print(json.dumps(matches, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
