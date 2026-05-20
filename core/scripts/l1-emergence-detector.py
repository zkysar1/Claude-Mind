#!/usr/bin/env python3
"""L1-emergence detector (S4 + S6 + S7 — Phase 4).

Reads `meta/l1-pick-log.jsonl` + `world/knowledge/tree/_tree.yaml` and
emits candidate signals for the /fresh-eyes-tree briefing.

Three analysis passes, each produces a list of candidates:

  S4 — New-L1 emergence detection
    Look for a topic cluster repeatedly added under one L1 whose siblings
    don't overlap with the new cluster. Indicator: ≥6 recent SPROUTs under
    the same parent within the last 200 picks, where the new children's
    summaries share distinctive tokens not present in the rest of the L1.
    Signal: "consider new L1 around topic X."

  S6 — Cross-domain leak detection
    Look for new nodes whose summary tokens overlap more strongly with a
    different L1's summary than with the L1 they were placed under.
    Signal: "node Y placed under L1 A may belong under L1 B."

  S7 — REPARENT auto-nomination
    Look for L1s whose recent pick-rate (last 200 picks) skews far away
    from their structural mass share. Two failure modes:
      a. L1 X is 70% of mass but receives 5% of picks → dying
      b. L1 Y is 5% of mass but receives 40% of picks → boundary wrong
    Signal: "pick-rate vs mass-share imbalance for L1 Z."

Output: JSON. Optional --markdown for human review. Designed to be called
from /fresh-eyes-tree Phase 2.4 (briefing assembly), the daily memory-
curation flow, or manually for diagnosis. Fail-open everywhere.

Usage:
    py -3 core/scripts/l1-emergence-detector.py                  # JSON
    py -3 core/scripts/l1-emergence-detector.py --markdown       # readable
    py -3 core/scripts/l1-emergence-detector.py --window 500     # last 500 picks
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

from _paths import META_DIR
# Single source of truth — DO NOT inline copies of these helpers.
# tree.py owns the algorithms; this detector consumes them in-process
# (no subprocess to tree.py, no parallel YAML parser).
from tree import _get_l1_for_node, safe_read_tree, compute_stats

PICK_LOG = Path(META_DIR) / "l1-pick-log.jsonl"

# Token extraction: keep alphanum + dashes, lowercase, drop short tokens
# and common stopwords that add noise to overlap scoring.
TOKEN_RE = re.compile(r"[a-z][a-z0-9-]{2,}")
STOPWORDS = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "into", "are",
    "was", "what", "when", "where", "which", "who", "how", "via", "per",
    "have", "has", "had", "but", "not", "all", "any", "more", "most",
    "node", "tree", "test", "tests", "data", "file", "path", "code",
})


def _tokenize(text):
    if not text:
        return set()
    tokens = set()
    for m in TOKEN_RE.finditer(text.lower()):
        t = m.group(0)
        if t not in STOPWORDS:
            tokens.add(t)
    return tokens


def _load_picks(window):
    """Last `window` entries from the L1 pick log."""
    if not PICK_LOG.exists():
        return []
    try:
        lines = PICK_LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries = []
    for line in lines[-window:]:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def detect_s4_new_l1_emergence(picks, tree, min_cluster_size=6, distinctive_overlap_floor=0.4):
    """S4: detect repeated SPROUTs forming a coherent new cluster.

    Find parents whose recent add-children form a tight token cluster AND
    that cluster diverges from the rest of the L1's vocabulary.

    Returns list of candidates:
      {parent_key, l1, cluster_size, distinctive_tokens, child_keys, signal_strength}
    """
    nodes = (tree.get("nodes") or {}) if tree else {}
    if not nodes:
        return []
    # Group recent add-child / sprout picks by parent.
    by_parent = defaultdict(list)
    for p in picks:
        if p.get("decision_type") not in ("add-child", "batch-add-child"):
            continue
        target = p.get("target_node")
        if not target or target not in nodes:
            continue
        parent_key = nodes[target].get("parent")
        if not parent_key or parent_key == "root":
            continue
        by_parent[parent_key].append(target)

    candidates = []
    for parent_key, child_keys in by_parent.items():
        if len(child_keys) < min_cluster_size:
            continue
        # Tokens of the new children (from their summaries)
        new_tokens = Counter()
        for ck in child_keys:
            new_tokens.update(_tokenize(nodes.get(ck, {}).get("summary", "")))
            new_tokens.update(_tokenize(ck.replace("-", " ")))
        if not new_tokens:
            continue
        # Tokens of OTHER children of this parent (NOT in the recent cluster)
        parent_node = nodes.get(parent_key, {})
        all_children = parent_node.get("children", []) or []
        other_children = [c for c in all_children if c not in set(child_keys)]
        other_tokens = Counter()
        for oc in other_children:
            other_tokens.update(_tokenize(nodes.get(oc, {}).get("summary", "")))
            other_tokens.update(_tokenize(oc.replace("-", " ")))
        # Distinctive tokens: high frequency in new cluster, absent in others
        distinctive = [
            (t, c) for t, c in new_tokens.items()
            if c >= max(2, len(child_keys) // 3) and other_tokens.get(t, 0) == 0
        ]
        if not distinctive:
            continue
        # Signal strength: fraction of new cluster's mass concentrated in
        # distinctive tokens.
        new_mass = sum(new_tokens.values())
        distinct_mass = sum(c for _, c in distinctive)
        strength = distinct_mass / new_mass if new_mass else 0
        if strength < distinctive_overlap_floor:
            continue
        l1 = _get_l1_for_node(nodes, parent_key) or "_orphan"
        candidates.append({
            "parent_key": parent_key,
            "l1": l1,
            "cluster_size": len(child_keys),
            "distinctive_tokens": [t for t, _ in sorted(distinctive, key=lambda x: -x[1])[:8]],
            "child_keys": sorted(child_keys)[:10],
            "signal_strength": round(strength, 3),
        })
    return sorted(candidates, key=lambda c: -c["signal_strength"])


def detect_s6_cross_domain_leak(picks, tree, stats, min_better_l1_overlap=0.3, min_lead=0.10):
    """S6: detect new nodes whose summary aligns more with a different L1.

    For each recent SPROUT, tokenize its summary, score overlap against
    each L1's summary, and flag when the assigned-L1 overlap is lower than
    a different-L1 overlap by at least `min_lead`.
    """
    nodes = (tree.get("nodes") or {}) if tree else {}
    if not nodes or not stats:
        return []
    by_l1 = (stats.get("by_l1") or {})
    # Get each L1's summary tokens (from tree.yaml — l1_domains list)
    # The tree.yaml lookup is via _tree.yaml.nodes[<l1>].summary
    l1_summary_tokens = {}
    for l1_key in by_l1.keys():
        if l1_key == "_orphan":
            continue
        l1_node = nodes.get(l1_key, {})
        l1_summary_tokens[l1_key] = _tokenize(l1_node.get("summary", ""))

    candidates = []
    seen_targets = set()
    for p in picks:
        target = p.get("target_node")
        if not target or target in seen_targets:
            continue
        seen_targets.add(target)
        node = nodes.get(target)
        if not node:
            continue
        # Use CURRENT L1 from the live tree, NOT the log's frozen `l1` field.
        # If the node was REPARENT'd since its SPROUT, the log still carries
        # the old L1 — comparing against old-L1 tokens flags the node as a
        # "leak" even though the reparent already moved it. Always read live.
        l1_assigned = _get_l1_for_node(nodes, target)
        if not l1_assigned or l1_assigned == "_orphan":
            continue
        node_tokens = _tokenize(node.get("summary", ""))
        node_tokens.update(_tokenize(target.replace("/", " ").replace("-", " ")))
        if not node_tokens:
            continue
        assigned_tokens = l1_summary_tokens.get(l1_assigned, set())
        assigned_overlap = (len(node_tokens & assigned_tokens) / max(len(node_tokens), 1)
                            if assigned_tokens else 0)
        # Score against all other L1s
        best_other = None
        best_other_overlap = 0
        for other_l1, other_tokens in l1_summary_tokens.items():
            if other_l1 == l1_assigned:
                continue
            ov = (len(node_tokens & other_tokens) / max(len(node_tokens), 1)
                  if other_tokens else 0)
            if ov > best_other_overlap:
                best_other = other_l1
                best_other_overlap = ov
        if best_other is None:
            continue
        if best_other_overlap >= min_better_l1_overlap and \
                (best_other_overlap - assigned_overlap) >= min_lead:
            candidates.append({
                "target_node": target,
                "current_l1": l1_assigned,
                "suggested_l1": best_other,
                "current_overlap": round(assigned_overlap, 3),
                "suggested_overlap": round(best_other_overlap, 3),
                "summary": node.get("summary", "")[:120],
            })
    return sorted(candidates, key=lambda c: -(c["suggested_overlap"] - c["current_overlap"]))[:20]


_S7_MIN_PICKS = 10  # Min picks needed before pick/mass ratios are meaningful.


def detect_s7_reparent_signal(picks, stats, imbalance_threshold=2.5):
    """S7: detect L1s whose pick-rate share diverges from mass share.

    pick_share = (picks landing in L1 X) / (total picks)
    mass_share = (nodes in L1 X) / (total nodes)
    imbalance = pick_share / mass_share

    imbalance >> 1 → L1 is growing faster than its current mass (hot)
    imbalance << 1 → L1 is stagnating relative to its mass (dying)

    Returns a verdict dict with `status` in
    {no_data, no_tree, data_sparse, balanced, imbalanced} plus `findings`,
    `total_picks`, `threshold`. The renderer reads `status` to distinguish
    "wait for more data" (data_sparse) from "all clear" (balanced) — they
    look identical in `findings`=[] otherwise.
    """
    base = {"status": "no_data", "findings": [], "total_picks": 0,
            "threshold": imbalance_threshold}
    if not stats or not picks:
        return base
    by_l1 = (stats.get("by_l1") or {})
    real_l1s = {k: v for k, v in by_l1.items() if k != "_orphan"}
    if not real_l1s or sum(b.get("total_nodes", 0) for b in real_l1s.values()) == 0:
        return {**base, "status": "no_tree"}
    total_nodes = sum(b.get("total_nodes", 0) for b in real_l1s.values())
    pick_counts = Counter(p.get("l1") for p in picks if p.get("l1") in real_l1s)
    total_picks = sum(pick_counts.values())
    if total_picks < _S7_MIN_PICKS:
        return {**base, "status": "data_sparse", "total_picks": total_picks}

    findings = []
    for l1, bucket in real_l1s.items():
        mass_share = bucket.get("total_nodes", 0) / total_nodes
        pick_share = pick_counts.get(l1, 0) / total_picks
        if mass_share == 0:
            continue
        imbalance = pick_share / mass_share
        if imbalance >= imbalance_threshold:
            findings.append({
                "l1": l1, "signal": "hot",
                "pick_share": round(pick_share, 3),
                "mass_share": round(mass_share, 3),
                "imbalance": round(imbalance, 2),
                "interpretation": (
                    "growing faster than current mass — possible boundary "
                    "drift; review whether the new picks fit the existing "
                    "L1 framing or signal a new sub-domain emerging."),
            })
        elif imbalance > 0 and imbalance <= (1.0 / imbalance_threshold):
            findings.append({
                "l1": l1, "signal": "stagnating",
                "pick_share": round(pick_share, 3),
                "mass_share": round(mass_share, 3),
                "imbalance": round(imbalance, 2),
                "interpretation": (
                    "receives far fewer new picks than its mass would "
                    "predict — possible candidate for retirement or merge "
                    "into a sibling L1."),
            })
    return {
        "status": "imbalanced" if findings else "balanced",
        "findings": sorted(findings, key=lambda f: f["imbalance"], reverse=True),
        "total_picks": total_picks,
        "threshold": imbalance_threshold,
    }


def render_markdown(report):
    lines = []
    lines.append("## L1 emergence detector — {}".format(report["ts"]))
    lines.append("Window: last {} picks ({} total in log).".format(
        report["window"], report["total_in_log"]))
    lines.append("")
    lines.append("### S4 — New-L1 emergence candidates")
    if report["s4_new_l1_candidates"]:
        for c in report["s4_new_l1_candidates"]:
            lines.append("- **{}** under L1 `{}` — {} children, "
                         "strength {:.0%}. Distinctive: {}".format(
                c["parent_key"], c["l1"], c["cluster_size"],
                c["signal_strength"],
                ", ".join(c["distinctive_tokens"][:5])))
    else:
        lines.append("_none_")
    lines.append("")
    lines.append("### S6 — Cross-domain leak candidates")
    if report["s6_cross_domain_leaks"]:
        lines.append("| Node | Current L1 | Suggested L1 | Lead |")
        lines.append("|---|---|---|---:|")
        for c in report["s6_cross_domain_leaks"]:
            lead = c["suggested_overlap"] - c["current_overlap"]
            lines.append("| `{}` | {} | {} | {:.0%} |".format(
                c["target_node"], c["current_l1"], c["suggested_l1"], lead))
    else:
        lines.append("_none_")
    lines.append("")
    lines.append("### S7 — Pick-rate vs mass-share imbalance")
    s7 = report["s7_reparent_signals"]
    if s7["findings"]:
        lines.append("| L1 | Signal | Pick share | Mass share | Imbalance |")
        lines.append("|---|---|---:|---:|---:|")
        for f in s7["findings"]:
            lines.append("| {} | {} | {:.1%} | {:.1%} | {:.1f}× |".format(
                f["l1"], f["signal"], f["pick_share"],
                f["mass_share"], f["imbalance"]))
    elif s7["status"] == "data_sparse":
        lines.append("_data_sparse — pick log has {} entries; need ≥{} for signal._".format(
            s7["total_picks"], _S7_MIN_PICKS))
    elif s7["status"] in ("no_data", "no_tree"):
        lines.append("_{} — detector inputs unavailable._".format(s7["status"]))
    else:
        lines.append("_balanced — all L1s within imbalance threshold ({}×)._".format(
            s7["threshold"]))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--window", type=int, default=200,
                    help="How many recent pick-log entries to analyze (default 200)")
    ap.add_argument("--markdown", action="store_true",
                    help="Output human-readable markdown")
    ap.add_argument("--s4-min-cluster", type=int, default=6,
                    help="S4: min cluster size to surface a new-L1 candidate (default 6)")
    ap.add_argument("--s7-imbalance", type=float, default=2.5,
                    help="S7: pick/mass imbalance threshold (default 2.5)")
    args = ap.parse_args()

    picks = _load_picks(args.window)
    tree = safe_read_tree() or {}
    stats = compute_stats(tree, by_l1=True) if tree else None

    s4 = detect_s4_new_l1_emergence(picks, tree, min_cluster_size=args.s4_min_cluster)
    s6 = detect_s6_cross_domain_leak(picks, tree, stats)
    s7 = detect_s7_reparent_signal(picks, stats, imbalance_threshold=args.s7_imbalance)

    # Count total entries in log for context (separate from windowed picks).
    total_in_log = 0
    if PICK_LOG and PICK_LOG.exists():
        try:
            total_in_log = sum(1 for _ in PICK_LOG.read_text(encoding="utf-8").splitlines() if _.strip())
        except OSError:
            pass

    report = {
        "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "window": min(args.window, total_in_log),
        "total_in_log": total_in_log,
        "s4_new_l1_candidates": s4,
        "s6_cross_domain_leaks": s6,
        "s7_reparent_signals": s7,
    }

    if args.markdown:
        print(render_markdown(report))
    else:
        print(json.dumps(report, indent=2, ensure_ascii=False))

    sys.exit(0)


if __name__ == "__main__":
    main()
