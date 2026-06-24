#!/usr/bin/env python3
# domain-leak-exempt: framework hypothesis-structure infra — generic graph logic, no domain strings.
"""Hypothesis lineage — give the flat pipeline its tree edges (Phase 3, Arbor).

Part of the evaluative substrate; eval_harness.py is the keystone + in-code index of all seven.

WHY THIS EXISTS
---------------
The framework's hypotheses live as independent records in pipeline.jsonl, but real inquiry
branches: a hypothesis spawns sub-hypotheses; a refutation should prune a whole branch; the
knowledge tree already does upward confidence rollup, yet the hypothesis store stayed flat. The
"Arbor" paper (2606.11926) showed persistent hypothesis management + insight propagation drove
>2.5x over flat baselines, and that the gain comes from the EDGES, not a heavyweight node schema.

This module adds exactly the edges, domain-free and additive: two optional fields on a
hypothesis record — `parent_hypothesis` (an id) and `relation` (how it relates to the parent) —
plus the small amount of graph logic that makes them useful: validation (refs resolve, relations
are valid, the graph is acyclic), traversal, and the one mechanism that pays for the structure —
**prune-on-refutation**.

RELATION SEMANTICS (the load-bearing distinction)
-------------------------------------------------
  refines      child specializes/narrows the parent; it DEPENDS on the parent holding.
               => if the parent is refuted, the child is undermined (prune candidate).
  contradicts  child is a competing/alternative hypothesis to the parent.
               => if the parent is refuted, the child is NOT pruned (it may now be MORE
                  likely — refuting an alternative corroborates the contradictor).
  supersedes   child replaces the parent (parent already treated as obsolete).
               => parent refutation is irrelevant to the child.
So prune-on-refutation follows ONLY `refines` edges. Getting this wrong would prune the very
alternative hypotheses that a refutation should promote — hence it is the core tested behavior.

GROUNDING (verified against Arbor full text, 2606.11926 §4.3): this trichotomy is Arbor's own.
Its HTR "Ideate" step has the coordinator propose children that each "represents a **refinement,
alternative, or correction** of the parent hypothesis" — which is exactly refines / contradicts /
supersedes. Arbor also conditions future ideation on the tree: "validated insights provide
assumptions to build on, **pruned nodes provide negative constraints**." So the id list this
module returns from `prune_on_refutation` is meant to be fed back as negative constraints (do not
re-propose an undermined branch) — not merely marked dead. That feedback loop is the caller's job;
this module supplies the structure and the prune set.

Pure, hermetic, domain-free. No import-time path resolution.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set

VALID_RELATIONS = frozenset({"refines", "contradicts", "supersedes"})


def index(records: Sequence[dict]) -> Dict[str, dict]:
    """Map id -> record. Raises on a missing or duplicate id (a lineage graph
    with ambiguous ids cannot be traversed safely)."""
    out: Dict[str, dict] = {}
    for r in records:
        if "id" not in r:
            raise ValueError(f"hypothesis record missing 'id': {r!r}")
        rid = r["id"]
        if rid is None or (isinstance(rid, str) and not rid.strip()):
            raise ValueError(f"hypothesis record has an empty/null 'id': {r!r}")
        if rid in out:
            raise ValueError(f"duplicate hypothesis id: {rid}")
        out[rid] = r
    return out


def validate_lineage(records: Sequence[dict]) -> None:
    """Validate the lineage graph. Raises ValueError on any defect:
      - a `parent_hypothesis` that does not resolve to a known record,
      - a `relation` not in VALID_RELATIONS (or absent when a parent is set),
      - a `relation` set without a `parent_hypothesis`,
      - a cycle (lineage must be a forest — a hypothesis cannot be its own ancestor).
    Records with neither field are valid roots (backward-compatible: existing flat
    records pass unchanged).
    """
    idx = index(records)
    for r in records:
        parent = r.get("parent_hypothesis")
        relation = r.get("relation")
        if parent is None and relation is None:
            continue  # flat/root record — fine
        if parent is not None and parent not in idx:
            raise ValueError(f"{r['id']}: parent_hypothesis {parent!r} not found")
        if parent is not None and relation not in VALID_RELATIONS:
            raise ValueError(f"{r['id']}: relation {relation!r} invalid with a parent "
                             f"(expected one of {sorted(VALID_RELATIONS)})")
        if parent is None and relation is not None:
            raise ValueError(f"{r['id']}: relation {relation!r} set without a parent_hypothesis")
    # cycle detection: walk parent chain from each node, bound by node count
    for r in records:
        seen: Set[str] = set()
        cur = r.get("parent_hypothesis")
        while cur is not None:
            if cur == r["id"] or cur in seen:
                raise ValueError(f"lineage cycle detected through {r['id']}")
            seen.add(cur)
            cur = idx[cur].get("parent_hypothesis")


def children(records: Sequence[dict], hid: str,
             relation: Optional[str] = None) -> List[dict]:
    """Direct children of `hid` (records whose parent_hypothesis == hid),
    optionally filtered to a single relation type."""
    return [r for r in records
            if r.get("parent_hypothesis") == hid
            and (relation is None or r.get("relation") == relation)]


def subtree_ids(records: Sequence[dict], root_id: str,
                follow_relations: Optional[Set[str]] = None) -> Set[str]:
    """Ids in the subtree rooted at `root_id` (inclusive), following only edges
    whose child `relation` is in `follow_relations` (None = follow all).
    Breadth-first; safe on a validated (acyclic) graph."""
    if root_id not in index(records):
        raise ValueError(f"unknown root id: {root_id}")
    out: Set[str] = {root_id}
    q = deque([root_id])
    while q:
        cur = q.popleft()
        for c in children(records, cur):
            if follow_relations is not None and c.get("relation") not in follow_relations:
                continue
            cid = c["id"]
            if cid not in out:
                out.add(cid)
                q.append(cid)
    return out


def prune_on_refutation(records: Sequence[dict], refuted_id: str) -> List[str]:
    """Ids undermined by refuting `refuted_id`: its transitive `refines`-descendants.

    Excludes the refuted node itself (the caller marks that refuted directly) and
    excludes `contradicts`/`supersedes` children (a refuted parent does NOT undermine
    a competing alternative — that is the whole point of tracking the relation).
    Returns a sorted list for determinism.
    """
    validate_lineage(records)  # never prune off a malformed graph
    sub = subtree_ids(records, refuted_id, follow_relations={"refines"})
    return sorted(sub - {refuted_id})


def child_outcome_summary(records: Sequence[dict], parent_id: str,
                          status_field: str = "status") -> Dict[str, Dict[str, int]]:
    """Neutral rollup the caller can turn into a confidence update: per relation,
    count children by their status value. (The confidence MATH is domain-specific —
    e.g. a confirmed `contradicts` child should lower the parent's confidence, a
    confirmed `refines` child should raise it — so this returns the counts and lets
    the caller apply its own policy rather than baking one in.)

    Does NOT call validate_lineage — it is a neutral count over whatever direct
    children exist, truthful even on an unvalidated graph. Call validate_lineage
    first if you need the graph's integrity guaranteed.
    """
    summary: Dict[str, Dict[str, int]] = {}
    for c in children(records, parent_id):
        rel = c.get("relation", "unknown")
        st = str(c.get(status_field, "unknown"))
        summary.setdefault(rel, {})
        summary[rel][st] = summary[rel].get(st, 0) + 1
    return summary


def _load(path) -> List[dict]:
    out: List[dict] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(json.loads(line))
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Hypothesis lineage operations.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate", help="validate lineage of a pipeline JSONL")
    v.add_argument("--pipeline", required=True)
    p = sub.add_parser("prune", help="ids undermined by refuting a hypothesis")
    p.add_argument("--pipeline", required=True)
    p.add_argument("--refuted", required=True)
    args = ap.parse_args(argv)
    records = _load(args.pipeline)
    if args.cmd == "validate":
        validate_lineage(records)
        print(json.dumps({"valid": True, "n": len(records)}, indent=2))
        return 0
    if args.cmd == "prune":
        print(json.dumps({"undermined": prune_on_refutation(records, args.refuted)}, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
