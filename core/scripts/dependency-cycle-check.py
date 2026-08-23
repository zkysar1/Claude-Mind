#!/usr/bin/env python3
"""Dependency-Cycle Check — walk the `blocked_by` GRAPH and report rings.

THE GAP THIS FILLS. Every existing check inspects a single EDGE, and a
dependency cycle is a property of the GRAPH, so a two-goal ring passes all of
them at once. Filed from ZDS-Mind by omni (2026-07-29) off a live incident:
X blocked_by Y and Y blocked_by X froze one aspiration at 71.8%, and each guard
waved it through for a different reason —
  * blocker_ref-presence sees a populated `blocked_by` and passes;
  * reason-less-blocked-check looks for an EMPTY block signal, these are full;
  * defer-recheck clears only when the cited dependency is COMPLETED, and here
    it is BLOCKED, which reads as a live dependency;
  * blocker-recheck re-probes CAPABILITY, not shape.
The selector then drops both goals from its candidate list AND from its own
blocked-work reporting, so the deadlock is invisible from every angle
simultaneously. The only escape is the 48h dependency fail-open, which releases
BOTH goals at once — handing a dependent back before its prerequisite — while
looking like an ordinary unblock. It was found only by dumping every blocked
goal beside its `blocked_by` and reading the list by hand.

WHY THIS IS A SEPARATE SWEEP rather than a field on
`blocked-signal-resolution-check.py`, which already loads exactly the records
needed: that sweep scans `status=blocked`, and guard-1690 names precisely that
filter as a DEAD ZONE — a goal set to `skipped` (or left `pending`) while
holding a live `blocked_by` is invisible to precheck 0.5b.11 and 0.5b.12. This
sweep therefore scans EVERY non-terminal goal regardless of status. Measured
2026-08-09 (bravo, cc-05): 26 non-terminal goals carry edges, of which only a
minority are `status=blocked`, so folding the walk into the blocked-only sweep
would have inherited a filter that hides most of the population.

DETECTIVE, NOT CORRECTIVE — deliberately no `--apply`. Breaking a cycle means
choosing which edge is wrong, and that is a judgment about intent, not shape:
in the founding incident the goal made to wait opened its own description with
the words "PREREQUISITE for", so the correct edge was recoverable only by
reading the goals. An automated break would pick a victim arbitrarily and would
look like a normal unblock while doing it — the same failure mode as the 48h
fail-open this sweep exists to pre-empt.

POPULATION IS ALWAYS REPORTED, even at zero cycles. A bare `cycles: 0` is
indistinguishable from a sweep that scanned nothing, and that ambiguity is the
whole reason the founding incident stayed invisible (rb-245, guard-1922:
a condition whose signal is not durably readable retires itself silently, always
as a pass). `goals_scanned` / `edges_total` / `goals_with_edges` make a zero
falsifiable.

Guards honoured: guard-1890 (resolve ids against the ARCHIVE as well as the
active queues — otherwise a COMPLETED-then-ARCHIVED dependency is
indistinguishable from one that never existed, and the sweep reports a
false dangling edge), guard-383 (per-source read error is fatal in an N>=2
aggregator), guard-614 (structured JSON output), guard-365 (bash wrapper),
guard-547 (`norm_blocked_by` lives in the shared `_dependency_graph` module,
not hand-mirrored here).

Reference: g-115-3875. ZDS-side knowledge: guard-218, rb-725.
"""

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from _dependency_graph import (  # noqa: E402
    TERMINAL_STATUSES,
    build_graph,
    find_cycles,
)
import _rt  # noqa: E402  canonical Python -> daemon client

SOURCES = ("world", "agent")


def _tolerant_decode(label, raw):
    """guard-383 contract: empty -> None, raw_decode recovery, fatal on a
    JSONDecodeError or a non dict-or-list body."""
    return _rt.tolerant_decode_aggregate(f"dependency-cycle-check: {label}", raw)


def _iter_goals(data, source, archived):
    for asp in (data.get("aspirations") if isinstance(data, dict) else data) or []:
        for g in asp.get("goals", []) or []:
            g["_source"] = source
            g["_aspiration_id"] = asp.get("id")
            g["_archived"] = archived
            yield g


def _read_active(source):
    """Active goals for one source. guard-383: a read error here is FATAL.

    A silent `return []` in an N>=2 source aggregator writes a
    complete-looking lie into the merged graph — and for THIS sweep the lie is
    specifically "no cycles", because a missing source cannot contribute edges.
    The single fail-open boundary is the shell wrapper, never here.
    """
    try:
        out = _rt.aspirations_read(source=source, active=True)
    except _rt.RtError as e:
        print(f"[dependency-cycle-check] {source} read failed: {e.body or e}",
              file=sys.stderr)
        sys.exit(1)
    data = _tolerant_decode(source, out)
    return [] if data is None else list(_iter_goals(data, source, False))


