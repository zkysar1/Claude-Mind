#!/usr/bin/env python3
# domain-leak-exempt: NARRATIVE_PATTERNS literally lists domain phrases ("game
# session", "post-processor game session", etc.) so the auditor can detect
# narrative defers that the agent could resolve itself. The terms are data
# powering capability-routing classification, not engine logic.
"""Audit all deferred goals for stale narrative defers.

Scans world/aspirations.jsonl + every <agent>/aspirations.jsonl, classifies
each goal with a non-null defer_reason into one of three categories:

  a) genuine — legitimate block (time-gated, sequential dep, agent-locked
     physical action, partner-claimed work)
  b) ambiguous — defer language is plausible but the underlying state is
     reachable via agent action (e.g., "needs game session data" when a
     game session is something the agent can launch)
  c) narrative-only — the defer reads like a narrative excuse rather than
     a structural block (matches NARRATIVE_PATTERNS, "both abstained",
     "needs user attention" without capability evidence, etc.)

Output: JSON to stdout (default) or markdown report to a path via --report.

Sibling to capability-gate.py — that gate fires at write time on new
defers/blockers. This audit catches PRE-existing defers that predate or
slipped past the gate.

Origin: g-255-06 / g-255-02 lineage.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

# Single source of truth for WORLD_DIR resolution. _paths.py reads the same
# local-paths.conf via _resolve_agent_name + _read_local_paths and applies the
# AYOAI_WORLD env override identically. Importing it here eliminates the prior
# divergence where this file's own _resolve_world_dir() hardcoded
# bravo-before-alpha agent fallback while _paths.py uses sorted(glob).
sys.path.insert(0, str(SCRIPT_DIR))
from _paths import WORLD_DIR, agents_root as _agents_root  # noqa: E402


# ---- Classifier ------------------------------------------------------------

# Narrative patterns from capability-gate.py NARRATIVE_PATTERNS plus extras
# specific to defer-reason audits (gate's list is for failure_reason on
# blockers; this list adds defer-flavored phrasings).
DEFER_NARRATIVE_PATTERNS = [
    # capability-gate's NARRATIVE_PATTERNS (synced 2026-04-25)
    "user approves", "user approved", "user authorizes", "user authorized",
    "waiting for user decision", "user-leg scope: approval",
    "user-leg: approval", "user must", "user needs to", "user should",
    "pending user sign-off", "pending user review",
    "blocked on user-initiated", "blocked on user action",
    # defer-specific extensions
    "both agents abstained",
    "needs user attention",
    "requires user setup",
    "cannot be triggered via api",  # often false: capability-routing may have [agent, user] split
]

# Cat-A signals — structurally legitimate defers
GENUINE_PREFIXES = (
    "blocked_on_dependency:",
    "precondition_unmet:",
    "time-gated:",
)

GENUINE_PATTERNS = [
    "weeks of post-",
    "weeks of live",
    "week measurement window",
    "month monitoring window",
    "12 months",
    "monitoring window",
    "post-ship is too early",
    "post-deploy",
    "circuit breaker",  # circuit-breaker style timed defers
    "multi-hour test-implementation",  # partner test work, persists across sessions
    "must ship+settle",
    "natural /stop",  # event-gated by orchestrator
    "observation window",
    "telemetry accumulated",
    "let predicate.py bake",
    "settle before",
    "t+",  # t+14d, T+14d (haystack lowercased at classify():134, so needle must be lowercase)
    "machine restart to clear",  # purely physical, but mark it for review (could be Unblock)
]

# Cat-B signals — ambiguous: defer mentions data that the agent COULD
# obtain by triggering a game session OR processor run, but only when
# such triggering is feasible right now (no live session, etc).
AMBIGUOUS_PATTERNS = [
    "post-processor game session",
    "post-fix game session",
    "no post-processor",
    "no post-fix",
    "needs processor run",
    "no processor run",
    "no live player sessions",
    "5 commits awaiting ci",
    "consolidatedmemory strategy",
    "structurechangeprocessor.lua algorithm",
]


def _has_any(text_lo: str, needles: list) -> list:
    return [n for n in needles if n in text_lo]


def _has_prefix(text: str, prefixes: tuple) -> str | None:
    s = text.lstrip()
    for p in prefixes:
        if s.lower().startswith(p):
            return p
    return None


def classify(defer_reason: str, participants: list | None) -> dict:
    """Return {"category": "a"|"b"|"c"|"unknown", "evidence": [...]}."""
    if not defer_reason:
        return {"category": "unknown", "evidence": ["empty defer_reason"]}

    text = defer_reason.strip()
    lo = text.lower()

    # Cat-A first: structured prefix defers are the most explicit
    pfx = _has_prefix(text, GENUINE_PREFIXES)
    if pfx:
        return {"category": "a", "evidence": [f"structured-prefix:{pfx}"]}

    # Cat-A pattern matches
    a_hits = _has_any(lo, GENUINE_PATTERNS)
    # Cat-C narrative pattern matches
    c_hits = _has_any(lo, DEFER_NARRATIVE_PATTERNS)
    # Cat-B ambiguous matches
    b_hits = _has_any(lo, AMBIGUOUS_PATTERNS)

    # Decision priorities: C beats A when narrative phrasing dominates,
    # because category-c is the actionable finding and we want it surfaced.
    # An A-pattern match alongside a C-pattern match is suspicious — a
    # legitimate dep should not also be using narrative excuse language.
    if c_hits and not a_hits:
        return {"category": "c", "evidence": [f"narrative:{p}" for p in c_hits]}
    if c_hits and a_hits:
        # Both A and C signal: surface as C with conflict note (reviewer should look)
        return {
            "category": "c",
            "evidence": [f"narrative:{p}" for p in c_hits]
                        + [f"genuine:{p}" for p in a_hits]
                        + ["conflict: defer carries both narrative and structural signals"],
        }
    if a_hits:
        return {"category": "a", "evidence": [f"genuine:{p}" for p in a_hits]}
    if b_hits:
        return {"category": "b", "evidence": [f"ambiguous:{p}" for p in b_hits]}

    # Unmatched — review manually
    return {"category": "b", "evidence": ["unmatched: review-by-hand"]}


# ---- Loader ---------------------------------------------------------------

def _enumerate_agents() -> list:
    """Find all <agent>/aspirations.jsonl pairs in PROJECT_ROOT."""
    out = []
    for child in sorted(_agents_root().iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith(".") or child.name in ("core", "meta", "world"):
            continue
        path = child / "aspirations.jsonl"
        if path.is_file():
            out.append((child.name, path))
    return out


def load_deferred() -> list:
    """Return list of dicts {src, agent, asp_id, goal_id, defer_reason,
    defer_set_at, participants, title, status}."""
    out = []
    sources = []
    world = WORLD_DIR if WORLD_DIR.is_dir() else None
    if world is not None:
        sources.append(("world", None, world / "aspirations.jsonl"))
    for agent_name, path in _enumerate_agents():
        sources.append(("agent", agent_name, path))

    for src, agent_name, path in sources:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                a = json.loads(line)
            except json.JSONDecodeError:
                continue
            for g in a.get("goals", []):
                dr = g.get("defer_reason")
                if not dr:
                    continue
                out.append({
                    "src": src,
                    "agent": agent_name,
                    "asp_id": a.get("id"),
                    "goal_id": g.get("id"),
                    "title": (g.get("title") or "")[:120],
                    "defer_reason": dr,
                    "defer_set_at": g.get("defer_reason_set_at"),
                    "participants": g.get("participants"),
                    "status": g.get("status"),
                })
    return out


# ---- Output ---------------------------------------------------------------

def render_markdown(records: list) -> str:
    by_cat = {"a": [], "b": [], "c": [], "unknown": []}
    for r in records:
        by_cat.setdefault(r["category"], []).append(r)
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"# Deferred Defers Audit — {today}", ""]
    lines.append(f"**Total deferred:** {len(records)}")
    lines.append(f"**Cat A (genuine):** {len(by_cat['a'])} | "
                 f"**Cat B (ambiguous):** {len(by_cat['b'])} | "
                 f"**Cat C (narrative-only):** {len(by_cat['c'])} | "
                 f"**Unknown:** {len(by_cat['unknown'])}")
    lines.append("")
    for cat, label in [("c", "Category C — Narrative-Only (action recommended)"),
                       ("b", "Category B — Ambiguous (review)"),
                       ("a", "Category A — Genuine (legitimate block)"),
                       ("unknown", "Category Unknown — manual review")]:
        rs = by_cat.get(cat, [])
        if not rs:
            continue
        lines.append(f"## {label}")
        lines.append("")
        lines.append("| Goal | Asp | Src | Participants | Defer Reason | Evidence |")
        lines.append("|------|-----|-----|--------------|--------------|----------|")
        for r in rs:
            parts = ",".join(r.get("participants") or [])
            reason = (r["defer_reason"] or "").replace("|", "\\|").replace("\n", " ")[:120]
            evid = ", ".join(r.get("evidence") or []).replace("|", "\\|")[:80]
            src_label = f"{r['src']}/{r['agent']}" if r['agent'] else r['src']
            lines.append(f"| {r['goal_id']} | {r['asp_id']} | {src_label} | {parts} | {reason} | {evid} |")
        lines.append("")
    return "\n".join(lines)


# ---- Main -----------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--output", choices=("json", "human"), default="json",
                   help="json (default) or human (summary table)")
    p.add_argument("--report", help="Write markdown report to this path")
    args = p.parse_args(argv)

    deferred = load_deferred()
    enriched = []
    for r in deferred:
        c = classify(r["defer_reason"], r.get("participants"))
        enriched.append({**r, "category": c["category"], "evidence": c["evidence"]})

    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(render_markdown(enriched), encoding="utf-8")

    if args.output == "json":
        print(json.dumps({
            "scanned_at": datetime.now().isoformat(timespec="seconds"),
            "total": len(enriched),
            "by_category": {
                cat: sum(1 for r in enriched if r["category"] == cat)
                for cat in ("a", "b", "c", "unknown")
            },
            "records": enriched,
        }, indent=2))
    else:
        print(f"Total deferred: {len(enriched)}")
        for cat in ("a", "b", "c", "unknown"):
            n = sum(1 for r in enriched if r["category"] == cat)
            print(f"  Category {cat}: {n}")
        print()
        for r in enriched:
            print(f"  [{r['category']}] {r['goal_id']:14} | {r['asp_id']} | {r['evidence']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
