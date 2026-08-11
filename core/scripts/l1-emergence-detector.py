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
import statistics
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


def _retrieval_per_node(bucket):
    """Read-side density for one L1: retrievals per node (0.0 when massless).

    Normalizing by node count is what makes cross-L1 comparison possible —
    RAW retrieval totals track mass, so a big L1 always "wins" and the
    comparison says nothing about consultation.
    """
    nodes = bucket.get("total_nodes", 0) or 0
    if nodes <= 0:
        return 0.0
    return (bucket.get("total_retrieval_count", 0) or 0) / nodes


def detect_s7_reparent_signal(picks, stats, imbalance_threshold=2.5):
    """S7: detect L1s whose pick-rate share diverges from mass share.

    pick_share = (picks landing in L1 X) / (total picks)
    mass_share = (nodes in L1 X) / (total nodes)
    imbalance = pick_share / mass_share

    imbalance >> 1 → L1 is growing faster than its current mass (hot)
    imbalance << 1 → L1 receives few new picks relative to its mass

    READ-SIDE AWARENESS (g-115-3214). Low write activity alone CANNOT
    distinguish a dying L1 from a stable reference one — both receive zero
    new picks. So the low-write branch is discriminated by per-node
    retrieval against the cross-L1 median:

      writes low + retrieval below median  → `stagnating`       (candidate
                                              for retirement or merge)
      writes low + retrieval at/above median → `stable-reference` (healthy:
                                              consulted often, simply done
                                              growing — do NOT retire)

    This branch previously carried an `imbalance > 0` guard, which silently
    DROPPED every zero-pick L1 — the exact population the signal is about.
    Removing that guard without the read-side test would have been the
    opposite error: measured live 2026-08-02, `performance` has 0 picks and
    78.9 retrievals/node, the HIGHEST density in the tree (median 74.7), so
    a write-side-only verdict would have labelled the best-consulted L1
    dying. Sibling reasoning: guard-731 (retrieval_count==0 is ONE signal,
    never a retirement verdict on its own).

    When NO L1 has any retrieval data, the read-side instrument is blind.
    Per guard-1974, total absence of evidence must never render as the
    healthy verdict, so those findings stay `stagnating` (the pre-existing
    write-side-only behavior) and carry `read_side: "unavailable"` to name
    the blindness rather than let it read as an all-clear.

    Returns a verdict dict with `status` in
    {no_data, no_tree, data_sparse, balanced, imbalanced} plus `findings`,
    `total_picks`, `threshold`. The renderer reads `status` to distinguish
    "wait for more data" (data_sparse) from "all clear" (balanced) — they
    look identical in `findings`=[] otherwise. NOTE `status: imbalanced`
    describes the NUMERIC condition, not a problem: a findings list holding
    only `stable-reference` entries is a healthy tree. Read `signal`.
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

    # Read-side signal, computed once across all L1s so each low-write L1 is
    # judged against its peers rather than an absolute cutoff. Massless L1s
    # are excluded from the BASIS as well as from findings (the loop below
    # skips them): a 0-node L1 has a meaningless 0.0 density, and leaving it
    # in would drag the median down and make `stable-reference` easier to
    # reach for every real L1. Not reachable today — an L1 bucket always
    # counts the L1 node itself — but the two populations must agree or the
    # comparison silently stops meaning what it says.
    density = {l1: _retrieval_per_node(b) for l1, b in real_l1s.items()
               if (b.get("total_nodes", 0) or 0) > 0}
    read_side_available = any(v > 0 for v in density.values())
    median_density = statistics.median(density.values()) if density else 0.0

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
        elif imbalance <= (1.0 / imbalance_threshold):
            # NOTE: no `imbalance > 0` guard — a zero-pick L1 (imbalance 0.0)
            # is precisely the case this branch exists to judge ().
            #
            # The healthy verdict is gated on THIS L1's OWN read-side
            # evidence, never on the median comparison alone. guard-963's
            # partial-coverage corollary: implementing the all-unmeasured
            # branch is the EASY half — some-measured-some-not must ALSO be
            # non-clean. A global "any L1 has data" rail leaves `0.0 >= 0.0`
            # true whenever the median collapses to zero (which it does as
            # soon as >=50% of L1s are unconsulted), handing every
            # zero-retrieval L1 the `stable-reference` verdict off a zero
            # basis. Measured pre-fix 2026-08-02: busy=(100 nodes, 5000
            # retr), quiet=(20, 0), mid=(50, 0) -> `quiet` scored
            # stable-reference with read_side "measured", which was also a
            # false claim about that L1. Found by the guard-343 fresh-eyes
            # gate in the same iteration that introduced it.
            #
            # `read_side_available` is now MESSAGE-SELECTION ONLY — it
            # distinguishes "the whole instrument is blind" from "this L1
            # alone is unconsulted" (different remedies: fix retrieval
            # logging vs. consider retiring the L1). It does NOT gate the
            # verdict; the per-L1 test below does. (guard-2240 clause 2 —
            # do not leave a condition that merely LOOKS load-bearing.)
            if density[l1] <= 0:
                signal = "stagnating"
                read_side = "unavailable"
                scope = ("no L1 has retrieval data, so the whole read-side "
                         "instrument is blind"
                         if not read_side_available else
                         "this L1 has no retrieval data, though peers do")
                interpretation = (
                    "receives far fewer new picks than its mass would "
                    "predict, and its read-side density is UNAVAILABLE "
                    "({}) — a write-side-only verdict, so corroborate "
                    "before retiring or merging (guard-731).".format(scope))
            elif density[l1] >= median_density:
                signal = "stable-reference"
                read_side = "measured"
                interpretation = (
                    "receives few new picks but is consulted at or above "
                    "the median rate ({:.1f} vs {:.1f} retrievals/node) — "
                    "a stable reference area, NOT a retirement candidate. "
                    "Done growing is not dying.".format(
                        density[l1], median_density))
            else:
                signal = "stagnating"
                read_side = "measured"
                interpretation = (
                    "receives far fewer new picks than its mass would "
                    "predict AND is consulted below the median rate "
                    "({:.1f} vs {:.1f} retrievals/node) — low on both the "
                    "write and read side, so a genuine candidate for "
                    "retirement or merge into a sibling L1.".format(
                        density[l1], median_density))
            findings.append({
                "l1": l1, "signal": signal,
                "pick_share": round(pick_share, 3),
                "mass_share": round(mass_share, 3),
                "imbalance": round(imbalance, 2),
                "read_side": read_side,
                "retrieval_per_node": round(density[l1], 1),
                "median_retrieval_per_node": round(median_density, 1),
                "interpretation": interpretation,
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
        lines.append("| L1 | Signal | Pick share | Mass share | Imbalance "
                     "| Retr/node | Median |")
        lines.append("|---|---|---:|---:|---:|---:|---:|")
        for f in s7["findings"]:
            # Read-side columns exist only on low-write findings; a `hot` L1
            # is judged on write share alone, so render "—" rather than
            # invent a number the verdict never consulted.
            if "retrieval_per_node" in f:
                rpn = "{:.1f}".format(f["retrieval_per_node"])
                med = "{:.1f}".format(f["median_retrieval_per_node"])
                if f.get("read_side") == "unavailable":
                    rpn = med = "n/a"
            else:
                rpn = med = "—"
            lines.append("| {} | {} | {:.1%} | {:.1%} | {:.1f}× | {} | {} |".format(
                f["l1"], f["signal"], f["pick_share"],
                f["mass_share"], f["imbalance"], rpn, med))
        if any(f.get("read_side") == "unavailable" for f in s7["findings"]):
            lines.append("")
            lines.append("_Read-side density UNAVAILABLE (no L1 carries "
                         "retrieval data) — `stagnating` above is a "
                         "write-side-only verdict, not a retirement "
                         "recommendation (guard-1974, guard-731)._")
        if any(f["signal"] == "stable-reference" for f in s7["findings"]):
            lines.append("")
            lines.append("_`stable-reference` = few new picks but consulted "
                         "at/above the median rate. Healthy — do NOT retire "
                         "or merge on the imbalance number alone._")
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
