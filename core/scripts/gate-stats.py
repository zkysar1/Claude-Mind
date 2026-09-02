#!/usr/bin/env python3
"""Gate audit dashboard — Phase 6 of the gate audit/retirement plan.

Passive read-only visibility into gate telemetry. Complements
gate-retirement-eval.py (which prescribes actions) with a description-only
view: counts, ratios, trigger histograms, bulk-override correlations,
recent fail_open events. No rules, no recommendations, no thresholds.

Use when:
  - You want to SEE what's happening without acting on it
  - You're sanity-checking the evaluator's recommendations against raw data
  - You're investigating a fail_open the evaluator surfaced
  - You're wondering which trigger pattern fires the most
  - You want to know whether bulk-override ledger entries correlate with
    actual gate-side decision=override firings (audit trail integrity)

Usage:
  py -3 core/scripts/gate-stats.py [--days N] [--top K] [--gate <id>]
                                   [--output json|human]

  --days N       Look-back window in days (default 30).
  --top K        Top-K trigger histogram entries to show (default 10).
  --gate <id>    Restrict output to a single gate id.
  --output       json (default) or human-readable dashboard.

Section glossary (human output):
  Overview               window stats + unique gate/agent counts
  Per-gate decisions     all 5 decisions (noop/pass/block/override/fail_open) per gate
  Override rates         override / (block+override) per gate with non-zero denominators
  Trigger histogram      top-K most-matched trigger strings across all gates
  Bulk-override audit    correlate world/override-bypass-ledger.jsonl tokens to
                         gate-firings decision=override entries (uses override_reason
                         field whose hash IS the token, capturing blast radius reality)
  Recent fail_open       gate_error strings from any decision=fail_open firings

Contract: never raises. Malformed records skipped silently with a stderr WARN.
"""

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from _paths import META_DIR, WORLD_DIR
from _gate_log import firings_paths

FIRINGS_JSONL = META_DIR / "gate-firings.jsonl"
LEDGER_JSONL = WORLD_DIR / "override-bypass-ledger.jsonl"


