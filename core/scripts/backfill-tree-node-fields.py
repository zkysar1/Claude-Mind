#!/usr/bin/env python3
"""backfill-tree-node-fields.py — one-shot tool to fill in missing
`capability_level` and `last_updated` fields on tree nodes from existing
signals.

Background:
  Audit on 2026-05-09 found 66.2% of tree nodes missing `capability_level`
  and 74.2% missing `last_updated`. Root cause: only `cmd_set` and the
  manual SKILL.md ceremony (`tree-update.sh --set <key> last_updated <today>`)
  populated those fields; bulk creation paths and many `cmd_add_child`
  callers never set them. The 2026-05-10 prevention fix in `tree.py`:
    - apply_defaults now defaults missing capability_level to "EXPLORE"
      (read-side presentation; on-disk stays missing for legacy nodes)
    - cmd_add_child / batch add-child stamp last_updated=today on creation
    - cmd_set / batch set auto-bump last_updated on every mutation
  Going forward new and modified nodes get correct values. This script
  closes the historical gap.

Backfill rules (preserving signal where possible):

  capability_level (when missing):
    1. If node.confidence is numeric → run _graduate_node_level (existing
       logic) against competence_mapping thresholds
    2. Else, walk up parent chain — first ancestor with capability_level
       wins, child inherits it (matches _enforce_child_limit's existing
       parent-defaulting convention)
    3. Else default to "EXPLORE" (lowest, contributes 0 capability bonus)

  last_updated (when missing):
    1. If node.last_retrieved is set → use that (most-recent-touch proxy)
    2. Else use HISTORICAL_ANCHOR ("2026-01-01" — old enough that the
       recency scoring layer's exponential decay yields a near-zero bonus,
       so anchored nodes are presented as "old, no-boost" rather than
       getting a false-positive freshness signal).

DOES NOT backfill `confidence`. Confidence reflects measured competence;
defaulting it to 0.5 would propagate fake values up parent chains via
`_propagate_in_memory`. Missing confidence is structurally meaningful
(unmeasured) and stays missing.

Usage:
  py -3 core/scripts/backfill-tree-node-fields.py             # dry-run
  py -3 core/scripts/backfill-tree-node-fields.py --apply     # mutate
  py -3 core/scripts/backfill-tree-node-fields.py --field capability_level
  py -3 core/scripts/backfill-tree-node-fields.py --field last_updated
  py -3 core/scripts/backfill-tree-node-fields.py --apply --reason "..."
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import yaml  # noqa: E402

from _paths import WORLD_DIR  # noqa: E402

TREE_YAML = WORLD_DIR / "knowledge" / "tree" / "_tree.yaml"
# Anchor for nodes with no signal at all. Chosen old enough that the
# recency-decay scoring layer (RECENCY_TAU_DAYS=30) yields a near-zero
# bonus — i.e., anchored nodes are presented as "old, no-boost" rather
# than getting a false-positive freshness signal.
HISTORICAL_ANCHOR = "2026-01-01"


def _load_competence_thresholds():
    """Read competence_mapping from core/config/tree.yaml (under
    `domain_health.competence_mapping`, matching tree.py
    `_load_competence_config`'s key path)."""
    cfg_path = SCRIPT_DIR.parent / "config" / "tree.yaml"
    if not cfg_path.exists():
        return {}
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cm = (cfg.get("domain_health", {}) or {}).get("competence_mapping", {}) or {}
    return {k: v for k, v in cm.items() if isinstance(v, (int, float))}


def _graduate_from_confidence(confidence, thresholds):
    """Same logic as tree.py _graduate_node_level — pick the highest
    threshold-named level whose threshold confidence meets or exceeds.
    Returns None when no threshold matches (confidence below the lowest)."""
    if not isinstance(confidence, (int, float)):
        return None
    levels_sorted = sorted(thresholds.items(), key=lambda x: x[1])
    if not levels_sorted:
        return None
    new_level = "EXPLORE"
    for level_name, threshold in levels_sorted:
        if confidence >= threshold:
            new_level = level_name
    return new_level


def _walk_ancestor_capability(nodes, key, max_hops=20):
    """Walk up parent chain looking for first ancestor with capability_level.
    Returns the level string or None."""
    visited = set()
    current = nodes.get(key, {}).get("parent")
    hops = 0
    while current and hops < max_hops:
        if current in visited:
            return None
        visited.add(current)
        node = nodes.get(current)
        if not node:
            return None
        cl = node.get("capability_level")
        if cl:
            return cl
        current = node.get("parent")
        hops += 1
    return None


def _resolve_capability_level(nodes, key, node, thresholds):
    """Returns (new_level, source) where source is one of:
      'confidence' (derived from numeric confidence)
      'parent'     (inherited from ancestor)
      'default'    (EXPLORE fallback)
    """
    conf = node.get("confidence")
    derived = _graduate_from_confidence(conf, thresholds)
    if derived:
        return derived, "confidence"
    inherited = _walk_ancestor_capability(nodes, key)
    if inherited:
        return inherited, "parent"
    return "EXPLORE", "default"


def _resolve_last_updated(node):
    """Returns (date_string, source)."""
    lr = node.get("last_retrieved")
    if lr:
        return str(lr)[:10], "last_retrieved"
    return HISTORICAL_ANCHOR, "anchor"


def _summarize(report, out=sys.stdout):
    print("\n=== Tree node field backfill ===", file=out)
    print(f"  Tree path:       {TREE_YAML}", file=out)
    print(f"  Total nodes:     {report['total']}", file=out)
    print(f"  Reference date:  {report['today']}", file=out)
    print(f"  Anchor for missing-and-no-signal: {HISTORICAL_ANCHOR}", file=out)

    cl = report["capability_level"]
    print(f"\n  capability_level missing:  {cl['missing_before']}", file=out)
    print(f"    → resolved by source:", file=out)
    for src, n in sorted(cl["sources"].items(), key=lambda x: -x[1]):
        print(f"        {src:12s} {n}", file=out)
    if cl["sample"]:
        print(f"    Sample (first 5):", file=out)
        for k, level, src in cl["sample"][:5]:
            print(f"        {k[:50]:50s}  →  {level} ({src})", file=out)

    lu = report["last_updated"]
    print(f"\n  last_updated missing:      {lu['missing_before']}", file=out)
    print(f"    → resolved by source:", file=out)
    for src, n in sorted(lu["sources"].items(), key=lambda x: -x[1]):
        print(f"        {src:14s} {n}", file=out)
    if lu["sample"]:
        print(f"    Sample (first 5):", file=out)
        for k, val, src in lu["sample"][:5]:
            print(f"        {k[:50]:50s}  →  {val} ({src})", file=out)


def main():
    p = argparse.ArgumentParser(
        description="Backfill missing capability_level / last_updated fields.",
        epilog="Default is DRY-RUN. Pass --apply to mutate _tree.yaml.",
    )
    p.add_argument("--apply", action="store_true",
                   help="Actually mutate the YAML. Default: dry-run.")
    p.add_argument("--field", choices=["capability_level", "last_updated", "both"],
                   default="both",
                   help="Which field to backfill (default both).")
    p.add_argument("--reason", type=str,
                   default="audit-2026-05-10-backfill-missing-fields",
                   help="String written to backfill_reason field on each "
                        "modified node (audit trail).")
    args = p.parse_args()

    if not TREE_YAML.exists():
        print(f"ERROR: tree yaml not found at {TREE_YAML}", file=sys.stderr)
        return 1

    thresholds = _load_competence_thresholds()
    if args.field in ("capability_level", "both") and not thresholds:
        print("WARN: no competence_mapping found — capability_level will "
              "fall back to parent inheritance / EXPLORE only.",
              file=sys.stderr)

    from datetime import date

    if args.apply:
        from _fileops import locked_modify_yaml
        captured = {"report": None}

        def _modify(tree):
            tree = tree or {}
            nodes = tree.setdefault("nodes", {})
            captured["report"] = _backfill(nodes, args, thresholds)
            tree["last_updated"] = date.today().isoformat()
            return tree

        locked_modify_yaml(TREE_YAML, _modify)
        report = captured["report"]
        _summarize(report)
        print(f"\nAPPLIED: capability_level={report['capability_level']['changed']}, "
              f"last_updated={report['last_updated']['changed']} "
              f"node(s) modified.",
              file=sys.stderr)
    else:
        with open(TREE_YAML, "r", encoding="utf-8") as f:
            tree = yaml.safe_load(f) or {}
        nodes = tree.get("nodes", {})
        report = _backfill(nodes, args, thresholds, dry_run=True)
        _summarize(report)
        print("\nDRY-RUN — no files modified. Pass --apply to backfill.",
              file=sys.stderr)
    return 0


def _backfill(nodes, args, thresholds, dry_run=False):
    """Walk every node, fill missing fields. Mutates `nodes` in place when
    not dry_run. Returns a report dict for the summary."""
    from datetime import date as _date
    today = _date.today().isoformat()

    cl_changed = 0
    cl_sources = Counter()
    cl_sample = []
    lu_changed = 0
    lu_sources = Counter()
    lu_sample = []

    do_cl = args.field in ("capability_level", "both")
    do_lu = args.field in ("last_updated", "both")

    cl_missing_before = sum(
        1 for n in nodes.values() if not n.get("capability_level")
    )
    lu_missing_before = sum(
        1 for n in nodes.values() if not n.get("last_updated")
    )

    for key, node in nodes.items():
        modified = False

        if do_cl and not node.get("capability_level"):
            new_level, src = _resolve_capability_level(
                nodes, key, node, thresholds
            )
            if not dry_run:
                node["capability_level"] = new_level
                node.setdefault("backfill_reason", args.reason)
            cl_changed += 1
            cl_sources[src] += 1
            if len(cl_sample) < 5:
                cl_sample.append((key, new_level, src))
            modified = True

        if do_lu and not node.get("last_updated"):
            val, src = _resolve_last_updated(node)
            if not dry_run:
                node["last_updated"] = val
                node.setdefault("backfill_reason", args.reason)
            lu_changed += 1
            lu_sources[src] += 1
            if len(lu_sample) < 5:
                lu_sample.append((key, val, src))
            modified = True

        if modified and not dry_run:
            nodes[key] = node

    return {
        "today": today,
        "total": len(nodes),
        "capability_level": {
            "missing_before": cl_missing_before,
            "changed": cl_changed,
            "sources": dict(cl_sources),
            "sample": cl_sample,
        },
        "last_updated": {
            "missing_before": lu_missing_before,
            "changed": lu_changed,
            "sources": dict(lu_sources),
            "sample": lu_sample,
        },
    }


if __name__ == "__main__":
    sys.exit(main())
