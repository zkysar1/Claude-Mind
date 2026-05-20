#!/usr/bin/env python3
"""Phase-cost report — Tier 0 measurement infrastructure.

Reads {agent}/session/execution-diary.jsonl, pairs phase_start/phase_end
records, and emits a per-phase wall-clock cost report. Wall-clock is a proxy
for LLM token cost — throughput is roughly constant, so seconds-per-phase
ranks cost centers accurately enough for tier-extraction decisions.

See plan: ~/.claude/plans/i-had-one-agent-luminous-reddy.md (Tier 0 section).

Output contract:
  JSON to stdout with a `summary` line plus detail arrays. Consumers should
  read the summary for a one-glance status and descend into `phases` only
  when the summary flags an anomaly (see LLM-script handoff constraint in the
  plan: "do NOT emit natural-language reports — that burns savings").

Pairing algorithm:
  Greedy FIFO by phase name (and iteration if both sides pass --iter). For
  each phase_start, matched with the next phase_end of the same phase name.
  Unmatched starts and ends are reported separately — unmatched starts may
  be in-flight (their duration is NOW - start); unmatched ends are data bugs.

Exit codes:
  0  report emitted successfully (report may flag warnings inline)
  1  no diary data (exit cleanly, empty report)
  2  input error (missing flag, bad file path)
"""

import argparse
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from _paths import AGENT_DIR


def _now():
    return datetime.now()


