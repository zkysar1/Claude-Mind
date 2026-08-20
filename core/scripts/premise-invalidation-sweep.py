#!/usr/bin/env python3
"""Surface goals whose PREMISE may have been invalidated by another goal's finding.

WHY THIS EXISTS
---------------
When a goal overturns a diagnosis, the superseded premise does not stay in one
place. It has already been written into the descriptions and outcome_notes of
whatever goals were filed FROM that diagnosis, and those goals keep running on
it. `.claude/rules/` guard-4526 makes correcting them an obligation; this script
is how you FIND them.

The obvious search — grep the store for the symptom's keywords — does not work.
Measured 2026-08-20 on g-326-468 (zakpod1 host-RAM root cause): a symptom-keyword
grep returned 180 candidate goals for a question with 3 real answers, which is
indistinguishable from no filter at all.

What does work is walking the goal-id CITATION graph, and the two
non-obvious properties are why this is a script and not a one-liner:

  1. It must be BIDIRECTIONAL. The parent symptom goal that a finding overturns
     typically does NOT cite the finding — the citation runs the other way,
     because the finding was filed FROM the parent. Measured: g-326-452 was the
     goal whose diagnosis was overturned and it cites g-326-468 nowhere, at any
     depth. Outbound-only traversal can never reach it.
  2. It must be TRANSITIVE. Goals filed from a goal filed from the finding cite
     their immediate parent, not the finding. g-326-472 cites g-326-469, which
     cites g-326-468.

Measured recall on that incident (2026-08-20), against the 3 goals that
genuinely needed correcting:

    symptom keyword grep      2/3      180 candidates
    outbound 1-hop            1/3        1
    outbound 2-hop            2/3        2
    bidirectional 1-hop       2/3        3
    bidirectional 2-hop       3/3       11      <- the default here

The RECALL column is the durable claim. The candidate COUNTS are dated and
drift upward as the store grows — the same seed returned 13 candidates a few
hours later the same day — so they are comparable only within that one run.
Re-measure before quoting a count, and do not read a changed count as a
regression.

REPORT-ONLY, DELIBERATELY
-------------------------
This script never writes and never mutates a goal. That is not timidity, it is
guard-1227 + guard-1231: a sweep that moves a goal to a terminal status must
leave the filer a recoverable trace and must have a visibility consumer, because
a swept goal leaves BOTH the selector candidate list and its blocked list and is
otherwise invisible (2026-07-19: 7 goals silently skipped for days). A premise
being stale is also not a terminal verdict — it is a prompt for a human or agent
to re-derive the goal, which is exactly the judgment `reclaim-routed-work.md`
rule 1 says not to delegate. So this surfaces candidates and stops.

USAGE
    py -3 core/scripts/premise-invalidation-sweep.py --goal g-326-468
    py -3 core/scripts/premise-invalidation-sweep.py --goal g-326-468 --hops 3 --json
    py -3 core/scripts/premise-invalidation-sweep.py --goal g-326-468 --exit-on-hits
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _paths import WORLD_DIR, AGENT_DIR  # noqa: E402

GOAL_ID_RE = re.compile(r"\bg-\d+-\d+\b")

# Mirrors CLAUDE.md "Status Values" for goals. A terminal goal can still carry a
# stale premise in its outcome_note ( did, and it was worth correcting),
# so terminal hits are reported — just ranked below the live ones.
TERMINAL_STATUSES = {"completed", "archived", "skipped", "expired"}

# Fields whose prose is scanned for citations. outcome_note matters as much as
# description: a diagnosis usually lands in the note, not the original ask.
CITATION_FIELDS = ("title", "description", "outcome_note")


def stale_backend_warning():
    """Return a warning string when the local JSONL read may not be authoritative.

    load_goals() reads the world aspirations JSONL straight off the local
    filesystem. rb-2636: under STORAGE_BACKEND=own-cloud the authoritative
    store is remote and served by the daemon, and a direct local read can
    return a stale or empty copy — which in this tool means silently FEWER
    candidates, the same false-negative direction as a dropped edge.

    Rerouting through a daemon wrapper is deliberately NOT the fix, for two
    independently sufficient reasons: guard-744 (a daemon-backed wrapper
    invoked from inside a Python subprocess misses the PreToolUse env
    injection and resolves an empty runtime port), and aspirations-read.sh
    exposes no bulk full-prose goal read to reroute to in the first place.
    So the condition is surfaced, exactly as the dropped-edge count is.
    """
    if os.environ.get("STORAGE_BACKEND", "local").strip().lower() == "own-cloud":
        return (
            "WARNING: STORAGE_BACKEND=own-cloud — this tool reads the LOCAL "
            "aspirations JSONL, which rb-2636 documents as non-authoritative "
            "under own-cloud. Candidates may be MISSING and this report may "
            "read as an all-clear when it is not. Cross-check via a "
            "daemon-routed reader before concluding nothing needs correcting."
        )
    return None


def load_goals():
    """Return {goal_id: goal_dict} across world + bound agent aspiration stores.

    Aspirations are NESTED one level (aspiration record -> .goals[]); a top-level
    scan finds nothing (guard-723). Unreadable or absent stores are skipped
    rather than fatal — this is an advisory tool.
    """
    goals = {}
    stores = [WORLD_DIR / "aspirations.jsonl"]
    if AGENT_DIR:
        stores.append(AGENT_DIR / "aspirations.jsonl")

    for store in stores:
        if not store.exists():
            continue
        with store.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    asp = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for goal in asp.get("goals") or []:
                    gid = goal.get("id")
                    if gid:
                        goals[gid] = goal
    return goals


def build_graph(goals):
    """Return (outbound, inbound, stats) adjacency over prose goal-id citations.

    outbound[x] = goals that cite x   (things derived FROM x)
    inbound[x]  = goals that x cites  (things x was derived FROM)

    stats reports how much of the graph could NOT be built. A citation whose
    target is absent from the loaded stores is unusable and gets dropped here.
    Dropping an edge can only REMOVE candidates downstream, never add one, so
    every drop is a potential false negative in a tool whose whole job is
    "which goals are still running on the superseded premise" — and a false
    negative here reads exactly like a clean all-clear. Hence it is counted
    and reported rather than silently absorbed.

    Measured 2026-08-20 on this corpus (alpha, DESKTOP-O91DLK2): 5,263 of
    10,173 edges dropped (51.7%), across 2,623 distinct unresolvable ids.
    Loading the archive is NOT the fix and was measured before being ruled
    out — only 103 of those 2,623 (3.9%) are in aspirations-archive.jsonl;
    the remaining 96.1% are dangling citations present in no store at all
    (three spot-checked through the daemon reader, 3/3 not-found). See
    g-115-6946 and guard-4472.
    """
    outbound, inbound = {}, {}
    edges_resolved = 0
    edges_dropped = 0
    unresolvable = set()
    for gid, goal in goals.items():
        prose = " ".join(str(goal.get(f) or "") for f in CITATION_FIELDS)
        cited = {r for r in GOAL_ID_RE.findall(prose) if r != gid}
        refs = {r for r in cited if r in goals}
        missing = cited - refs
        edges_resolved += len(refs)
        edges_dropped += len(missing)
        unresolvable |= missing
        inbound[gid] = refs
        for ref in refs:
            outbound.setdefault(ref, set()).add(gid)
    total = edges_resolved + edges_dropped
    stats = {
        "edges_total": total,
        "edges_resolved": edges_resolved,
        "edges_dropped": edges_dropped,
        "dropped_pct": round(100.0 * edges_dropped / total, 1) if total else 0.0,
        "unresolvable_ids": len(unresolvable),
    }
    return outbound, inbound, stats


def walk(seed, outbound, inbound, hops):
    """Bidirectional BFS from seed. Returns {goal_id: (distance, via)}."""
    found = {}
    frontier = {seed}
    seen = {seed}
    for dist in range(1, hops + 1):
        nxt = {}
        for node in frontier:
            for neighbour in outbound.get(node, set()) | inbound.get(node, set()):
                if neighbour not in seen and neighbour not in nxt:
                    nxt[neighbour] = node
        if not nxt:
            break
        for gid, via in nxt.items():
            found[gid] = (dist, via)
            seen.add(gid)
        frontier = set(nxt)
    return found


def main():
    ap = argparse.ArgumentParser(
        description="Surface goals whose premise may be invalidated by a goal's finding."
    )
    ap.add_argument("--goal", required=True,
                    help="the goal whose finding/diagnosis changed (e.g. g-326-468)")
    ap.add_argument("--hops", type=int, default=2,
                    help="citation-graph traversal depth (default 2; measured sufficient)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--exit-on-hits", action="store_true",
                    help="exit 1 when live (non-terminal) candidates are found")
    args = ap.parse_args()

    goals = load_goals()
    backend_warning = stale_backend_warning()
    if backend_warning and not args.json:
        print(backend_warning, file=sys.stderr)

    if args.goal not in goals:
        msg = f"goal {args.goal} not found in world or agent aspirations"
        if backend_warning:
            # A stale own-cloud read is a live candidate explanation for a seed
            # that "does not exist", so say so here rather than letting the
            # caller conclude the goal id was wrong.
            msg += (" — NOTE: STORAGE_BACKEND=own-cloud, so this may be the "
                    "stale-local-read failure in rb-2636 rather than a bad id")
        print(json.dumps({"error": "goal_not_found", "detail": msg})
              if args.json else f"ERROR: {msg}", file=sys.stderr)
        return 2

    outbound, inbound, graph_stats = build_graph(goals)
    found = walk(args.goal, outbound, inbound, args.hops)

    rows = []
    for gid, (dist, via) in found.items():
        goal = goals[gid]
        status = (goal.get("status") or "unknown").lower()
        rows.append({
            "id": gid,
            "status": status,
            "live": status not in TERMINAL_STATUSES,
            "hops": dist,
            "via": via if via != args.goal else None,
            "title": str(goal.get("title") or "")[:100],
        })
    # live first, then nearest, then id for stable output
    rows.sort(key=lambda r: (not r["live"], r["hops"], r["id"]))
    live = [r for r in rows if r["live"]]

    if args.json:
        print(json.dumps({
            "seed": args.goal, "hops": args.hops,
            "candidates": len(rows), "live_candidates": len(live),
            "graph": graph_stats,
            "backend_warning": backend_warning,
            "results": rows,
        }, indent=2))
    else:
        print(f"PREMISE-INVALIDATION SWEEP — seed {args.goal}, bidirectional {args.hops}-hop")
        print("REPORT ONLY: nothing is mutated. Re-derive each goal; do not close on age.\n")
        if not rows:
            print("  no goals cite or are cited by this one — nothing to re-derive.")
        for row in rows:
            flag = "LIVE" if row["live"] else "term"
            via = f" via {row['via']}" if row["via"] else ""
            print(f"  [{flag}] {row['id']:<12} {row['status']:<12} {row['hops']}-hop{via}")
            print(f"         {row['title']}")
        print(f"\n  {len(live)} live of {len(rows)} candidates.")
        # Coverage prints WITH the count it qualifies, and unconditionally.
        # A dropped edge can only hide a candidate, so an unqualified "0 live"
        # is the single output most likely to be misread as an all-clear.
        print(f"  graph coverage: {graph_stats['edges_resolved']} of "
              f"{graph_stats['edges_total']} citation edges resolved; "
              f"{graph_stats['edges_dropped']} dropped "
              f"({graph_stats['dropped_pct']}%) across "
              f"{graph_stats['unresolvable_ids']} unresolvable ids.")
        if graph_stats["edges_dropped"]:
            print("  dropped edges can only HIDE candidates, never invent one — "
                  "read this list as a floor, not a complete set.")
        if live:
            print("  For each: does its premise still hold given the new finding? "
                  "(guard-4526)")

    return 1 if (args.exit_on_hits and live) else 0


if __name__ == "__main__":
    sys.exit(main())
