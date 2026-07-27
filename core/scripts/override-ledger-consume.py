#!/usr/bin/env python3
"""Override-bypass-ledger consumer/analyzer ().

Aggregates `world/override-bypass-ledger.jsonl` entries by gate and by
reason-cluster (simple keyword grouping), producing per-gate override
counts, a ledger-to-firings CONSISTENCY ratio (ledger records vs
`meta/gate-firings.jsonl` decision=override count — a cross-store
audit-trail-integrity signal, NOT a bounded override fraction; can exceed
1.0), top reason clusters, and heuristic threshold-adjustment suggestions.

The BOUNDED override fraction (override / (block + override), in [0,1]) — the
canonical "this gate is over-ridden a lot" metric and the source of the
g-115-603 tighten trigger — lives in gate-retirement-eval.py and gate-stats.py.
This analyzer intentionally does NOT recompute it (single source of truth); the
ratio here answers a different question (are the two override-logging paths
consistent?). See the note at analyze() (g-115-2790).

Distinct from gate-stats.py: that script is a passive descriptive dashboard
across the entire firings + ledger telemetry surface. This script is a
ledger-focused analyzer aimed at "which gates and which reasons need
attention" — feeding downstream automation (gate-retirement-eval, evolution).

Handles BOTH record shapes produced by `_override_helpers.py`:
  - bulk override (audit_bulk_override):
      {slots_filled: [...], gate_ids: [...], gate_ids_unmapped: [...]}
  - cross-lane claim (audit_cross_lane_claim, g-282-07):
      {gate: "<gate-id>", context: {goal_id, intended_agent, agent_claiming, ...}}

Reason-cluster: simple keyword tagging (substring match against a curated
tag table) — TF-IDF reserved for when the ledger has 50+ records. Records
matching multiple tags appear in each cluster.

Suggested threshold adjustments: heuristic — if a single tag accounts for
>=50% of overrides on a single gate (with N>=3), suggest reviewing whether
that pattern is a legitimate bypass or a sign the gate threshold is too
tight.

Usage:
  py -3 core/scripts/override-ledger-consume.py [--days N] [--output json|human]
                                                [--top K] [--gate <id>]

Flags:
  --days N       Look-back window in days (default 30).
  --top K        Top-K reason clusters to show (default 10).
  --gate <id>    Restrict output to a single gate id.
  --output       json (default) or human-readable report.

Contract: never raises. Malformed records skipped with stderr WARN.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from _paths import META_DIR, WORLD_DIR
from _override_helpers import SLOT_TO_GATE_ID

LEDGER_JSONL = WORLD_DIR / "override-bypass-ledger.jsonl"
FIRINGS_JSONL = META_DIR / "gate-firings.jsonl"

# Reason-cluster tag table. Keys are tag names; values are case-insensitive
# substring patterns that match a record's `justification` field. A record
# may match multiple tags. Keep ordered most-specific → most-generic so the
# "primary" tag (first match) tends to be the most informative.
REASON_TAG_PATTERNS = [
    ("smoke-test", [r"smoke[- ]test", r"\bsmoke\b"]),
    ("maintain-primitive", [r"maintain primitive", r"maintain goal", r"in-flight framework"]),
    ("fresh-eyes-review", [r"fresh[- ]eyes", r"adversarial review", r"\bFE[- ]?s\d"]),
    ("out-of-cycle-work", [r"out[- ]of[- ]cycle", r"user[- ]directed assistant"]),
    ("follow-up-goal", [r"follow[- ]up", r"intentional follow", r"alongside g-"]),
    ("false-positive-prose", [r"false[- ]positive prose", r"false[- ]positive match"]),
    ("cross-lane-partner-pto", [r"partner on PTO", r"urgent.*partner"]),
    ("cross-lane-coordination", [r"cross-lane", r"capability-route"]),
    # Clusters observed in the live ledger (, 2026-07-21). The
    # justification field IS populated — the earlier "--tag path unused / 100%
    # untagged" premise was stale; the true cause of the ~95% untagged share was
    # this table lacking patterns for the dominant real reasons. "insight-trigger
    # conversion" alone was ~30% of records and fell through to "untagged".
    ("insight-trigger-conversion", [r"insight[- ]trigger conversion"]),
    ("arc-planning", [r"validated arc\b", r"arc task/action boundary"]),
    ("tree-contradiction-audit", [r"tree contradiction audit"]),
    ("stale-claim-takeback", [r"stale[- ]claim take[- ]?back", r"intended_agent stale", r"dormant.*reallocation"]),
    ("handoff-routing", [r"handoff_to="]),
    ("test-fixture", [r"test[- ]fixture", r"test justification"]),
]


def _load_ledger(path: Path, since: datetime):
    """Yield ledger records with ts >= since. Skip malformed lines silently."""
    if not path.is_file():
        return
    skipped = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
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
        if ts < since:
            continue
        yield rec
    if skipped:
        print(f"[override-ledger-consume] WARN: skipped {skipped} malformed line(s) in {path}",
              file=sys.stderr)


def _load_firings_override_counts(path: Path, since: datetime):
    """Count decision=override firings per gate from meta/gate-firings.jsonl.

    Used as the denominator for override-rate. Returns {gate_id: count}.
    Returns empty dict if file missing — rate will be undefined for that gate.
    """
    counts: Counter[str] = Counter()
    if not path.is_file():
        return counts
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        ts_raw = rec.get("ts")
        if not isinstance(ts_raw, str):
            continue
        try:
            ts = datetime.strptime(ts_raw, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
        if ts < since:
            continue
        if rec.get("decision") != "override":
            continue
        gate = rec.get("gate_id") or rec.get("gate")
        if isinstance(gate, str) and gate:
            counts[gate] += 1
    return counts


def _record_to_gate_ids(rec: dict) -> list[str]:
    """Extract canonical gate ids from a ledger record (handles both shapes).

    bulk override: rec.gate_ids is a list (canonical, post-g-281-01).
      Older bulk records lack gate_ids — fall back to mapping rec.slots_filled
      via SLOT_TO_GATE_ID so legacy records still aggregate per-gate. Unmapped
      slots are tagged with a `slot:` prefix so they remain visible rather
      than dropping into an "unknown" bucket.
    cross-lane: rec.gate is a single string.
    """
    out: list[str] = []
    gate_ids = rec.get("gate_ids")
    if isinstance(gate_ids, list) and gate_ids:
        out.extend(g for g in gate_ids if isinstance(g, str))
    else:
        # Legacy bulk record (pre-gate_ids enrichment) — map slots_filled.
        slots = rec.get("slots_filled")
        if isinstance(slots, list):
            for slot in slots:
                if not isinstance(slot, str):
                    continue
                gid = SLOT_TO_GATE_ID.get(slot)
                if gid:
                    out.append(gid)
                else:
                    out.append(f"slot:{slot}")
    unmapped = rec.get("gate_ids_unmapped")
    if isinstance(unmapped, list):
        out.extend(f"slot:{s}" for s in unmapped if isinstance(s, str))
    single = rec.get("gate")
    if isinstance(single, str) and single:
        out.append(single)
    return out


def _tag_record(rec: dict) -> list[str]:
    """Return list of reason-cluster tags matching this record's justification."""
    justification = rec.get("justification") or ""
    if not justification:
        return ["untagged"]
    matches: list[str] = []
    for tag, patterns in REASON_TAG_PATTERNS:
        for pat in patterns:
            if re.search(pat, justification, re.IGNORECASE):
                matches.append(tag)
                break
    return matches or ["untagged"]