def _read_archived(source):
    """Goals inside ARCHIVED aspirations for one source (guard-1890).

    DEGRADES, never fatal — and the asymmetry with `_read_active` above is
    deliberate. Losing the archive falls back to exactly the pre-guard-1890
    behaviour (an archived referent reads as dangling), which is a
    false-positive direction: noisy but visible. Losing an ACTIVE source
    removes edges and silently manufactures a clean verdict. Degradation is
    reported in the payload as `archive_degraded` so a dangling-edge list is
    never read as authoritative when the archive was unavailable.
    """
    try:
        out = _rt.aspirations_read(source=source, archive=True)
    except _rt.RtError as e:
        print(f"[dependency-cycle-check] {source} archive read failed "
              f"(degrading): {e.body or e}", file=sys.stderr)
        return None
    data = _tolerant_decode(f"{source} archive", out)
    # An EMPTY archive is a valid state (fresh world), NOT a failure — flipping
    # `archive_degraded` there would make every clean world look degraded.
    return [] if data is None else list(_iter_goals(data, source, True))


def main():
    ap = argparse.ArgumentParser(
        description="Detect dependency cycles in the blocked_by graph across "
                    "the world and agent queues (detective only).")
    ap.add_argument("--output", choices=["json", "human"], default="json")
    ap.add_argument("--exit-on-cycles", action="store_true",
                    help="exit 1 when any cycle is found (for gates/CI); "
                         "default exits 0 so a precheck sweep never blocks "
                         "the loop on a detection")
    args = ap.parse_args()

    goal_index = {}
    archive_degraded = False
    for source in SOURCES:
        for g in _read_active(source):
            if g.get("id"):
                goal_index[g["id"]] = g
        arch = _read_archived(source)
        if arch is None:
            archive_degraded = True
            continue
        for g in arch:
            # Live records win: an aspiration can be mid-archive, so the
            # archive copy may be a stale snapshot of a goal that is also live.
            if g.get("id") and g["id"] not in goal_index:
                goal_index[g["id"]] = g

    edges, dangling = build_graph(goal_index)
    cycles = find_cycles(edges)

    detail = []
    for cyc in cycles:
        detail.append({
            "length": len(cyc),
            "self_loop": len(cyc) == 1,
            "goals": [{
                "goal_id": gid,
                "status": (goal_index.get(gid) or {}).get("status"),
                "aspiration_id": (goal_index.get(gid) or {}).get("_aspiration_id"),
                "source": (goal_index.get(gid) or {}).get("_source"),
                "title": ((goal_index.get(gid) or {}).get("title") or "")[:90],
                "blocked_by": (goal_index.get(gid) or {}).get("blocked_by"),
            } for gid in cyc],
        })

    payload = {
        # POPULATION FIRST — a zero verdict is only meaningful beside what was
        # actually scanned (see the module docstring).
        "goals_scanned": len(goal_index),
        "goals_with_edges": len(edges),
        "edges_total": sum(len(v) for v in edges.values()),
        "sources": list(SOURCES),
        "archive_degraded": archive_degraded,
        "cycles_found": len(cycles),
        "cycles": detail,
        "dangling_edges": [{"goal_id": u, "missing_target": v}
                           for u, v in dangling],
        "dangling_count": len(dangling),
        "verdict": ("cycles-detected" if cycles else
                    "clean" if goal_index else "skipped-empty-population"),
    }

    if args.output == "json":
        print(json.dumps(payload, indent=2))
    else:
        print(f"[dependency-cycle-check] scanned {payload['goals_scanned']} goals, "
              f"{payload['goals_with_edges']} carry edges "
              f"({payload['edges_total']} edges) -> "
              f"{payload['cycles_found']} cycle(s), "
              f"{payload['dangling_count']} dangling"
              + (" [ARCHIVE DEGRADED]" if archive_degraded else ""))
        for d in detail:
            kind = "SELF-LOOP" if d["self_loop"] else f"{d['length']}-goal ring"
            print(f"  {kind}: " + " -> ".join(
                g["goal_id"] for g in d["goals"]) + f" -> {d['goals'][0]['goal_id']}")
            for g in d["goals"]:
                print(f"      {g['goal_id']} [{g['status']}] {g['title']}")
        for d in payload["dangling_edges"]:
            print(f"  DANGLING: {d['goal_id']} -> {d['missing_target']} "
                  f"(absent from live AND archived queues)")

    return 1 if (cycles and args.exit_on_cycles) else 0


if __name__ == "__main__":
    sys.exit(main())
