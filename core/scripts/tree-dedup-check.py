#!/usr/bin/env python3
"""Pre-create duplicate gate for the knowledge tree.

Given a proposed new child node (parent, key, summary), decide whether it
should be created or whether an existing sibling already covers the same ground.

Called before every tree write path (tree add, state-update Step 8 encoding,
reflect-tree-update, research-topic sprout, /tree maintain SPROUT) to stop the
"breadth without depth" accumulation.

Pure read-only. Never writes _tree.yaml.

Exit codes:
  0  accept    — no conflict, safe to create
  2  reject    — sibling with exact key already exists under this parent
  3  reject    — sibling with high summary overlap already exists (update-in-place suggested)
  4  error     — tree unreadable, parent missing, or invalid arguments

Thresholds read from core/config/tree.yaml under the `dedup:` section:
  sibling_overlap_threshold  (default 0.6)  Jaccard threshold over distinctive tokens
  min_tokens_for_overlap     (default 3)    skip check on trivially short summaries
  enforce_from_depth         (default 2)    don't block L1/L2 growth
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
    sys.exit(4)

from _paths import WORLD_DIR, CONFIG_DIR

TREE_PATH = WORLD_DIR / "knowledge" / "tree" / "_tree.yaml"
TREE_CONFIG = CONFIG_DIR / "tree.yaml"

DEFAULT_CONFIG = {
    "sibling_overlap_threshold": 0.6,
    "min_tokens_for_overlap": 3,
    "enforce_from_depth": 2,
}

STOPWORDS = frozenset([
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "has", "have", "in", "is", "it", "its", "of", "on", "or", "that",
    "the", "this", "to", "was", "were", "will", "with", "but", "not",
    "been", "being", "via", "over", "into", "than", "then",
])


def _load_dedup_config():
    try:
        with open(TREE_CONFIG, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        merged = dict(DEFAULT_CONFIG)
        merged.update(cfg.get("dedup", {}) or {})
        return merged
    except (OSError, yaml.YAMLError):
        return dict(DEFAULT_CONFIG)


def _load_tree():
    if not TREE_PATH.exists():
        return None
    with open(TREE_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "nodes" not in data:
        return None
    return data


def _tokenize(text: str) -> set:
    """Domain-agnostic tokenizer: lowercase word-chars, drop stopwords and <3 chars."""
    if not text:
        return set()
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return {t for t in tokens if len(t) >= 3 and t not in STOPWORDS}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def check_dedup(parent_key: str, proposed_key: str, proposed_summary: str, tree: dict = None) -> dict:
    """Core dedup decision. Returns dict with at minimum `action` and `exit_code`.

    If `tree` is provided (pre-loaded dict from _tree.yaml), uses it directly —
    avoids a re-read and ensures the caller's transactional view is honored.
    """
    if tree is None:
        tree = _load_tree()
    if tree is None:
        return {
            "action": "error",
            "reason": "tree_unreadable",
            "exit_code": 4,
            "message": f"Cannot read tree at {TREE_PATH}",
        }

    nodes = tree.get("nodes", {})
    parent = nodes.get(parent_key)
    if parent is None:
        return {
            "action": "error",
            "reason": "parent_not_found",
            "exit_code": 4,
            "message": f"Parent '{parent_key}' not found in tree",
        }

    cfg = _load_dedup_config()
    parent_depth = parent.get("depth", 0)
    proposed_depth = parent_depth + 1

    # Depth gate: don't block L1/L2 growth
    if proposed_depth < cfg["enforce_from_depth"]:
        return {
            "action": "accept",
            "reason": "below_enforce_depth",
            "exit_code": 0,
            "proposed_depth": proposed_depth,
            "enforce_from_depth": cfg["enforce_from_depth"],
        }

    proposed_key_lower = proposed_key.lower()
    children = parent.get("children", []) or []

    # Exact key match (case-insensitive) = hard reject
    for sibling_key in children:
        if sibling_key.lower() == proposed_key_lower:
            return {
                "action": "reject",
                "reason": "exact_key_match",
                "exit_code": 2,
                "sibling_key": sibling_key,
                "suggestion": "update_existing",
            }

    # Summary overlap check
    proposed_tokens = _tokenize(proposed_summary)
    if len(proposed_tokens) < cfg["min_tokens_for_overlap"]:
        return {
            "action": "accept",
            "reason": "summary_too_short",
            "exit_code": 0,
            "proposed_token_count": len(proposed_tokens),
            "min_tokens_for_overlap": cfg["min_tokens_for_overlap"],
        }

    best_match = None
    best_overlap = 0.0
    threshold = cfg["sibling_overlap_threshold"]

    for sibling_key in children:
        sibling = nodes.get(sibling_key)
        if not sibling:
            continue
        sibling_summary = sibling.get("summary") or ""
        sibling_tokens = _tokenize(sibling_summary)
        if len(sibling_tokens) < cfg["min_tokens_for_overlap"]:
            continue
        overlap = _jaccard(proposed_tokens, sibling_tokens)
        if overlap > best_overlap:
            best_overlap = overlap
            best_match = sibling_key

    if best_match is not None and best_overlap >= threshold:
        return {
            "action": "reject",
            "reason": "summary_overlap",
            "exit_code": 3,
            "sibling_key": best_match,
            "overlap": round(best_overlap, 4),
            "threshold": threshold,
            "suggestion": "update_existing",
        }

    return {
        "action": "accept",
        "reason": "no_conflict",
        "exit_code": 0,
        "best_sibling_overlap": round(best_overlap, 4) if best_match else 0.0,
        "best_sibling_key": best_match,
        "threshold": threshold,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Check whether a proposed new child conflicts with an existing sibling."
    )
    parser.add_argument("--parent", required=True, help="Parent node key")
    parser.add_argument("--key", required=True, help="Proposed child node key")
    parser.add_argument("--summary", required=True, help="Proposed child summary")
    parser.add_argument("--dry-run", action="store_true",
                        help="Always exit 0 but still print the decision (observation mode)")
    args = parser.parse_args()

    result = check_dedup(args.parent, args.key, args.summary)
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if args.dry_run:
        sys.exit(0)
    sys.exit(result["exit_code"])


if __name__ == "__main__":
    main()
