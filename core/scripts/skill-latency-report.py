#!/usr/bin/env python3
"""skill-latency-report.py — per-skill latency percentiles from skill-invocations.jsonl.

Layer 5 of the skill-telemetry signal repair (g-304-13). Read-only by default.

WHY derived, not logged: PostToolUse does NOT fire for the Skill tool (it injects
content into the conversation stream rather than returning a tool result), so a true
skill-END event cannot be captured on the PreToolUse[Skill] hot path
(context-reads-skill-gate.sh is annotated IRREDUCIBLY LOCAL — latency-budgeted —
so it stays a pure append, no back-stamp). The standard latency proxy for a
start-only event log is the inter-event delta:

    duration[i] = ts[i+1] - ts[i]   (consecutive records within one (agent, session))

Semantics + caveat (reported honestly, not hidden):
- LEAF skill (invokes no sub-skill): duration == wall-clock execution time.
- PARENT skill (invokes sub-skills): the delta measures time-to-first-sub-skill —
  a LOWER BOUND on the parent's total duration, because the sub-skill's start is the
  next record. Nesting depth is not in the records, so the report cannot separate the
  two cases; it reports inter-skill-start latency and labels it as such.
- The LAST record in a session has no successor -> open interval, excluded.
- An inter-start gap exceeding --max-gap-seconds (default 1800s = 30min) is treated
  as a session-idle boundary (turn gap / agent paused) and excluded, so a skill that
  happened to precede a long pause does not inflate p95/max.

Usage:
  py -3 core/scripts/skill-latency-report.py                 # text report (all agents)
  py -3 core/scripts/skill-latency-report.py --json          # machine-readable
  py -3 core/scripts/skill-latency-report.py --agent delta   # one agent only
  py -3 core/scripts/skill-latency-report.py --min-count 5   # only skills with >=5 samples
  py -3 core/scripts/skill-latency-report.py --max-gap-seconds 900
  py -3 core/scripts/skill-latency-report.py --backfill      # persist duration_seconds
                                                             # into the BOUND agent's OWN file

--backfill writes a derived `duration_seconds` field onto each record of the bound
agent's own skill-invocations.jsonl (the schema extension named in g-304-13). It is
idempotent (recomputes from ts every run), own-file-only (never a cross-agent write),
off the hot path (run on demand), and additive (consumers read records via
json.loads + .get(), so the new field is ignored by skill-attribution.py etc.).
"""
import os
import sys
import json
import math
import argparse
import datetime
from collections import defaultdict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import _paths  # noqa: E402

INVOCATION_FILE = "skill-invocations.jsonl"
DEFAULT_MAX_GAP_SECONDS = 1800  # 30 min — beyond this is a session-idle boundary, not skill latency