def _load_records(path, since):
    """Yield JSONL records with ts >= since. Skip malformed lines."""
    if not Path(path).is_file():
        return
    skipped = 0
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            skipped += 1
            continue
        # A line can PARSE and still not be a record: a bare int/list/str is
        # valid JSON, so json.loads does not raise and the next .get() dies with
        # AttributeError, taking the whole dashboard down over one bad row —
        # which is what "skip malformed lines" above already promised not to do.
        # Measured 2026-08-30: meta/gate-firings-2026-08-19.jsonl line 1 is `7`,
        # and this script crashed on every invocation because of it. The
        # identical guard was added to override-ledger-consume.py's twin loader
        # on 2026-08-29 and NOT swept to this one (guard-1710 class: a fix
        # applied to one of two consumers). guard-1512 is the general form — one
        # malformed record must never abort a whole-store walk; guard-5469 is
        # the write-side twin.
        if not isinstance(rec, dict):
            skipped += 1
            continue
        ts_raw = rec.get("ts")
        if not isinstance(ts_raw, str):
            skipped += 1
            continue
        try:
            ts = datetime.strptime(ts_raw, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            skipped += 1
            continue
        if ts >= since:
            yield rec
    if skipped:
        print(f"[gate-stats] WARN: skipped {skipped} malformed record(s) "
              f"in {path.name}", file=sys.stderr)


def _build_stats(firings, ledger_records, gate_filter=None):
    """Aggregate firings into stat sections. Returns dict."""
    decisions_per_gate = defaultdict(Counter)
    triggers_per_gate = defaultdict(Counter)
    triggers_global = Counter()
    fail_open_records = []
    agents = set()
    earliest = None
    latest = None
    total = 0

    for rec in firings:
        gid = rec.get("gate_id")
        if not gid:
            continue
        if gate_filter and gid != gate_filter:
            continue
        decision = rec.get("decision", "noop")
        decisions_per_gate[gid][decision] += 1
        trigger = rec.get("trigger_matched")
        if trigger:
            triggers_per_gate[gid][trigger] += 1
            triggers_global[trigger] += 1
        if decision == "fail_open":
            fail_open_records.append({
                "ts": rec.get("ts"),
                "gate_id": gid,
                "caller": rec.get("caller"),
                "gate_error": rec.get("gate_error"),
            })
        ag = rec.get("agent")
        if ag:
            agents.add(ag)
        ts_raw = rec.get("ts")
        if ts_raw:
            if earliest is None or ts_raw < earliest:
                earliest = ts_raw
            if latest is None or ts_raw > latest:
                latest = ts_raw
        total += 1

    # Override-rate per gate (numerator: override; denominator: block+override).
    override_rates = {}
    for gid, counts in decisions_per_gate.items():
        bo = counts.get("block", 0) + counts.get("override", 0)
        if bo > 0:
            override_rates[gid] = {
                "block": counts.get("block", 0),
                "override": counts.get("override", 0),
                "total_block_or_override": bo,
                "rate": round(counts.get("override", 0) / bo, 3),
            }

    # Bulk-override correlation: for each ledger record's token, count how
    # many decision=override firings within the window carry the SAME hash
    # (computed from override_reason). If the slots_filled field promised N
    # gates would receive the override, this should match (or be close to)
    # the number of decision=override firings tagged with that token.
    #
    # IMPORTANT: a ∅ marker here can mean EITHER (a) the bulk override was
    # supplied but no per-gate gate actually fired (ledger-on-intent
    # captured the supply, gates were noop on this run), OR (b) the
    # downstream gates are in `pending_audit` (uninstrumented) and emit no
    # firing records at all. Per gates.yaml at time of writing,
    # aspirations.py's two slots (origin-signal-gate, goal-duplication-gate)
    # are still in pending_audit — those bulk overrides will ALWAYS show
    # ∅ until those gates are instrumented. Don't read ∅ as "bug"; check
    # gates.yaml first.
    # Pre-index decision=override firings by override_reason hash to keep
    # the correlation loop O(L) instead of O(L*F). Done once up front; each
    # ledger record then does a dict lookup. Hash collision on 48 bits at
    # agent-scale is statistically impossible (well under birthday bound).
    firings_by_token = defaultdict(Counter)
    for rec in firings:
        if rec.get("decision") != "override":
            continue
        ovr = rec.get("override_reason") or ""
        ovr_hash = hashlib.sha1(ovr.encode("utf-8", errors="replace")
                                ).hexdigest()[:12]
        firings_by_token[ovr_hash][rec.get("gate_id", "unknown")] += 1

    correlation = []
    for lr in ledger_records:
        token = lr.get("override_token")
        justification = lr.get("justification") or ""
        if not token:
            continue
        matched_gates = firings_by_token.get(token, Counter())
        correlation.append({
            "ts": lr.get("ts"),
            "token": token,
            "justification_preview": justification[:80],
            "slots_filled_promised": lr.get("slots_filled", []),
            "ledger_blast_radius": len(lr.get("slots_filled", [])),
            "actual_override_firings_matched": dict(matched_gates),
            "actual_count": sum(matched_gates.values()),
        })

    return {
        "overview": {
            "total_firings": total,
            "earliest_ts": earliest,
            "latest_ts": latest,
            "unique_gates_seen": len(decisions_per_gate),
            "unique_agents_seen": sorted(agents),
        },
        "decisions_per_gate": {gid: dict(c) for gid, c
                               in decisions_per_gate.items()},
        "override_rates": override_rates,
        "triggers_per_gate": {gid: dict(c.most_common(5)) for gid, c
                              in triggers_per_gate.items()},
        "triggers_global_top": dict(triggers_global.most_common(10)),
        "bulk_override_correlation": correlation,
        "fail_open_records": fail_open_records,
    }


def _human_dashboard(stats, top_k):
    out = []
    o = stats["overview"]
    out.append("=" * 72)
    out.append("GATE STATS DASHBOARD")
    out.append("=" * 72)
    out.append(f"Window:   {o['earliest_ts']}  →  {o['latest_ts']}")
    out.append(f"Total firings: {o['total_firings']}")
    out.append(f"Unique gates:  {o['unique_gates_seen']}")
    out.append(f"Agents seen:   {', '.join(o['unique_agents_seen']) or '(none)'}")
    out.append("")

    # Per-gate decisions
    out.append("─── Per-gate decision counts ───")
    out.append(f"{'GATE ID':<32s} {'NOOP':>6s} {'PASS':>5s} {'BLK':>5s} "
               f"{'OVR':>5s} {'FAIL':>5s} {'TOTAL':>6s}")
    out.append("-" * 70)
    rows = sorted(stats["decisions_per_gate"].items(),
                  key=lambda x: -sum(x[1].values()))
    for gid, c in rows:
        total = sum(c.values())
        out.append(f"{gid:<32s} {c.get('noop', 0):>6d} {c.get('pass', 0):>5d} "
                   f"{c.get('block', 0):>5d} {c.get('override', 0):>5d} "
                   f"{c.get('fail_open', 0):>5d} {total:>6d}")
    out.append("")

    # Override rates
    out.append("─── Override rates (only gates with block+override > 0) ───")
    if not stats["override_rates"]:
        out.append("  (none — no blocking decisions in window)")
    else:
        out.append(f"{'GATE ID':<32s} {'BLOCK':>6s} {'OVR':>5s} {'B+O':>5s} {'RATE':>7s}")
        out.append("-" * 60)
        for gid, r in sorted(stats["override_rates"].items(),
                             key=lambda x: -x[1]["rate"]):
            out.append(f"{gid:<32s} {r['block']:>6d} {r['override']:>5d} "
                       f"{r['total_block_or_override']:>5d} "
                       f"{r['rate']:>6.0%}")
    out.append("")

    # Trigger histogram
    out.append(f"─── Top {top_k} trigger strings (across all gates) ───")
    top = list(stats["triggers_global_top"].items())[:top_k]
    if not top:
        out.append("  (no trigger strings in window)")
    else:
        for trig, n in top:
            out.append(f"  {n:>4d}×  {trig!r}")
    out.append("")

    # Bulk-override correlation
    out.append("─── Bulk-override audit (ledger ↔ gate firings) ───")
    if not stats["bulk_override_correlation"]:
        out.append("  (no bulk overrides in window)")
    else:
        out.append("  Each row: did the ledger's promised blast radius match the actual")
        out.append("  decision=override firings tagged with the same justification hash?")
        out.append("")
        for c in stats["bulk_override_correlation"]:
            promise = c["ledger_blast_radius"]
            actual = c["actual_count"]
            mark = "✓" if actual >= promise else ("∅" if actual == 0 else "△")
            out.append(f"  [{mark}] {c['ts']}  token={c['token']}  "
                       f"promised={promise}  actual={actual}")
            out.append(f"        justification: {c['justification_preview']}")
            if c["actual_override_firings_matched"]:
                gates_matched = ", ".join(
                    f"{g}({n})" for g, n
                    in sorted(c["actual_override_firings_matched"].items()))
                out.append(f"        matched gates: {gates_matched}")
        out.append("")
        out.append("  Legend: ✓ ledger matched gate-side reality; △ partial; "
                   "∅ ledger record but zero gate-side override firings "
                   "(bulk override supplied but no gate consumed it)")
    out.append("")

    # Recent fail_open
    out.append("─── Recent fail_open events (gate had a bug) ───")
    if not stats["fail_open_records"]:
        out.append("  (none — no gate raised an exception in window)")
    else:
        for f in stats["fail_open_records"][-10:]:
            out.append(f"  {f['ts']}  {f['gate_id']}")
            out.append(f"      caller: {f['caller']}")
            out.append(f"      error:  {(f['gate_error'] or '')[:120]}")
    out.append("")
    out.append("=" * 72)
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Read-only gate telemetry dashboard. Complements "
                    "gate-retirement-eval.py with passive visibility — "
                    "counts, ratios, trigger histograms, audit correlations.")
    ap.add_argument("--days", type=int, default=30,
                    help="Look-back window in days (default 30).")
    ap.add_argument("--top", type=int, default=10,
                    help="Top-K trigger histogram entries (default 10).")
    ap.add_argument("--gate", default=None,
                    help="Restrict to a single gate id.")
    ap.add_argument("--output", default="json", choices=["json", "human"],
                    help="Output format (default json).")
    args = ap.parse_args(argv)

    since = datetime.now() - timedelta(days=args.days)
    # : read through the store's composition seam (see _gate_log
    # .firings_paths) rather than a hardcoded filename, so date segments are
    # picked up with no change here. Today this resolves to exactly
    # FIRINGS_JSONL, so output is byte-identical.
    firings = [r for p in firings_paths(FIRINGS_JSONL.parent)
               for r in _load_records(p, since)]
    ledger = list(_load_records(LEDGER_JSONL, since))
    stats = _build_stats(firings, ledger, gate_filter=args.gate)

    if args.output == "human":
        print(_human_dashboard(stats, args.top))
    else:
        print(json.dumps({
            "window_days": args.days,
            "evaluated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            **stats,
        }, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
