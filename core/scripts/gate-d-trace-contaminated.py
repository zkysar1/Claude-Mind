#!/usr/bin/env python3
# domain-leak-exempt: Gate D analysis tool — references the gate-d telemetry paths,
# the ayoai world identifier, and experiment arm labels by design (analysis-stage
# companion to the pre-registered methodology; runs at interim/final reads only).
"""gate-d-trace-contaminated.py — SPILL-1 contaminated-ids tracer (Gate D).

Binding spec: Gate D methodology (RATIFIED 2026-06-10) Addendum A; consumer is
the Lodestar verdict analyzer: ``gate-d-analyze.ts --contaminated <jsonl>``.
ANALYSIS-STAGE tool — it never touches assignment or flag logic, and it is run
by omni at interim/final reads, not by the loop.

What it computes
----------------
Arm-A goals that plausibly benefited from knowledge ENCODED out of arm-B
executions (spillover through the shared stores). The verdict module excludes
these in a sensitivity recompute (SPILL-1): if the GO direction flips without
them, the verdict degrades to INCONCLUSIVE.

Method (most-precise available evidence, falling back conservatively):
1. ASSIGNMENT records (``record_type: assignment``) are read from the telemetry
   JSONL(s); B-derived goal-ids = arm B with ``injection_status: injected``.
2. "Tainted" store entries = reasoning-bank records and knowledge-tree nodes
   whose ``origin_goal_id`` (g-325-06 instrumentation) is one of those B-arm
   goal-ids.
3. An arm-A goal is marked contaminated when a ``retrieval-trace.jsonl`` row
   for the SAME (agent, goal_id) is timestamped AFTER a tainted entry's
   creation AND its retrieval ``category`` text shares >= MIN_OVERLAP
   distinctive tokens with the tainted entry's category/tags/title.
4. If no retrieval trace file exists, EVERY arm-A goal assigned after the
   first tainted entry's creation is marked (``basis: time-fallback``).

CONSERVATIVENESS: the retrieval trace records categories, not record ids, so
step 3 over-marks (any same-flavored retrieval counts, whether or not the
tainted record itself surfaced). Over-marking enlarges the SPILL-1 exclusion
set, which can only weaken a GO — it can never inflate the measured effect.
The time-fallback is maximally conservative for the same reason.

Usage:
  py -3 core/scripts/gate-d-trace-contaminated.py \
      --telemetry "agents/*/session/gate-d-telemetry.jsonl" \
      --out gate-d-contaminated.jsonl
  (paths relative to the Ayoai-Mind repo root; world resolved via _paths)
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

MIN_OVERLAP = 2  # distinctive shared tokens between retrieval category and tainted entry

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP = {
    "the", "and", "for", "are", "was", "with", "this", "that", "from", "have",
    "not", "but", "all", "can", "its", "our", "out", "you", "will", "when",
    "what", "which", "how", "why", "where", "into", "over", "under", "about",
}


def _tok(text):
    return {t for t in _TOKEN_RE.findall(str(text).lower()) if len(t) >= 3 and t not in _STOP}


def _read_jsonl(path):
    out = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def _tree_nodes_with_origin(tree_yaml_path):
    """Yield (origin_goal_id, text, created) from _tree.yaml nodes, tolerant of shape."""
    try:
        import yaml  # noqa: PLC0415 — optional; tree taint is skipped without it
        with open(tree_yaml_path, "r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
    except Exception:
        return
    stack = [doc]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            origin = node.get("origin_goal_id")
            if origin:
                text = " ".join(
                    str(node.get(k, "")) for k in ("name", "title", "category", "summary")
                )
                yield str(origin), text, str(node.get("created", node.get("last_updated", "")))
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def main():
    ap = argparse.ArgumentParser(description="Gate D SPILL-1 contaminated-ids tracer")
    ap.add_argument("--telemetry", required=True,
                    help="glob for gate-d-telemetry.jsonl files (repo-root-relative ok)")
    ap.add_argument("--out", required=True, help="output JSONL path")
    ap.add_argument("--world-dir", default=None,
                    help="override world dir (default: resolve via _paths)")
    args = ap.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(script_dir))
    if args.world_dir:
        world = args.world_dir
    else:
        sys.path.insert(0, script_dir)
        from _paths import WORLD_DIR  # noqa: PLC0415
        world = str(WORLD_DIR)

    # 1) telemetry -> arm sets
    assignments = []
    for pat in [args.telemetry, os.path.join(root, args.telemetry)]:
        hits = glob.glob(pat)
        if hits:
            for p in hits:
                assignments.extend(
                    r for r in _read_jsonl(p) if r.get("record_type") == "assignment"
                )
            break
    b_goal_ids = {r["goal_id"] for r in assignments
                  if r.get("arm") == "B" and r.get("injection_status") == "injected"}
    a_pairs = [(r.get("agent", ""), r["goal_id"], str(r.get("timestamp", "")))
               for r in assignments if r.get("arm") == "A" and not r.get("excluded")]
    print(f"[trace] assignments={len(assignments)} B-injected={len(b_goal_ids)} "
          f"A-eligible={len(a_pairs)}", file=sys.stderr)

    # 2) tainted store entries (rb + tree) by origin_goal_id
    tainted = []  # (entry_id, token_set, created_ts)
    for rec in _read_jsonl(os.path.join(world, "reasoning-bank.jsonl")):
        if rec.get("origin_goal_id") in b_goal_ids:
            text = " ".join([str(rec.get("category", "")), str(rec.get("title", "")),
                             " ".join(rec.get("tags") or [])])
            tainted.append((str(rec.get("id")), _tok(text), str(rec.get("created", ""))))
    tree_yaml = os.path.join(world, "knowledge", "tree", "_tree.yaml")
    if os.path.isfile(tree_yaml):
        for origin, text, created in _tree_nodes_with_origin(tree_yaml) or []:
            if origin in b_goal_ids:
                tainted.append((f"tree:{origin}", _tok(text), created))
    print(f"[trace] tainted-entries={len(tainted)}", file=sys.stderr)

    contaminated = []
    if tainted:
        first_taint_ts = min(ts for _, _, ts in tainted if ts) if any(
            ts for _, _, ts in tainted) else ""
        trace_path = os.path.join(world, "retrieval-trace.jsonl")
        traces = _read_jsonl(trace_path)
        if traces:
            # 3) category+time-aware match (conservative: categories, not record ids)
            by_pair = {}
            for t in traces:
                key = (str(t.get("agent", "")), str(t.get("goal_id", "")))
                by_pair.setdefault(key, []).append(t)
            for agent, goal_id, _ts in a_pairs:
                refs = set()
                for t in by_pair.get((agent, goal_id), []):
                    cat_toks = _tok(t.get("category", ""))
                    t_ts = str(t.get("ts", ""))
                    for entry_id, toks, created in tainted:
                        if created and t_ts and t_ts <= created:
                            continue
                        if len(cat_toks & toks) >= MIN_OVERLAP:
                            refs.add(entry_id)
                if refs:
                    contaminated.append({"agent": agent, "goal_id": goal_id,
                                         "basis": "retrieval-trace-category",
                                         "tainted_refs": sorted(refs)})
        else:
            # 4) maximally conservative fallback
            for agent, goal_id, ts in a_pairs:
                if not first_taint_ts or not ts or ts >= first_taint_ts:
                    contaminated.append({"agent": agent, "goal_id": goal_id,
                                         "basis": "time-fallback", "tainted_refs": []})

    with open(args.out, "w", encoding="utf-8") as fh:
        for row in contaminated:
            fh.write(json.dumps(row) + "\n")
    print(f"[trace] contaminated={len(contaminated)} -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