def _parse_ts(value):
    """Parse an ISO-8601 local timestamp ('2026-06-12T13:08:49'); None on failure."""
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _percentile(vals_sorted, p):
    """Linear-interpolation percentile. vals_sorted must be ascending. p in [0,1]."""
    if not vals_sorted:
        return None
    n = len(vals_sorted)
    if n == 1:
        return vals_sorted[0]
    k = (n - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return vals_sorted[int(k)]
    return vals_sorted[f] + (vals_sorted[c] - vals_sorted[f]) * (k - f)


def _read_rows(path, agent_name):
    """Read one skill-invocations.jsonl; return rows tagged with agent + line index."""
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as f:
        for idx, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            row["_agent"] = agent_name
            row["_line"] = idx
            rows.append(row)
    return rows


def _agent_files(agent_filter=None):
    """Yield (agent_name, path) for every agent's skill-invocations.jsonl.

    Canonical cross-agent glob (agents_root().glob('*/...')) — auto-tracks an
    AGENTS_PARENT_DIR rename. Do NOT re-derive via os.listdir(PROJECT_ROOT) (that
    is the depth-1 drift bug class — see CLAUDE.md Agent-dir Resolution).
    """
    root = _paths.agents_root()
    for conf in sorted(root.glob("*/local-paths.conf")):
        agent_name = conf.parent.name
        if agent_filter and agent_name != agent_filter:
            continue
        yield agent_name, str(conf.parent / INVOCATION_FILE)


def _durations_for_session(rows_sorted, max_gap):
    """Given one (agent, sid) session's rows sorted by ts, yield (row, duration_seconds).

    duration = next-record ts - this-record ts, when both parse and the gap is in
    (0, max_gap]. The last row and over-gap rows yield duration None (open/idle).
    """
    parsed = [(_parse_ts(r.get("ts")), r) for r in rows_sorted]
    for i, (ts_i, row) in enumerate(parsed):
        dur = None
        if ts_i is not None and i + 1 < len(parsed):
            ts_next = parsed[i + 1][0]
            if ts_next is not None:
                delta = (ts_next - ts_i).total_seconds()
                if 0 <= delta <= max_gap:
                    dur = delta
        yield row, dur


def collect(agent_filter, max_gap):
    """Return (per_skill durations dict, total_rows, total_paired)."""
    all_rows = []
    for agent_name, path in _agent_files(agent_filter):
        all_rows.extend(_read_rows(path, agent_name))

    # Group by (agent, sid) so durations never cross an agent or a session.
    sessions = defaultdict(list)
    for r in all_rows:
        sessions[(r.get("_agent", ""), r.get("sid", ""))].append(r)

    per_skill = defaultdict(list)
    total_paired = 0
    for key, rows in sessions.items():
        rows_sorted = sorted(rows, key=lambda r: (r.get("ts", ""), r.get("_line", 0)))
        for row, dur in _durations_for_session(rows_sorted, max_gap):
            if dur is None:
                continue
            skill = row.get("skill", "")
            if not skill:
                continue
            per_skill[skill].append(dur)
            total_paired += 1
    return per_skill, len(all_rows), total_paired


def summarize(per_skill, min_count):
    """Compute per-skill stats; return list of dicts sorted by p95 desc."""
    out = []
    for skill, durs in per_skill.items():
        if len(durs) < min_count:
            continue
        s = sorted(durs)
        out.append({
            "skill": skill,
            "count": len(s),
            "p50_seconds": round(_percentile(s, 0.50), 2),
            "p95_seconds": round(_percentile(s, 0.95), 2),
            "max_seconds": round(s[-1], 2),
            "mean_seconds": round(sum(s) / len(s), 2),
        })
    out.sort(key=lambda d: d["p95_seconds"], reverse=True)
    return out


def backfill_bound_agent(max_gap):
    """Persist derived duration_seconds into the BOUND agent's own ledger.

    Own-file-only (never cross-agent), idempotent, off the hot path.
    """
    agent_dir = getattr(_paths, "AGENT_DIR", "") or ""
    if not agent_dir:
        return {"error": "no bound agent (AGENT_DIR empty) — run with MIND_AGENT set"}
    path = os.path.join(str(agent_dir), INVOCATION_FILE)
    if not os.path.exists(path):
        return {"error": f"no {INVOCATION_FILE} for bound agent at {path}"}

    agent_name = os.path.basename(str(agent_dir).rstrip("/\\"))
    rows = _read_rows(path, agent_name)
    sessions = defaultdict(list)
    for r in rows:
        sessions[r.get("sid", "")].append(r)

    line_to_dur = {}
    for sid, srows in sessions.items():
        srows_sorted = sorted(srows, key=lambda r: (r.get("ts", ""), r.get("_line", 0)))
        for row, dur in _durations_for_session(srows_sorted, max_gap):
            line_to_dur[row["_line"]] = dur

    updated = 0
    out_lines = []
    for r in sorted(rows, key=lambda r: r["_line"]):
        dur = line_to_dur.get(r["_line"])
        clean = {k: v for k, v in r.items() if not k.startswith("_")}
        clean["duration_seconds"] = round(dur, 2) if dur is not None else None
        out_lines.append(json.dumps(clean))
        updated += 1

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines) + ("\n" if out_lines else ""))
    os.replace(tmp, path)
    filled = sum(1 for ln in line_to_dur.values() if ln is not None)
    return {"agent": agent_name, "records": updated, "durations_filled": filled, "path": path}


def main():
    ap = argparse.ArgumentParser(description="Per-skill latency percentiles from skill-invocations.jsonl")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--agent", default=None, help="restrict to one agent")
    ap.add_argument("--min-count", type=int, default=1, help="only skills with >= N paired samples")
    ap.add_argument("--max-gap-seconds", type=float, default=DEFAULT_MAX_GAP_SECONDS,
                    help="inter-start gaps above this are session-idle boundaries (excluded)")
    ap.add_argument("--backfill", action="store_true",
                    help="persist derived duration_seconds into the BOUND agent's own ledger")
    args = ap.parse_args()

    if args.backfill:
        res = backfill_bound_agent(args.max_gap_seconds)
        print(json.dumps(res, indent=2))
        return 0 if "error" not in res else 1

    per_skill, total_rows, total_paired = collect(args.agent, args.max_gap_seconds)
    report = summarize(per_skill, args.min_count)

    if args.json:
        print(json.dumps({
            "total_records": total_rows,
            "total_paired_samples": total_paired,
            "max_gap_seconds": args.max_gap_seconds,
            "skills": report,
        }, indent=2))
        return 0

    if not report:
        print(f"No latency samples (records={total_rows}, paired={total_paired}). "
              f"Need >=2 skill invocations in a session within {args.max_gap_seconds}s.")
        return 0
    print(f"Skill latency (inter-skill-start delta; {total_paired} paired samples "
          f"from {total_rows} records; gap cap {args.max_gap_seconds:.0f}s)")
    print(f"{'skill':<34} {'n':>5} {'p50':>8} {'p95':>8} {'max':>9} {'mean':>8}")
    print("-" * 76)
    for r in report:
        print(f"{r['skill']:<34} {r['count']:>5} {r['p50_seconds']:>8.2f} "
              f"{r['p95_seconds']:>8.2f} {r['max_seconds']:>9.2f} {r['mean_seconds']:>8.2f}")
    print("\nNote: duration = time from a skill's start to the NEXT skill's start in the "
          "same\nsession (PostToolUse does not fire for Skill). Leaf skills => execution "
          "time;\nparent skills that invoke sub-skills => lower bound (time to first sub-skill).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
