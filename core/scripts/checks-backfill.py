#!/usr/bin/env python3
"""checks-backfill.py — Tier 1b migration report (REPORT-ONLY).

Scans all active aspirations' goals and REPORTS which ones are candidates for
`verification.checks[]` backfill from `core/config/aspirations.yaml`
goal_templates. This tool NEVER writes — apply is a manual follow-up.

Plan: ~/.claude/plans/i-had-one-agent-luminous-reddy.md (Tier 1b #3).

Candidate sources (in priority order):
  1. Legacy `completion_check` field → would move to verification.checks[0]
  2. Goal's `skill` field matches a template → copy template's checks[]
  3. Goal's `category` field matches a template name → copy template's checks[]
  4. Otherwise: skip (leave checks: [] for LLM Q1/Q2/Q3 fall-through)

Output: JSON rollup + per-goal decision.
"""

import argparse
import json
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from _paths import AGENT_DIR, PROJECT_ROOT, WORLD_DIR  # type: ignore
from _fileops import log_script_decision  # type: ignore

try:
    import yaml  # type: ignore
except ImportError:
    print("ERROR: PyYAML not installed", file=sys.stderr)
    sys.exit(2)


def _load_templates():
    cfg_path = Path(PROJECT_ROOT) / "core" / "config" / "aspirations.yaml"
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return cfg.get("goal_templates") or {}


def _template_for_goal(goal, templates):
    """Pick a template by skill → category fallback."""
    skill = (goal.get("skill") or "").lstrip("/")
    category = goal.get("category") or ""

    # Strip argument suffix: "/review-hypotheses --resolve" → "review-hypotheses"
    skill_base = re.split(r"\s+", skill, maxsplit=1)[0]

    # Direct keyword match: template keys like "research", "development", "hypothesis"
    if skill_base.replace("-", "_") in templates:
        return skill_base.replace("-", "_"), templates[skill_base.replace("-", "_")]

    # Skill hints: /research-topic → research, /decompose → development, etc.
    skill_to_template = {
        "research-topic": "research",
        "review-hypotheses": "review",
        "tree-maintain": "maintenance",
        "reflect": "reflection",
    }
    key = skill_to_template.get(skill_base)
    if key and key in templates:
        return key, templates[key]

    if category in templates:
        return category, templates[category]

    return None, None


def _load_aspirations(source):
    """Load world or agent aspirations list."""
    if source == "world":
        path = Path(WORLD_DIR) / "aspirations.jsonl" if WORLD_DIR else None
    elif source == "agent":
        if AGENT_DIR is None:
            return []
        path = Path(AGENT_DIR) / "aspirations.jsonl"
    else:
        return []

    if path is None or not path.exists():
        return []

    items = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return items


def _classify_goal(goal, templates):
    """Return a backfill decision record."""
    verification = goal.get("verification") or {}
    checks = verification.get("checks") or []
    legacy_completion_check = goal.get("completion_check")

    decision = {
        "goal_id": goal.get("id"),
        "title": goal.get("title"),
        "skill": goal.get("skill"),
        "category": goal.get("category"),
        "current_checks_count": len(checks),
        "action": None,
        "source": None,
        "proposed_checks": None,
    }

    # Priority 1: legacy completion_check migration
    if legacy_completion_check and not checks:
        decision["action"] = "migrate_legacy"
        decision["source"] = "completion_check"
        decision["proposed_checks"] = [legacy_completion_check]
        return decision

    # Already has checks — skip
    if checks:
        decision["action"] = "skip_has_checks"
        return decision

    # Priority 2/3: template lookup
    tmpl_name, tmpl = _template_for_goal(goal, templates)
    if tmpl is None:
        decision["action"] = "skip_no_template"
        return decision

    tmpl_verification = tmpl.get("verification") or {}
    tmpl_checks = tmpl_verification.get("checks") or []
    if not tmpl_checks:
        decision["action"] = "skip_template_empty_checks"
        decision["source"] = tmpl_name
        return decision

    decision["action"] = "backfill_from_template"
    decision["source"] = tmpl_name
    decision["proposed_checks"] = tmpl_checks
    return decision


# NOTE: This tool is DRY-RUN ONLY by design.
#
# An earlier `--apply` path invoked `aspirations.py update-goal <id>
# verification.checks <json>`. That path was broken: aspirations.py
# cmd_update_goal does `goal[field] = value` — a flat key assignment —
# so the dotted path `verification.checks` would have been written as a
# LITERAL top-level key next to the existing nested `verification` dict,
# corrupting goal schema silently. aspirations.py intentionally has no
# dotted-path support; verification is a nested block that belongs to the
# `update` (aspiration-level) code path, not update-goal.
#
# Single source of truth: goal shape is managed by aspirations.py. If a
# future migration needs to rewrite nested fields, extend aspirations.py
# first — do NOT reintroduce a parallel writer here.


def main():
    ap = argparse.ArgumentParser(
        description="Report which goals are candidates for verification.checks[] "
                    "backfill from templates. REPORT-ONLY — apply is manual.")
    ap.add_argument("--source", choices=["world", "agent", "both"], default="both")
    ap.add_argument("--goal-status", action="append",
                    default=None,
                    help="Only process goals with these statuses (repeatable). "
                         "Default: pending, in-progress")
    args = ap.parse_args()

    status_filter = set(args.goal_status or ["pending", "in-progress"])
    templates = _load_templates()

    sources = ["world", "agent"] if args.source == "both" else [args.source]
    all_decisions = []

    for src in sources:
        aspirations = _load_aspirations(src)
        for asp in aspirations:
            if asp.get("status") != "active":
                continue
            for goal in asp.get("goals") or []:
                if goal.get("status") not in status_filter:
                    continue
                decision = _classify_goal(goal, templates)
                decision["source_file"] = src
                all_decisions.append(decision)

    # Stats rollup
    action_counts = {}
    for d in all_decisions:
        action_counts[d["action"]] = action_counts.get(d["action"], 0) + 1

    backfill_candidates = [
        d for d in all_decisions
        if d["action"] in ("migrate_legacy", "backfill_from_template")
    ]

    result = {
        "subcommand": "checks-backfill",
        "source_scope": sources,
        "status_filter": sorted(status_filter),
        "total_goals_scanned": len(all_decisions),
        "action_counts": action_counts,
        "backfill_candidates_count": len(backfill_candidates),
        "summary": (
            f"scanned {len(all_decisions)} goals; "
            f"{len(backfill_candidates)} candidate(s) for backfill (REPORT-ONLY)"
        ),
        "decisions": all_decisions,
    }
    log_script_decision("checks-backfill", {
        "scanned": len(all_decisions),
        "backfill_candidates": len(backfill_candidates),
    })
    print(json.dumps(result, ensure_ascii=False, default=str))
    sys.exit(0)


if __name__ == "__main__":
    main()
