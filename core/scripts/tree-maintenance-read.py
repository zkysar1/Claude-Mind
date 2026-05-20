#!/usr/bin/env python3
"""Read tool for world/tree-maintenance-log.jsonl.

Modes:
  --since Nd            Entries from the last N days (N=1 means 24h ago → now).
  --run-id ID           The single record with that run_id.
  --aggregate           Sum candidates/actioned/skipped across records in the
                        window. Works alone (all-time) or combined with --since.

Output:
  --human (default)     Compact table / summary text.
  --json                Full JSON payload.

Purpose:
  Consumed by /tree maintain audits and framework reflection to answer
  "why isn't the tree draining?" The per-record `candidates_pre_filter` vs.
  `llm_reported.actioned` delta is the drain-rate signal. Aggregating over a
  window reveals persistent skip-reason clusters (e.g. `insufficient_retrievals`
  dominating distill for many sessions → new nodes aren't accumulating
  retrieval signal fast enough).
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from _paths import WORLD_DIR

LOG_PATH = WORLD_DIR / "tree-maintenance-log.jsonl"

# All maintenance phases the LLM pseudocode may act on. Only the first three
# have Python-side pre-filter candidate discovery (see tree.py get_*_candidates);
# the remaining five are LLM-driven actions that appear only in llm_reported.
_ALL_PHASES = ("decompose", "distill", "redistribute",
               "split", "sprout", "merge", "prune", "retire")
_PRE_FILTER_PHASES = ("decompose", "distill", "redistribute")


def _parse_ts(s):
    """Parse ISO-8601 local timestamp. Returns None on failure."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _read_records():
    if not LOG_PATH.exists():
        return []
    records = []
    with open(LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                # Corrupt line — skip silently. The log is append-only, so a
                # bad line means a truncated write; ignore it rather than
                # poison the whole aggregate.
                continue
    return records


def _filter_since(records, days):
    cutoff = datetime.now() - timedelta(days=days)
    kept = []
    for rec in records:
        ts = _parse_ts(rec.get("ended_at") or rec.get("started_at"))
        if ts is None:
            continue
        if ts >= cutoff:
            kept.append(rec)
    return kept


def _tally_skip_reason(agg_phase, reason, n):
    """Add `n` to agg_phase['skipped_by_reason_total'][reason] when numeric,
    else stash the string sentinel under agg_phase['_sentinels'][reason] so
    visibility is preserved instead of crashing int(n).

    g-115-882 / g-115-872: llm_reported.<phase>.skipped_by_reason may carry
    string sentinels like "all" (e.g. not_inspected_this_run: all,
    none_eligible: all). The prior `int(n or 0)` form raised
    ValueError: invalid literal for int() with base 10: 'all' and crashed
    --aggregate. _sentinels keeps a per-(reason, value) occurrence count so
    aggregate readers can still see how often each sentinel appeared.

    isinstance(n, bool) is excluded from the numeric path because bool is
    an int subclass in Python — True/False would otherwise count as 1/0
    against a numeric skip-reason, which is misleading.
    """
    if isinstance(n, (int, float)) and not isinstance(n, bool):
        agg_phase["skipped_by_reason_total"][reason] = (
            agg_phase["skipped_by_reason_total"].get(reason, 0) + int(n)
        )
    else:
        sentinels = agg_phase.setdefault("_sentinels", {})
        sentinels.setdefault(reason, {})
        key = str(n)
        sentinels[reason][key] = sentinels[reason].get(key, 0) + 1


def _aggregate(records):
    """Aggregate counts across records. Shape matches per-record schema so
    downstream consumers can compare a single run to the aggregate directly.
    """
    agg = {
        "run_count": len(records),
        "window": {"first": None, "last": None},
        "candidates_pre_filter": {},
        "llm_reported": {},
        "post_run_debt_trend": [],
    }
    # Only the pre-filter phases get a pre_filter block — the LLM-only phases
    # (split/sprout/merge/prune/retire) have no Python-side candidate discovery.
    for phase in _PRE_FILTER_PHASES:
        agg["candidates_pre_filter"][phase] = {
            "candidates_in_total": 0,
            "skipped_by_reason_total": {},
        }
    for phase in _ALL_PHASES:
        agg["llm_reported"][phase] = {
            "actioned_total": 0,
            "skipped_by_reason_total": {},
        }

    first_ts = None
    last_ts = None
    for rec in records:
        ts = _parse_ts(rec.get("started_at"))
        if ts is not None:
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts

        pre = rec.get("candidates_pre_filter") or {}
        for phase in _PRE_FILTER_PHASES:
            block = pre.get(phase) or {}
            agg_phase = agg["candidates_pre_filter"][phase]
            agg_phase["candidates_in_total"] += int(block.get("candidates_in", 0) or 0)
            for reason, n in (block.get("skipped_by_reason") or {}).items():
                _tally_skip_reason(agg_phase, reason, n)

        llm = rec.get("llm_reported") or {}
        for phase in _ALL_PHASES:
            block = llm.get(phase) or {}
            if not isinstance(block, dict):
                continue
            agg_phase = agg["llm_reported"][phase]
            agg_phase["actioned_total"] += int(block.get("actioned", 0) or 0)
            for reason, n in (block.get("skipped_by_reason") or {}).items():
                _tally_skip_reason(agg_phase, reason, n)
            # Phase-specific follow-on fields — sum when numeric.
            for extra_key in ("children_created", "bytes_removed"):
                if extra_key in block and isinstance(block[extra_key], (int, float)):
                    agg_phase[extra_key + "_total"] = (
                        agg_phase.get(extra_key + "_total", 0) + block[extra_key]
                    )

        debt = rec.get("post_run_debt") or {}
        agg["post_run_debt_trend"].append({
            "run_id": rec.get("run_id"),
            "ended_at": rec.get("ended_at"),
            "total": debt.get("total"),
            "cleared": debt.get("cleared"),
        })

    if first_ts:
        agg["window"]["first"] = first_ts.isoformat(timespec="seconds")
    if last_ts:
        agg["window"]["last"] = last_ts.isoformat(timespec="seconds")

    return agg


def _fmt_human_record(rec):
    lines = []
    lines.append("run_id: " + str(rec.get("run_id")))
    lines.append("agent: " + str(rec.get("agent", "")))
    lines.append("mode: " + str(rec.get("mode", "")))
    lines.append("window: " + str(rec.get("started_at", "")) + " → " + str(rec.get("ended_at", "")))
    pre = rec.get("candidates_pre_filter") or {}
    llm = rec.get("llm_reported") or {}
    lines.append("phase                 cand_in  actioned  skip_reasons (pre|llm)")
    for phase in _ALL_PHASES:
        pre_block = pre.get(phase) or {}
        cand_in = pre_block.get("candidates_in", "—")
        pre_reasons = pre_block.get("skipped_by_reason") or {}
        # : llm.get(phase) can be an int (older record schema stored
        # actioned-count directly under the phase key, not in a nested dict).
        # `or {}` only catches falsy values; int 5 passes through and
        # 5.get(...) crashes. Type-guard to a dict before .get().
        block = llm.get(phase)
        if isinstance(block, dict):
            actioned = block.get("actioned", "—")
            llm_reasons = block.get("skipped_by_reason") or {}
        else:
            actioned = block if block is not None else "—"
            llm_reasons = {}
        # : mirror _fmt_human_aggregate's dual-source display
        # (lines 215-219). The per-record view previously dropped
        # pre-filter skip reasons entirely; now both sides are shown
        # with `pre:` / `llm:` prefixes so the reader can tell which
        # phase of the maintenance pipeline rejected each candidate.
        reasons_fmt = []
        for k, v in pre_reasons.items():
            reasons_fmt.append("pre:{}={}".format(k, v))
        for k, v in llm_reasons.items():
            reasons_fmt.append("llm:{}={}".format(k, v))
        skipped_fmt = ", ".join(reasons_fmt) or "—"
        lines.append("  {:<20}{:>7}  {:>8}  {}".format(
            phase, str(cand_in), str(actioned), skipped_fmt,
        ))
    debt = rec.get("post_run_debt") or {}
    lines.append("post_run_debt: total={} cleared={} threshold={}".format(
        debt.get("total"), debt.get("cleared"), debt.get("threshold"),
    ))
    return "\n".join(lines)


def _fmt_human_aggregate(agg):
    lines = []
    lines.append("runs: {}".format(agg["run_count"]))
    w = agg["window"]
    lines.append("window: {} → {}".format(w.get("first") or "—", w.get("last") or "—"))
    lines.append("")
    lines.append("phase                 cand_in_total  actioned_total  top_skip_reasons")
    for phase in _ALL_PHASES:
        pre = (agg["candidates_pre_filter"].get(phase) or {})
        llm = (agg["llm_reported"].get(phase) or {})
        cand_in = pre.get("candidates_in_total", 0)
        actioned = llm.get("actioned_total", 0)
        pre_reasons = pre.get("skipped_by_reason_total") or {}
        llm_reasons = llm.get("skipped_by_reason_total") or {}
        # Combine pre-filter and LLM-reported skip reasons in the summary,
        # prefixed to show which side they came from.
        top = []
        for reason, n in sorted(pre_reasons.items(), key=lambda kv: -kv[1])[:3]:
            top.append("pre:{}={}".format(reason, n))
        for reason, n in sorted(llm_reasons.items(), key=lambda kv: -kv[1])[:3]:
            top.append("llm:{}={}".format(reason, n))
        lines.append("  {:<20}{:>14}  {:>14}  {}".format(
            phase, cand_in, actioned, ", ".join(top) or "—",
        ))
    lines.append("")
    lines.append("post_run_debt trend (oldest → newest):")
    for d in agg["post_run_debt_trend"]:
        lines.append("  {}  total={}  cleared={}".format(
            d.get("ended_at") or "—", d.get("total"), d.get("cleared"),
        ))
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Read world/tree-maintenance-log.jsonl",
    )
    mode_group = parser.add_mutually_exclusive_group(required=False)
    mode_group.add_argument("--run-id", type=str,
                            help="Single record matching run_id")
    parser.add_argument("--since", type=str, default=None,
                        help="Window: Nd (days). Combines with --aggregate.")
    parser.add_argument("--aggregate", action="store_true",
                        help="Aggregate counts across records (respects --since).")
    parser.add_argument("--json", action="store_true",
                        help="Emit raw JSON instead of human-readable text.")
    args = parser.parse_args()

    records = _read_records()

    # --since filter
    if args.since:
        s = args.since.strip().lower()
        if not s.endswith("d"):
            print("Error: --since expects Nd (e.g. 1d, 7d).", file=sys.stderr)
            sys.exit(1)
        try:
            days = int(s[:-1])
        except ValueError:
            print("Error: --since N must be an integer (got {!r}).".format(args.since),
                  file=sys.stderr)
            sys.exit(1)
        records = _filter_since(records, days)

    if args.run_id:
        found = [r for r in records if r.get("run_id") == args.run_id]
        if not found:
            if args.json:
                print("null")
            else:
                print("No record with run_id={}".format(args.run_id))
            sys.exit(1)
        rec = found[0]
        if args.json:
            print(json.dumps(rec, indent=2, ensure_ascii=False))
        else:
            print(_fmt_human_record(rec))
        return

    if args.aggregate:
        agg = _aggregate(records)
        if args.json:
            print(json.dumps(agg, indent=2, ensure_ascii=False))
        else:
            print(_fmt_human_aggregate(agg))
        return

    # Default: list all records in window (most recent first)
    records_sorted = sorted(
        records,
        key=lambda r: _parse_ts(r.get("ended_at") or r.get("started_at")) or datetime.min,
        reverse=True,
    )
    if args.json:
        print(json.dumps(records_sorted, indent=2, ensure_ascii=False))
    else:
        if not records_sorted:
            print("No tree-maintenance records.")
            return
        for i, rec in enumerate(records_sorted):
            if i > 0:
                print("─" * 60)
            print(_fmt_human_record(rec))


if __name__ == "__main__":
    main()