def analyze(days: int, gate_filter: str | None, top_k: int):
    """Main aggregation. Returns the JSON-shaped result dict."""
    since = datetime.now() - timedelta(days=days)
    records = list(_load_ledger(LEDGER_JSONL, since))
    firings_override_counts = _load_firings_override_counts(FIRINGS_JSONL, since)

    # Per-gate aggregation (cross-agent — the primary signal for threshold tuning).
    # See world/conventions/override-ledger.md "Cross-Agent Aggregation Rule" ().
    per_gate_count: Counter[str] = Counter()
    per_gate_tags: dict[str, Counter[str]] = defaultdict(Counter)
    per_gate_records: dict[str, list[dict]] = defaultdict(list)

    # Per-agent breakdown (diagnostic — secondary to threshold tuning).
    per_agent_count: Counter[str] = Counter()
    per_agent_gates: dict[str, Counter[str]] = defaultdict(Counter)

    # Cross-cut aggregation.
    tag_count: Counter[str] = Counter()
    record_shape: Counter[str] = Counter()  # "bulk" or "cross-lane"

    for rec in records:
        shape = "cross-lane" if rec.get("gate") else "bulk"
        record_shape[shape] += 1
        tags = _tag_record(rec)
        for tag in tags:
            tag_count[tag] += 1
        agent = rec.get("agent") or "unknown"
        per_agent_count[agent] += 1
        for gate in _record_to_gate_ids(rec):
            if gate_filter and gate != gate_filter:
                continue
            per_gate_count[gate] += 1
            for tag in tags:
                per_gate_tags[gate][tag] += 1
            per_gate_records[gate].append(rec)
            per_agent_gates[agent][gate] += 1

    # Ledger-to-firings CONSISTENCY ratio per gate: ledger_count /
    # firings_decision_override_count. This is a cross-store AUDIT-INTEGRITY
    # signal (how many override-bypass-ledger records exist per gate-side
    # decision=override firing), NOT a bounded override fraction. It can exceed
    # 1.0 — one bulk-override ledger record maps to many gate_ids, and cross-lane
    # claims log to the ledger but not always to firings — so it is NOT
    # comparable to the 0.5 tighten threshold. A ratio near 1.0 is healthy
    # (every override logged in both stores); far from 1.0 flags a logging-path
    # gap. The BOUNDED override fraction (override / (block + override), in
    # [0,1]) that the  tighten trigger uses is computed in
    # gate-retirement-eval.py / gate-stats.py — this analyzer does NOT recompute
    # it (single source of truth). When the denominator is 0 (no decision=override
    # firings for that gate in the window), the ratio is undefined — reported as
    # null rather than collapsing to 0 or 1, so consumers can distinguish
    # "ratio known to be low" from "no firings telemetry available." ()
    per_gate_rate: dict[str, dict] = {}
    for gate, count in per_gate_count.items():
        firings = firings_override_counts.get(gate, 0)
        if firings > 0:
            per_gate_rate[gate] = {
                "ledger_count": count,
                "firings_override_count": firings,
                "rate": round(count / firings, 3),
            }
        else:
            per_gate_rate[gate] = {
                "ledger_count": count,
                "firings_override_count": 0,
                "rate": None,
            }

    # Threshold-adjustment suggestions: when one tag accounts for >=50% of
    # ledger overrides on a single gate (with N>=3), surface it as a review
    # candidate. The gate may have a threshold that is producing too many
    # override-requests for the same legitimate-looking pattern.
    suggestions: list[dict] = []
    for gate, tags in per_gate_tags.items():
        total = sum(tags.values())
        if total < 3:
            continue
        for tag, n in tags.most_common(1):
            if tag == "untagged":
                continue
            ratio = n / total
            if ratio >= 0.5:
                suggestions.append({
                    "gate": gate,
                    "dominant_tag": tag,
                    "tag_count": n,
                    "gate_total": total,
                    "ratio": round(ratio, 3),
                    "recommendation": (
                        f"Review whether gate '{gate}' is firing on legitimate "
                        f"'{tag}' patterns ({n}/{total} overrides). Consider "
                        f"adding a pre-gate filter, tightening the rule's "
                        f"signal-source, or formally documenting this as an "
                        f"accepted bypass class."
                    ),
                })

    # Top reason clusters across all records.
    top_clusters = [
        {"tag": tag, "count": count, "share": round(count / max(len(records), 1), 3)}
        for tag, count in tag_count.most_common(top_k)
    ]

    return {
        "window_days": days,
        "since": since.strftime("%Y-%m-%dT%H:%M:%S"),
        "total_records": len(records),
        "record_shapes": dict(record_shape),
        "per_gate": {
            gate: {
                "ledger_count": per_gate_count[gate],
                "firings_override_count": per_gate_rate[gate]["firings_override_count"],
                "ledger_to_firings_ratio": per_gate_rate[gate]["rate"],
                "top_tags": dict(per_gate_tags[gate].most_common(5)),
            }
            for gate in per_gate_count
        },
        "per_agent": {
            agent: {
                "ledger_count": per_agent_count[agent],
                "gates": dict(per_agent_gates[agent]),
            }
            for agent in per_agent_count
        },
        "top_reason_clusters": top_clusters,
        "suggested_threshold_adjustments": suggestions,
        "computed_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }


def render_human(result: dict) -> str:
    """Human-readable text report."""
    lines: list[str] = []
    lines.append("=== Override-Bypass Ledger Report ===")
    lines.append(f"Window: {result['since']} → now ({result['window_days']} days)")
    lines.append(f"Total records: {result['total_records']}")
    if result["record_shapes"]:
        shapes = ", ".join(f"{k}={v}" for k, v in result["record_shapes"].items())
        lines.append(f"Record shapes: {shapes}")
    lines.append("")

    if result["per_gate"]:
        lines.append("-- Per-gate breakdown --")
        # Sort by ledger_count desc.
        sorted_gates = sorted(
            result["per_gate"].items(),
            key=lambda kv: kv[1]["ledger_count"],
            reverse=True,
        )
        for gate, stats in sorted_gates:
            ratio = stats["ledger_to_firings_ratio"]
            ratio_str = f"{ratio:.2f}" if ratio is not None else "n/a (no firings)"
            lines.append(
                f"  {gate}: {stats['ledger_count']} ledger overrides | "
                f"firings={stats['firings_override_count']} | "
                f"ledger/firings ratio={ratio_str}"
            )
            if stats["top_tags"]:
                tags_str = ", ".join(f"{t}={n}" for t, n in stats["top_tags"].items())
                lines.append(f"      top tags: {tags_str}")
        lines.append("")
    else:
        lines.append("(no records in window)")
        lines.append("")

    if result.get("per_agent"):
        lines.append("-- Per-agent breakdown (diagnostic) --")
        # Sort by ledger_count desc.
        sorted_agents = sorted(
            result["per_agent"].items(),
            key=lambda kv: kv[1]["ledger_count"],
            reverse=True,
        )
        for agent, stats in sorted_agents:
            gates_str = ", ".join(f"{g}={n}" for g, n in stats["gates"].items())
            lines.append(
                f"  {agent}: {stats['ledger_count']} records | gates: {gates_str or '(none)'}"
            )
        lines.append("")

    if result["top_reason_clusters"]:
        lines.append("-- Top reason clusters --")
        for c in result["top_reason_clusters"]:
            lines.append(
                f"  {c['tag']}: {c['count']} ({c['share']:.1%})"
            )
        lines.append("")

    if result["suggested_threshold_adjustments"]:
        lines.append("-- Suggested threshold adjustments --")
        for s in result["suggested_threshold_adjustments"]:
            lines.append(
                f"  [{s['gate']}] dominant tag '{s['dominant_tag']}' "
                f"({s['tag_count']}/{s['gate_total']}, {s['ratio']:.0%})"
            )
            lines.append(f"      → {s['recommendation']}")
        lines.append("")
    else:
        lines.append("-- Suggested threshold adjustments --")
        lines.append("  (none — no gate has ≥50% single-tag concentration with N≥3)")
        lines.append("")

    lines.append(f"computed_at: {result['computed_at']}")
    return "\n".join(lines)


def render_proposal_goals(result: dict) -> list[dict]:
    """Generate participants:[agent,user] goal JSONs for each threshold suggestion ().

    Each suggestion in result["suggested_threshold_adjustments"] becomes one
    proposal goal. The goal is participants:[agent,user] because threshold
    edits must never auto-land — the human reviews and approves, the agent
    applies after approval. Each goal references the specific gate id,
    proposes a concrete config-field edit, and embeds the supporting
    evidence (dominant tag share, raw counts, ratio).

    The user sees this as a normal item in the aspiration queue. On
    approval, the agent updates `core/config/gates.yaml` (or the gate's
    own config) and the change lands through the standard goal pipeline.

    Returns: list of goal-JSON dicts ready to feed `aspirations-add-goal.sh`
    one-per-line. Returns [] when no suggestions exist.
    """
    suggestions = result.get("suggested_threshold_adjustments") or []
    goals: list[dict] = []
    for s in suggestions:
        gate = s["gate"]
        tag = s["dominant_tag"]
        n = s["tag_count"]
        total = s["gate_total"]
        ratio = s["ratio"]
        title = (
            f"Tune {gate} threshold to address '{tag}' dominance "
            f"({n}/{total} overrides, {ratio:.0%})"
        )
        description = (
            f"The override-bypass-ledger analyzer detected that gate '{gate}' "
            f"is being overridden predominantly for the same reason cluster: "
            f"'{tag}' accounts for {n} of {total} overrides ({ratio:.0%}) "
            f"in the look-back window.\n\n"
            f"PROPOSAL: Review whether '{tag}' represents (a) a legitimate "
            f"bypass class that should be formally exempted at the gate, "
            f"(b) a sign that the gate's threshold or signal-source is too "
            f"tight, or (c) a false-positive prose pattern that needs a "
            f"pre-filter. Apply the appropriate config-field edit to "
            f"`core/config/gates.yaml` (or the gate's own threshold config) "
            f"OR retire the gate via /verify-learning if the override "
            f"pattern indicates the rule is obsolete.\n\n"
            f"EVIDENCE:\n"
            f"- Gate: {gate}\n"
            f"- Dominant reason cluster: {tag}\n"
            f"- Override count for this tag: {n}\n"
            f"- Total overrides for gate (window): {total}\n"
            f"- Concentration ratio: {ratio:.0%}\n"
            f"- Analyzer window: {result['window_days']} days "
            f"(since {result['since']})\n"
            f"- Computed at: {result['computed_at']}\n\n"
            f"DECISION RULE: Threshold edits never auto-land. User reviews "
            f"this goal, approves a specific config-field change with a "
            f"new value, agent applies the edit, then the goal completes."
        )
        goal = {
            "title": title,
            "description": description,
            "status": "pending",
            "priority": "MEDIUM",
            "category": "framework-architecture",
            "participants": ["agent", "user"],
            "origin_signal": f"override-ledger-consume:{gate}:{tag}",
            "verification": {
                "outcomes": [
                    f"User approves a specific config-field change for {gate} "
                    f"(or decides no change is warranted with documented rationale)",
                    f"If approved: agent edits `core/config/gates.yaml` (or "
                    f"gate-specific config) with the user-supplied new value",
                    f"If retired: gate-retirement-eval.sh logs the decision "
                    f"with supporting evidence from this analyzer report",
                ],
            },
        }
        goals.append(goal)
    return goals


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Override-bypass-ledger consumer/analyzer (g-281-03)",
    )
    parser.add_argument("--days", type=int, default=30,
                        help="Look-back window in days (default 30)")
    parser.add_argument("--top", type=int, default=10,
                        help="Top-K reason clusters to show (default 10)")
    parser.add_argument("--gate", type=str, default=None,
                        help="Restrict output to a single gate id")
    parser.add_argument("--output", choices=["json", "human", "goals"], default="json",
                        help="Output format (default json). 'goals' emits one "
                             "JSON proposal-goal per line for threshold "
                             "suggestions (g-281-05; participants:[agent,user] "
                             "— feed to aspirations-add-goal.sh).")
    args = parser.parse_args(argv)

    result = analyze(days=args.days, gate_filter=args.gate, top_k=args.top)

    if args.output == "json":
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.output == "human":
        print(render_human(result))
    else:  # goals
        goals = render_proposal_goals(result)
        for g in goals:
            print(json.dumps(g, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
