#!/usr/bin/env python3
# domain-leak-exempt: framework retrieval A/B harness; entity-ID prefixes
#   (rb-, guard-, node:) are framework record identifiers, not domain examples.
"""ppr-blend-ab-harness.py -- A/B compare baseline vs PPR-blended Mind retrieval.

g-306-44 (BRD Gap 1b+1c, child 3/4 of g-306-15). Runs the unified retrieval
ranking (retrieve.load_tree_nodes -> _score_weight_limit) for one or more query
categories TWICE -- once with the PPR blend OFF (token-overlap only, the
production default) and once with it ON (token-overlap + Personalized PageRank
seeded from the top baseline matches) -- and reports how the ranking changed.

The blend is boost-only (see retrieve._ppr_weight), so a node can only move UP
when PPR-proximate to the recognized query entities; nothing is demoted in
absolute score. What the A/B shows is the RE-ORDERING: records reachable in 1-2
hops from the seeds (sharing a category/tag hub, or cross-referencing a top
match) surfacing above lexically-unrelated records a pure-lexical match ranks
the same. This is the operational evidence g-306-45 validates.

The harness mutates only the in-process retrieval config cache
(retrieve._RETRIEVAL_CFG_CACHE) to toggle the flag; it runs read_only=True so no
retrieval counters are bumped. It does NOT change the on-disk default (which
stays OFF). Reversible + bounded (self.md).

Usage:
  ppr-blend-ab-harness.py --category "framework-architecture"
  ppr-blend-ab-harness.py --category "cat-a" --category "cat-b" --top-k 15
  ppr-blend-ab-harness.py --category "framework-patterns" --depth deep --output json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import retrieve  # noqa: E402  (import-time: sets WORLD_DIR/AGENT_DIR module globals)


def _node_score(node):
    """Best-effort score field from a load_tree_nodes() node dict."""
    for k in ("match_score", "score", "effective_score"):
        v = node.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _rank(category, depth, ppr_on):
    """Run load_tree_nodes for one category with ppr_blend toggled; return the
    ordered list of (key, score). Toggles ONLY ppr_blend_enabled on top of the
    real merged config, so every other knob matches production."""
    # Rebuild the real merged config, then overlay just the PPR flag.
    retrieve._RETRIEVAL_CFG_CACHE = None
    merged = dict(retrieve._load_retrieval_config())
    merged["ppr_blend_enabled"] = bool(ppr_on)
    retrieve._RETRIEVAL_CFG_CACHE = merged
    try:
        nodes, _channels = retrieve.load_tree_nodes([category], depth,
                                                    read_only=True)
    finally:
        retrieve._RETRIEVAL_CFG_CACHE = None  # leave no residue
    return [(n.get("key", ""), _node_score(n)) for n in nodes if n.get("key")]


def _compare(category, depth, top_k):
    baseline = _rank(category, depth, ppr_on=False)
    blended = _rank(category, depth, ppr_on=True)

    base_order = [k for k, _ in baseline]
    blend_order = [k for k, _ in blended]
    base_pos = {k: i for i, k in enumerate(base_order)}
    blend_pos = {k: i for i, k in enumerate(blend_order)}

    # Promotions: keys whose rank improved (smaller index) under the blend.
    promotions = []
    for k in blend_order:
        if k in base_pos and blend_pos[k] < base_pos[k]:
            promotions.append({
                "key": k,
                "baseline_rank": base_pos[k] + 1,
                "blended_rank": blend_pos[k] + 1,
                "delta": base_pos[k] - blend_pos[k],
            })
    promotions.sort(key=lambda p: -p["delta"])

    # New-in-top-K: keys the blend surfaced into the top-K that baseline did not.
    base_topk = set(base_order[:top_k])
    blend_topk = set(blend_order[:top_k])
    new_in_topk = sorted(blend_topk - base_topk)
    dropped_from_topk = sorted(base_topk - blend_topk)
    jaccard = (len(base_topk & blend_topk) / len(base_topk | blend_topk)
               if (base_topk | blend_topk) else 1.0)

    return {
        "category": category,
        "depth": depth,
        "baseline_count": len(baseline),
        "blended_count": len(blended),
        "reordered": sum(1 for k in blend_order
                         if k in base_pos and blend_pos[k] != base_pos[k]),
        "promotions": promotions,
        "top_k": top_k,
        "topk_jaccard": round(jaccard, 4),
        "new_in_topk": new_in_topk,
        "dropped_from_topk": dropped_from_topk,
        "baseline_topk": base_order[:top_k],
        "blended_topk": blend_order[:top_k],
    }


def _print_text(rep):
    print(f"=== A/B retrieval: category={rep['category']!r} depth={rep['depth']} "
          f"(baseline {rep['baseline_count']} nodes / blended {rep['blended_count']}) ===")
    print(f"  top-{rep['top_k']} Jaccard(baseline,blended) = {rep['topk_jaccard']} "
          f"| {rep['reordered']} node(s) reordered")
    if rep["new_in_topk"]:
        print(f"  PPR surfaced into top-{rep['top_k']}: {rep['new_in_topk']}")
    if rep["dropped_from_topk"]:
        print(f"  pushed out of top-{rep['top_k']}: {rep['dropped_from_topk']}")
    if rep["promotions"]:
        print("  promotions (rank improved by PPR proximity):")
        for p in rep["promotions"][:10]:
            print(f"    {p['key']}: #{p['baseline_rank']} -> #{p['blended_rank']} "
                  f"(+{p['delta']})")
    else:
        print("  no rank changes (PPR found no graph-proximate boost for these seeds)")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="A/B compare baseline vs PPR-blended Mind retrieval (g-306-44).")
    ap.add_argument("--category", action="append", required=True,
                    help="Query category (repeatable for multiple A/B runs).")
    ap.add_argument("--depth", choices=("shallow", "medium", "deep"),
                    default="deep")
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--output", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)

    reports = [_compare(c, args.depth, args.top_k) for c in args.category]

    if args.output == "json":
        print(json.dumps({"reports": reports}, ensure_ascii=True, indent=2))
    else:
        for rep in reports:
            _print_text(rep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
