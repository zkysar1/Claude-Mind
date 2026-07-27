#!/usr/bin/env python3
"""Scorer-override detective audit — Scorer Sovereignty Layer C ().

Scans every agent's execution-diary for ``entry_type=scorer_override`` entries
(written by the Layer B claim gate, ``scorer-verdict-gate.py``) in the last N
hours, groups by agent + sanctioned-deviation code, and flags HITS:

  * any single agent with MORE THAN 3 overrides in the window, OR
  * any ``force-override`` use at all.

The "file an Investigate goal" half of Layer C lives in the WRAPPER
(``scorer-override-audit.sh``), NOT here — same script/wrapper split as the
sibling ``aspirations-rejection-audit.py`` (the script SCANS + returns a
report + exit code; the wrapper files the goal via ``aspirations-add-goal.sh``
JSON stdin). This keeps the audit engine side-effect-free and unit-testable.

Cross-agent glob routes through ``agents_root()`` per CLAUDE.md "Agent-dir
Resolution" (the cross-agent-glob table) — it auto-tracks an
``AGENTS_PARENT_DIR`` rename and never depth-1-redrifts to
``PROJECT_ROOT.glob`` (the g-115-1405 class). ``--agents-root`` overrides it
for hermetic tests only (mirrors ``read_ledger``'s ``root=`` param in
``skill-coinvocation-discovery.py``).

Metric of success (design intent): fleet-wide UNSANCTIONED-override rate is 0
BY CONSTRUCTION (the Layer B gate refuses unsanctioned deviations), so every
row here is a SANCTIONED deviation — this audit makes the sanctioned rate
visible per agent per code. A sustained nonzero force-override rate is a DESIGN
SIGNAL (the deviation enum is missing a legitimate code), not noise — hence
force-override is a hit at count 1.

Run modes:
  --since-hours <N>   # only count events from the last N hours (default 24)
  --json              # machine-readable JSON report
  --agents-root <p>   # override the agents root (TEST hermeticity only)
  --exit-on-hits      # exit 1 if any hits in window (cron regression shape)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import agents_root  # noqa: E402

# Layer B content shape (scorer-verdict-gate.py):
#   "scorer-override: claimed <goal> over scorer top <top> (deviation=<code>)"
_OVERRIDE_RE = re.compile(
    r"claimed\s+(\S+)\s+over\s+scorer\s+top\s+(\S+)\s+\(deviation=([^)]+)\)"
)
OVER_THRESHOLD = 3  # STRICTLY MORE THAN this many per agent in-window -> hit
FORCE_CODE = "force-override"

# : deviation codes are NOT fungible — classify them so the Layer-C
# recommendation names the specific fix lane instead of always saying "tune
# scorer weights" (that recommendation was 0/2 across both real fires;
# force_override=0 both times —  bravo 21 dev,  bravo 5 dev).
#   WEIGHT_SIGNAL   — force-override: the ONLY class that signals a genuine
#                     scorer weight/enum gap (a legitimate reason to escape the
#                     scorer). The one case where "change the scorer" is right.
#   LANE_DISCIPLINE — cross-agent + self-abstention: by-design deviations the
#                     scorer INTENTIONALLY leaves to the agent layer. Encoding
#                     lane into the weights VIOLATES Scorer Sovereignty
#                     (). NOT a weights signal.
#   RUNNABILITY     — precondition-fail / partner-claim / blocker-gate: the
#                     scorer-top was temporarily un-runnable. Fixable by the
#                     SELECTOR/ROUTING (de-rank until runnable), never by weights.
# Plus a per-scorer_top recurrence count: a single scorer_top deviated-over
# > STUCK_TOP_THRESHOLD times is a STUCK-AT-TOP goal (un-runnable or MISROUTED)
# — the real actionable signal both real fires carried ( 4×,
#  13×) that the "tune weights" text buried.
WEIGHT_SIGNAL_CODES = frozenset({"force-override"})
LANE_DISCIPLINE_CODES = frozenset({"cross-agent", "self-abstention"})
RUNNABILITY_CODES = frozenset({"precondition-fail", "partner-claim", "blocker-gate"})
STUCK_TOP_THRESHOLD = 3  # STRICTLY MORE THAN this many deviations over ONE scorer_top


def _classify(code: str) -> str:
    if code in WEIGHT_SIGNAL_CODES:
        return "weight_signal"
    if code in LANE_DISCIPLINE_CODES:
        return "lane_discipline"
    if code in RUNNABILITY_CODES:
        return "runnability"
    return "other"


def _parse_ts(s):
    """Parse a naive-UTC ISO timestamp (TZ=UTC fleet-wide, );
    tolerate a trailing Z AND an offset-aware suffix (e.g. +00:00). Always
    returns a NAIVE datetime (or None on parse failure) so callers can compare
    against the naive datetime.now() cutoff in audit() without a TypeError
    (g-115-3001: an offset-aware stamp crashed the whole audit)."""
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", ""))
    except Exception:  # noqa: BLE001 — malformed stamp -> drop the row, never crash
        return None
    if dt.tzinfo is not None:
        # Normalize an offset-aware stamp to naive-UTC wall time. guard-982:
        # framework timestamps are naive by convention, so an aware stamp is the
        # anomaly — convert to UTC and drop the tzinfo rather than crash.
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def audit(since_hours: int = 24, root: Path | None = None) -> dict:
    """Scan all agents' diaries and return the report dict. Pure read — no
    goal filing, no writes."""
    base = root if root is not None else agents_root()
    cutoff = datetime.now() - timedelta(hours=since_hours)  # naive UTC
    per_agent: dict[str, dict] = {}
    # : global scorer_top -> recurrence. Stuck-at-top is a property of
    # the GOAL/routing, not the agent — a misrouted top can be deviated-over by
    # multiple agents' cross-agent selectors, so count across ALL agents.
    per_top: dict[str, dict] = {}
    for diary in sorted(base.glob("*/session/execution-diary.jsonl")):
        agent = diary.parent.parent.name
        try:
            lines = diary.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for ln in lines:
            ln = ln.strip()
            # Cheap pre-filter before json.loads (the diary is mostly
            # non-override phase entries).
            if not ln or "scorer_override" not in ln:
                continue
            try:
                e = json.loads(ln)
            except Exception:  # noqa: BLE001 — torn/partial line, skip
                continue
            if e.get("entry_type") != "scorer_override":
                continue
            ts = _parse_ts(e.get("timestamp"))
            if ts is None or ts < cutoff:
                continue
            m = _OVERRIDE_RE.search(e.get("content", ""))
            code = m.group(3).strip() if m else "unparsed"
            claimed = m.group(1) if m else e.get("goal_id", "?")
            top = m.group(2) if m else "?"
            cls = _classify(code)  # 
            a = per_agent.setdefault(
                agent,
                {"total": 0, "by_code": {}, "by_class": {}, "force": 0, "rows": []},
            )
            a["total"] += 1
            a["by_code"][code] = a["by_code"].get(code, 0) + 1
            a["by_class"][cls] = a["by_class"].get(cls, 0) + 1  # 
            if code == FORCE_CODE:
                a["force"] += 1
            a["rows"].append(
                {
                    "agent": agent,
                    "claimed": claimed,
                    "scorer_top": top,
                    "deviation": code,
                    "timestamp": e.get("timestamp"),
                }
            )
            # : track per-scorer_top recurrence globally. Only parsed
            # rows carry a real top; an unparsed row (top="?") is not a goal, so
            # it must never accumulate into a false stuck-at-top signal.
            if m:
                t = per_top.setdefault(top, {"count": 0, "agents": {}, "codes": {}})
                t["count"] += 1
                t["agents"][agent] = t["agents"].get(agent, 0) + 1
                t["codes"][code] = t["codes"].get(code, 0) + 1

    agents_over = {
        a: d["total"] for a, d in per_agent.items() if d["total"] > OVER_THRESHOLD
    }
    force_rows = [
        r for d in per_agent.values() for r in d["rows"] if r["deviation"] == FORCE_CODE
    ]
    # : a single scorer_top deviated-over > STUCK_TOP_THRESHOLD times is
    # a STUCK-AT-TOP goal — the real actionable signal (routing/selector, NOT
    # weights). Both real fires were this shape ( 4×,  13×).
    stuck_tops = {
        t: d for t, d in per_top.items() if d["count"] > STUCK_TOP_THRESHOLD
    }
    stuck_top_ids = set(stuck_tops)
    # Evidence rows for the Investigate goal: the three hit conditions —
    # offending agents' rows + every force-override row + every row over a
    # stuck top.
    evidence_rows = [
        r
        for d in per_agent.values()
        for r in d["rows"]
        if r["agent"] in agents_over
        or r["deviation"] == FORCE_CODE
        or r["scorer_top"] in stuck_top_ids
    ]
    hits = bool(agents_over) or bool(force_rows) or bool(stuck_tops)  # 
    return {
        "since_hours": since_hours,
        "per_agent": {
            a: {
                "total": d["total"],
                "by_code": d["by_code"],
                "by_class": d["by_class"],  # 
                "force": d["force"],
            }
            for a, d in per_agent.items()
        },
        "agents_over_threshold": agents_over,
        "force_override_rows": force_rows,
        # : {top: {count, agents, codes}} for tops over the recurrence
        # threshold — the stuck-at-top / routing signal.
        "stuck_tops": {
            t: {"count": d["count"], "agents": d["agents"], "codes": d["codes"]}
            for t, d in stuck_tops.items()
        },
        "evidence_rows": evidence_rows,
        "hits": hits,
        "total_overrides": sum(d["total"] for d in per_agent.values()),
    }


def _recommendation_clauses(report: dict) -> list[tuple[str, str]]:
    """Compose code-class-aware recommendation clauses (). The ONLY
    clause that recommends scorer weight/enum tuning is the WEIGHT_SIGNAL one
    (force-override present) — deviation codes are NOT fungible, so lane-discipline
    and runnability deviations must never be attributed to a scorer-weights gap.
    Returns [(tag, text), ...]; tag ∈ {weight, routing, lane, review}."""
    clauses: list[tuple[str, str]] = []
    force = len(report.get("force_override_rows", []))
    stuck = report.get("stuck_tops", {})
    per_agent = report.get("per_agent", {})
    over = report.get("agents_over_threshold", {})

    def _bc(a: str) -> dict:
        return per_agent.get(a, {}).get("by_class", {})

    # WEIGHT/ENUM — the ONLY clause that mentions weight tuning; force-override only.
    if force:
        clauses.append((
            "weight",
            f"WEIGHT/ENUM SIGNAL ({force}× force-override): force-override is the ONLY deviation "
            "code that signals a genuine scorer weight/enum gap (a legitimate reason the agent had "
            "to escape the scorer). Check whether the deviation enum is missing a code, or the "
            "scorer weights need tuning — the one case where 'change the scorer' (Scorer "
            "Sovereignty) is the right action.",
        ))
    # STUCK-AT-TOP — routing/selector, NEVER weights.
    if stuck:
        tops = ", ".join(
            f"{t} ({d['count']}×)"
            for t, d in sorted(stuck.items(), key=lambda kv: -kv[1]["count"])
        )
        clauses.append((
            "routing",
            f"STUCK-AT-TOP -> SELECTOR/ROUTING (NOT weights): {tops} — goal(s) repeatedly ranked "
            "scorer-top yet deviated-over. A goal that stays top while agents keep passing over it "
            "is un-runnable or MISROUTED; the SELECTOR should de-rank it (until runnable) or its "
            "routing (intended_agent / lane) should be fixed — never the weights. Both real fires "
            "were this shape: g-001-341 (temp-drain misrouting) and g-001-339 (routed-to-me "
            "mis-abstention).",
        ))
    # LANE-DISCIPLINE — by-design; explicitly NOT weights. Over-threshold agents
    # whose deviations are entirely lane-discipline (no weight / runnability / other).
    lane_agents = [
        (a, over[a])
        for a in over
        if _bc(a).get("lane_discipline", 0) > 0
        and _bc(a).get("weight_signal", 0) == 0
        and _bc(a).get("runnability", 0) == 0
        and _bc(a).get("other", 0) == 0
    ]
    if lane_agents:
        who = ", ".join(f"{a} ({n}×)" for a, n in lane_agents)
        clauses.append((
            "lane",
            f"LANE-DISCIPLINE (by-design, NOT weights): {who} — cross-agent + self-abstention are "
            "deviations the scorer INTENTIONALLY leaves to the agent layer. Encoding lane into the "
            "weights would VIOLATE Scorer Sovereignty (g-115-2939 explicit conclusion). No weight "
            "tuning warranted; any residual action is agent-layer lane discipline, not the scorer.",
        ))
    # RUNNABILITY over-threshold agents not already explained by a stuck-top.
    runnability_agents = [a for a in over if _bc(a).get("runnability", 0) > 0]
    if runnability_agents and not stuck:
        who = ", ".join(runnability_agents)
        clauses.append((
            "routing",
            f"RUNNABILITY -> SELECTOR/ROUTING (NOT weights): {who} had precondition-fail / "
            "partner-claim / blocker-gate deviations — the scorer-top was temporarily un-runnable. "
            "Fixable by the SELECTOR (de-rank until runnable), never by the weights.",
        ))
    if not clauses:
        clauses.append((
            "review",
            "Deviations exceeded the threshold but did not classify into a known signal "
            "(weight / lane-discipline / runnability). Review the evidence rows manually.",
        ))
    return clauses


def recommendation_classes(report: dict) -> list[str]:
    """Ordered, de-duplicated list of recommendation clause tags ().
    'weight' appears IFF force-override was used — the single guard that
    lane-discipline / runnability deviations never get attributed to a
    scorer-weights gap. Public so callers + tests assert the attribution without
    string-matching the prose."""
    seen: list[str] = []
    for tag, _ in _recommendation_clauses(report):
        if tag not in seen:
            seen.append(tag)
    return seen


def build_investigate_goal(report: dict) -> dict | None:
    """Shape the Investigate goal (Layer C files one on hits). Pure — returns
    the goal dict, or None when there are no hits. The recommendation is
    code-class-aware (g-115-2999): it names the specific fix lane (weights/enum,
    selector/routing, or agent-layer lane discipline) instead of always saying
    'tune scorer weights' — which was 0/2 across both real fires (force_override=0
    both times). The WRAPPER (scorer-override-audit.sh) does the actual filing via
    aspirations-add-goal.sh; keeping the JSON construction here (not inline in bash)
    makes it testable and dodges the nested-quoting `py -3 -c` hazard."""
    if not report.get("hits"):
        return None
    rows = report["evidence_rows"]
    over = report["agents_over_threshold"]
    force = len(report["force_override_rows"])
    stuck = report.get("stuck_tops", {})
    stuck_summary = {t: d["count"] for t, d in stuck.items()}
    ev = "\n".join(
        f"  {r['agent']}: claimed {r['claimed']} over {r['scorer_top']} "
        f"(deviation={r['deviation']}) @ {r['timestamp']}"
        for r in rows
    )
    clauses = _recommendation_clauses(report)
    tags = [t for t, _ in clauses]
    rec = "\n\n".join(text for _, text in clauses)
    # Title leads with the strongest actionable signal (weight > routing > lane > review).
    if "weight" in tags:
        headline = "enum/weights gap (force-override)"
    elif "routing" in tags:
        headline = "stuck-at-top / routing (selector fix, not weights)"
    elif "lane" in tags:
        headline = "by-design lane discipline (no weights action)"
    else:
        headline = "unclassified deviations"
    desc = (
        f"Scorer-override audit (Scorer Sovereignty Layer C, g-115-2813) flagged HITS in the "
        f"last {report['since_hours']}h. agents_over_threshold={over}; "
        f"force_override_count={force}; stuck_tops={stuck_summary}.\n\n"
        f"Evidence rows:\n{ev}\n\n"
        f"RECOMMENDATION (code-class-aware — deviation codes are NOT fungible, g-115-2999):\n{rec}"
    )
    return {
        "title": f"Investigate: scorer-override audit — {headline} (Layer C)",
        "priority": "MEDIUM",
        "participants": ["agent"],
        "origin_signal": "scorer-override-audit-hit",
        "category": "framework-architecture",
        "description": desc,
    }


def main():
    ap = argparse.ArgumentParser(
        description="Scorer-override detective audit (Scorer Sovereignty Layer C, g-115-2813)"
    )
    ap.add_argument("--since-hours", type=int, default=24)
    ap.add_argument("--json", action="store_true")
    ap.add_argument(
        "--agents-root",
        type=Path,
        default=None,
        help="Override agents root (TEST hermeticity only)",
    )
    ap.add_argument(
        "--emit-investigate-goal",
        action="store_true",
        help="Print the Investigate goal JSON on hits (nothing when clean); the "
        "wrapper pipes it to aspirations-add-goal.sh",
    )
    ap.add_argument("--exit-on-hits", action="store_true")
    args = ap.parse_args()

    report = audit(since_hours=args.since_hours, root=args.agents_root)

    if args.emit_investigate_goal:
        goal = build_investigate_goal(report)
        if goal is not None:
            print(json.dumps(goal))
        sys.exit(1 if (args.exit_on_hits and report["hits"]) else 0)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(
            f"scorer-override audit ({args.since_hours}h): "
            f"{report['total_overrides']} overrides across "
            f"{len(report['per_agent'])} agent(s)"
        )
        for a, d in sorted(report["per_agent"].items()):
            flag = " ⚠OVER-THRESHOLD" if a in report["agents_over_threshold"] else ""
            ff = f" force={d['force']}" if d["force"] else ""
            print(f"  {a}: {d['total']}{flag}{ff} — {d['by_code']}")
        if report.get("stuck_tops"):
            st = ", ".join(
                f"{t}={d['count']}×" for t, d in report["stuck_tops"].items()
            )
            print(f"  STUCK-AT-TOP (routing signal, NOT weights): {st}")
        if report["hits"]:
            print(
                f"HITS: agents_over={list(report['agents_over_threshold'])} "
                f"force_overrides={len(report['force_override_rows'])} "
                f"stuck_tops={list(report.get('stuck_tops', {}))} "
                f"recommendation={recommendation_classes(report)}"
            )
        else:
            print("clean — no agent over threshold, no force-overrides, no stuck-at-top goal")

    if args.exit_on_hits:
        sys.exit(1 if report["hits"] else 0)
    sys.exit(0)


if __name__ == "__main__":
    main()