def _parse_ts(entry):
    ts = entry.get("timestamp", "")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _load_markers(diary_path: Path):
    """Yield phase_start/phase_end records in file order, with parsed ts."""
    if not diary_path.exists():
        return []
    out = []
    with open(diary_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = e.get("entry_type")
            if etype not in ("phase_start", "phase_end"):
                continue
            ts = _parse_ts(e)
            if ts is None:
                continue
            e["_ts"] = ts
            out.append(e)
    return out


def _pair_markers(markers, max_pair_seconds=None):
    """Greedy FIFO pairing on phase name (+ iteration if present on both).

    Returns (pairs, unmatched_starts, unmatched_ends, stale_pairs).
      pairs: list of (start_entry, end_entry, duration_seconds)
      unmatched_starts: list of entries never paired (candidates: in_flight)
      unmatched_ends: list of entries never paired (data bug indicators)
      stale_pairs: pairs exceeding max_pair_seconds — likely straddling an
        autocompact / crash where a phase_start escaped emitting phase_end
        and the greedy FIFO matched it to the next iteration's end. Kept
        for debugging but excluded from aggregation.
    """
    open_starts = {}  # key -> list[start_entry], FIFO per key
    pairs = []
    stale_pairs = []
    unmatched_ends = []

    for m in markers:
        phase = m.get("phase", "")
        iteration = m.get("iteration")
        key = (phase, iteration)
        if m["entry_type"] == "phase_start":
            open_starts.setdefault(key, []).append(m)
        else:
            # phase_end — try exact key, then fall back to phase-only if no
            # iteration was on the end record (callers can skip --iter).
            bucket = open_starts.get(key) or open_starts.get((phase, None))
            if bucket:
                start = bucket.pop(0)
                dur = (m["_ts"] - start["_ts"]).total_seconds()
                if max_pair_seconds is not None and dur > max_pair_seconds:
                    stale_pairs.append((start, m, dur))
                else:
                    pairs.append((start, m, dur))
            else:
                unmatched_ends.append(m)

    unmatched_starts = [s for bucket in open_starts.values() for s in bucket]
    return pairs, unmatched_starts, unmatched_ends, stale_pairs


def _aggregate(pairs, unmatched_starts, now):
    """Build per-phase rollup from paired and in-flight durations."""
    # Group durations by phase name.
    by_phase = {}
    for start, _end, dur in pairs:
        phase = start.get("phase", "")
        by_phase.setdefault(phase, []).append(dur)

    in_flight = {}
    for start in unmatched_starts:
        phase = start.get("phase", "")
        age = (now - start["_ts"]).total_seconds()
        in_flight.setdefault(phase, []).append(age)

    phases = []
    for phase, durs in by_phase.items():
        durs_sorted = sorted(durs)
        phases.append({
            "phase": phase,
            "count": len(durs),
            "total_s": round(sum(durs), 3),
            "mean_s": round(statistics.mean(durs), 3),
            "median_s": round(statistics.median(durs), 3),
            "p95_s": round(durs_sorted[max(0, int(len(durs_sorted) * 0.95) - 1)], 3) if durs else 0.0,
            "min_s": round(min(durs), 3),
            "max_s": round(max(durs), 3),
            "in_flight_count": len(in_flight.get(phase, [])),
        })
    phases.sort(key=lambda p: p["total_s"], reverse=True)
    return phases


def _summarize(phases, pair_count, unmatched_start_count, unmatched_end_count):
    if not phases:
        return f"no phase pairs found (unmatched_starts={unmatched_start_count}, unmatched_ends={unmatched_end_count})"
    top = phases[0]
    flags = []
    if unmatched_end_count > 0:
        flags.append(f"unmatched_ends={unmatched_end_count}")
    if unmatched_start_count > 0:
        flags.append(f"in_flight={unmatched_start_count}")
    flag_str = f" [{' '.join(flags)}]" if flags else ""
    return (
        f"{len(phases)} phases, {pair_count} pairs; "
        f"top: {top['phase']} total={top['total_s']}s mean={top['mean_s']}s"
        f"{flag_str}"
    )


def _filter_rolling(markers, rolling_n):
    """If rolling_n set, keep only the last N iterations' worth.

    Heuristic: treat a phase_start where phase == 'phase-0' as an iteration
    boundary. The last `rolling_n` such starts define the window.
    """
    if not rolling_n or rolling_n <= 0:
        return markers
    boundaries = [i for i, m in enumerate(markers)
                  if m["entry_type"] == "phase_start" and m.get("phase", "").startswith("phase-0")]
    if len(boundaries) <= rolling_n:
        return markers
    cutoff_idx = boundaries[-rolling_n]
    return markers[cutoff_idx:]


def main():
    parser = argparse.ArgumentParser(description="Phase-cost report from execution diary")
    parser.add_argument("--diary", type=str, default=None,
                        help="Override diary path (default: {agent}/session/execution-diary.jsonl)")
    parser.add_argument("--rolling", type=int, default=None,
                        help="Restrict to last N iterations (heuristic: phase-0 starts = iteration boundaries)")
    parser.add_argument("--write-report", action="store_true",
                        help="Also write report to {agent}/reports/phase-costs/<timestamp>.json")
    parser.add_argument("--output", choices=["json", "human"], default="json",
                        help="Output format (default: json)")
    parser.add_argument("--max-pair-seconds", type=float, default=600.0,
                        help="Drop pairs exceeding this duration as stale (default: 600s). "
                             "Set 0 to disable filtering. Stale pairs report separately.")
    args = parser.parse_args()

    if args.diary:
        diary_path = Path(args.diary)
    elif AGENT_DIR:
        diary_path = AGENT_DIR / "session" / "execution-diary.jsonl"
    else:
        print("ERROR: no agent bound (MIND_AGENT unset) and no --diary override", file=sys.stderr)
        sys.exit(2)

    markers = _load_markers(diary_path)
    if not markers:
        empty = {
            "summary": "no phase markers in diary (Tier 0 not wired into loop yet?)",
            "phase_count": 0, "pair_count": 0,
            "unmatched_starts": 0, "unmatched_ends": 0,
            "phases": [], "top_5_by_total": [],
            "diary_path": str(diary_path),
        }
        print(json.dumps(empty, ensure_ascii=False))
        sys.exit(1)

    markers = _filter_rolling(markers, args.rolling)
    max_pair = args.max_pair_seconds if args.max_pair_seconds > 0 else None
    pairs, unmatched_starts, unmatched_ends, stale_pairs = _pair_markers(markers, max_pair)
    phases = _aggregate(pairs, unmatched_starts, _now())

    report = {
        "summary": _summarize(phases, len(pairs), len(unmatched_starts), len(unmatched_ends)),
        "phase_count": len(phases),
        "pair_count": len(pairs),
        "unmatched_starts": len(unmatched_starts),
        "unmatched_ends": len(unmatched_ends),
        "stale_pair_count": len(stale_pairs),
        "max_pair_seconds": max_pair,
        "phases": phases,
        "top_5_by_total": phases[:5],
        "diary_path": str(diary_path),
        "generated_at": _now().strftime("%Y-%m-%dT%H:%M:%S"),
        "rolling": args.rolling,
    }

    if args.write_report and AGENT_DIR:
        out_dir = AGENT_DIR / "reports" / "phase-costs"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = _now().strftime("%Y%m%dT%H%M%S")
        out_path = out_dir / f"{stamp}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        report["written_to"] = str(out_path)

    if args.output == "human":
        print(report["summary"])
        print(f"phases: {len(phases)}, pairs: {len(pairs)}, "
              f"unmatched_starts={len(unmatched_starts)}, unmatched_ends={len(unmatched_ends)}")
        print(f"{'phase':<30} {'count':>6} {'total_s':>10} {'mean_s':>10} {'p95_s':>8}")
        for p in phases[:10]:
            print(f"{p['phase']:<30} {p['count']:>6} {p['total_s']:>10.2f} "
                  f"{p['mean_s']:>10.3f} {p['p95_s']:>8.3f}")
    else:
        print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
