#!/usr/bin/env python3
"""bulk-retire-tree-leaves.py — read-only audit tool that surfaces tree leaf
nodes eligible for the RETIRE phase.

Background:
  Knowledge-system audit (2026-05-09) found that `/tree maintain` ran the
  retire phase 43 times in 30 days but produced 0 candidates and 0 actions.
  Top skip reason in tree-maintenance-log: `llm:no_scanner=1`. The spec
  for RETIRE exists in `.claude/skills/tree/SKILL.md` Phase 5.5 and the
  config knobs (`retire_sessions_unused`, `retire_noise_threshold`,
  `retire_noise_min_retrievals`, `max_retire_per_invocation`) live in
  `core/config/tree.yaml`, but the candidate-detection code is missing
  from `core/scripts/tree.py`. This script fills that gap as a read-only
  audit so the agent can surface the candidate pile without mutating
  shared state.

Criteria (from .claude/skills/tree/SKILL.md Phase 5.5 + core/config/tree.yaml):

  DEAD candidates (never consulted):
    node_type == "leaf"
    AND retrieval_count == 0
    AND depth > 1                     (NOT an L1 domain root)
    AND growth_state != "growing"
    AND age_days >= --min-age-days   (default 30; uses last_updated)

  NOISY candidates (actively unhelpful):
    node_type == "leaf"
    AND retrieval_count >= --noise-min-retrievals  (default 10)
    AND utility_ratio < --noise-threshold          (default 0.1)
    AND depth > 1
    AND growth_state != "growing"

This script is intentionally DRY-RUN ONLY. Tree retirement is more
destructive than RB/guardrail retirement (mutates `_tree.yaml` AND moves
`.md` files to `world/knowledge/archive/`), so a `--apply` mode would
require explicit user authorization. This audit produces the candidate
list; the apply-step lives elsewhere (or in a follow-up script gated on
user signal).

Usage:
  py -3 core/scripts/bulk-retire-tree-leaves.py                  # full report
  py -3 core/scripts/bulk-retire-tree-leaves.py --kind dead      # only dead
  py -3 core/scripts/bulk-retire-tree-leaves.py --kind noisy     # only noisy
  py -3 core/scripts/bulk-retire-tree-leaves.py --json           # machine-readable
  py -3 core/scripts/bulk-retire-tree-leaves.py --min-age-days 14  # tighter age
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _paths import WORLD_DIR  # noqa: E402

import yaml  # noqa: E402

TREE_YAML = WORLD_DIR / "knowledge" / "tree" / "_tree.yaml"


def _parse_date(val):
    """Parse a date or ISO datetime string. Returns date or None."""
    if not val:
        return None
    s = str(val)
    try:
        if "T" in s:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _node_age_days(node, today):
    """Age in days based on last_updated (preferred) or last_retrieved fallback.
    Returns None if neither field is parseable."""
    for field in ("last_updated", "last_retrieved"):
        d = _parse_date(node.get(field))
        if d is not None:
            return (today - d).days
    return None


def _is_dead_candidate(key, node, min_age_days, today):
    if node.get("node_type") != "leaf":
        return False
    if (node.get("retrieval_count", 0) or 0) != 0:
        return False
    if (node.get("depth", 0) or 0) <= 1:
        return False
    if node.get("growth_state") == "growing":
        return False
    age = _node_age_days(node, today)
    if age is None or age < min_age_days:
        return False
    return True


def _is_noisy_candidate(key, node, min_retrievals, threshold):
    if node.get("node_type") != "leaf":
        return False
    rc = node.get("retrieval_count", 0) or 0
    if rc < min_retrievals:
        return False
    ratio = node.get("utility_ratio", 0.0) or 0.0
    if ratio >= threshold:
        return False
    if (node.get("depth", 0) or 0) <= 1:
        return False
    if node.get("growth_state") == "growing":
        return False
    return True


def _summarize_humanreadable(report, out):
    print("\n=== Tree RETIRE candidate audit ===", file=out)
    print(f"  Tree path:             {TREE_YAML}", file=out)
    print(f"  Total nodes:           {report['total_nodes']}", file=out)
    print(f"  Total leaves:          {report['total_leaves']}", file=out)
    print(f"  Reference date:        {report['reference_date']}", file=out)
    print(f"  Min age days (DEAD):   {report['min_age_days']}", file=out)
    print(f"  Noise min retrievals:  {report['noise_min_retrievals']}",
          file=out)
    print(f"  Noise threshold:       {report['noise_threshold']}", file=out)

    for kind in ("dead", "noisy"):
        cands = report["candidates"][kind]
        print(f"\n  {kind.upper()} candidates: {len(cands)} "
              f"({len(cands) / max(report['total_leaves'], 1) * 100:.1f}% of leaves)",
              file=out)
        if not cands:
            continue
        # Sort: dead by age desc, noisy by rc desc
        if kind == "dead":
            samples = sorted(cands,
                             key=lambda c: c.get("age_days") or 0,
                             reverse=True)[:10]
            print(f"    Top 10 by age (oldest never-retrieved first):",
                  file=out)
        else:
            samples = sorted(cands,
                             key=lambda c: c.get("retrieval_count", 0),
                             reverse=True)[:10]
            print(f"    Top 10 by retrieval_count (most-noisy first):",
                  file=out)
        for c in samples:
            key = c.get("key", "?")
            age = c.get("age_days")
            age_s = f"{age}d" if age is not None else "?"
            rc = c.get("retrieval_count", 0)
            ur = c.get("utility_ratio", 0)
            depth = c.get("depth", "?")
            cap = (c.get("capability_level") or "?")[:9]
            print(f"      {key[:42]:42s}  d={depth} cap={cap:9s} "
                  f"rc={rc:3d}  ur={ur:0.2f}  age={age_s}",
                  file=out)


def main():
    p = argparse.ArgumentParser(
        description="Audit tree leaves eligible for RETIRE (dry-run only).",
        epilog="No mutations performed. Apply path TBD pending user sign-off.",
    )
    p.add_argument("--kind", choices=["dead", "noisy", "both"], default="both",
                   help="Which candidate kind to surface (default: both).")
    p.add_argument("--min-age-days", type=int, default=30,
                   help="DEAD: min age (last_updated → today) in days. "
                        "Default 30. Spec says 'created before session N-5' "
                        "which roughly maps to weeks of calendar age.")
    p.add_argument("--noise-min-retrievals", type=int, default=10,
                   help="NOISY: minimum retrieval_count for a leaf to count "
                        "as noise (default 10, matches retire_noise_min_retrievals).")
    p.add_argument("--noise-threshold", type=float, default=0.1,
                   help="NOISY: utility_ratio strictly less than this counts "
                        "as noise (default 0.1, matches retire_noise_threshold).")
    p.add_argument("--json", action="store_true",
                   help="Emit raw JSON instead of human-readable text.")
    args = p.parse_args()

    if not TREE_YAML.exists():
        print(f"ERROR: tree yaml not found at {TREE_YAML}", file=sys.stderr)
        return 1

    with open(TREE_YAML, "r", encoding="utf-8") as f:
        tree = yaml.safe_load(f) or {}
    nodes = tree.get("nodes", {})
    today = date.today()

    leaves = [(k, n) for k, n in nodes.items()
              if n.get("node_type") == "leaf"]

    dead = []
    noisy = []
    if args.kind in ("dead", "both"):
        for k, n in leaves:
            if _is_dead_candidate(k, n, args.min_age_days, today):
                dead.append({
                    "key": k,
                    "depth": n.get("depth"),
                    "parent": n.get("parent"),
                    "file": n.get("file"),
                    "summary": (n.get("summary") or "")[:120],
                    "retrieval_count": n.get("retrieval_count", 0),
                    "times_helpful": n.get("times_helpful", 0),
                    "times_noise": n.get("times_noise", 0),
                    "utility_ratio": n.get("utility_ratio", 0.0),
                    "growth_state": n.get("growth_state"),
                    "capability_level": n.get("capability_level"),
                    "last_updated": n.get("last_updated"),
                    "last_retrieved": n.get("last_retrieved"),
                    "age_days": _node_age_days(n, today),
                })
    if args.kind in ("noisy", "both"):
        for k, n in leaves:
            if _is_noisy_candidate(k, n, args.noise_min_retrievals,
                                   args.noise_threshold):
                noisy.append({
                    "key": k,
                    "depth": n.get("depth"),
                    "parent": n.get("parent"),
                    "file": n.get("file"),
                    "summary": (n.get("summary") or "")[:120],
                    "retrieval_count": n.get("retrieval_count", 0),
                    "times_helpful": n.get("times_helpful", 0),
                    "times_noise": n.get("times_noise", 0),
                    "utility_ratio": n.get("utility_ratio", 0.0),
                    "growth_state": n.get("growth_state"),
                    "capability_level": n.get("capability_level"),
                    "last_updated": n.get("last_updated"),
                    "last_retrieved": n.get("last_retrieved"),
                    "age_days": _node_age_days(n, today),
                })

    report = {
        "tree_yaml": str(TREE_YAML),
        "reference_date": today.isoformat(),
        "total_nodes": len(nodes),
        "total_leaves": len(leaves),
        "min_age_days": args.min_age_days,
        "noise_min_retrievals": args.noise_min_retrievals,
        "noise_threshold": args.noise_threshold,
        "candidates": {"dead": dead, "noisy": noisy},
    }

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _summarize_humanreadable(report, sys.stdout)
        print(file=sys.stdout)
        print("DRY-RUN ONLY — no nodes retired. Apply path requires user "
              "sign-off (mutates _tree.yaml + archives .md files).",
              file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
