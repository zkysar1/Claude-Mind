#!/usr/bin/env python3
"""Skill analytics and reporting.

Aggregates skill quality, co-invocation patterns, goal coverage, and
trend data to produce actionable recommendations (forge, retire, improve,
substitute).

Subcommands:
  reuse-report     — Per-skill usage and quality report
  co-invocation    — Skill co-invocation pair frequencies
  coverage         — Goal category skill coverage and success rates
  recommendations  — Forge/retire/improve/substitute suggestions
  trend            — Quality trend per skill over evaluations
"""

import argparse
import json
import sys
from collections import Counter, defaultdict

# Ensure stdout/stderr handle unicode on all platforms (Windows cp1252 fix)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

from _paths import META_DIR, AGENT_DIR, CONFIG_DIR, WORLD_DIR

# Meta-strategies (meta/) — domain-agnostic
QUALITY_PATH = META_DIR / "skill-quality.yaml"
SKILL_GAPS_PATH = META_DIR / "skill-gaps.yaml"

# World-level shared state
WORLD_RELATIONS_PATH = WORLD_DIR / "skill-relations.yaml"
BASE_RELATIONS_PATH = CONFIG_DIR / "skill-relations.yaml"
EXPERIENCE_PATH = AGENT_DIR / "experience.jsonl" if AGENT_DIR else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def read_yaml(path):
    """Read a YAML file, return parsed dict. Returns {} if missing."""
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if data is not None else {}


def read_jsonl(path):
    """Read a JSONL file, return list of dicts. Returns [] if missing."""
    if not path.exists():
        return []
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def load_all_relations():
    """Load and merge base + forged relations into a combined list.

    Base relations come from core/config/skill-relations.yaml under 'relations'.
    Forged relations come from world/skill-relations.yaml under 'forged_relations'.
    Returns a list of relation dicts.
    """
    base = read_yaml(BASE_RELATIONS_PATH)
    world_data = read_yaml(WORLD_RELATIONS_PATH)

    base_relations = base.get("relations", [])
    if not isinstance(base_relations, list):
        base_relations = []

    forged_relations = world_data.get("forged_relations", [])
    if not isinstance(forged_relations, list):
        forged_relations = []

    return base_relations + forged_relations


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_reuse_report(args):
    """Per-skill usage and quality report."""
    quality = read_yaml(QUALITY_PATH)
    skills_data = quality.get("skills", {})
    if not isinstance(skills_data, dict):
        skills_data = {}

    result_skills = {}
    total_evaluated = 0
    quality_sum = 0.0

    for skill_name, skill_info in skills_data.items():
        if not isinstance(skill_info, dict):
            continue

        evaluations = skill_info.get("evaluations", [])
        if not isinstance(evaluations, list) or not evaluations:
            continue

        total_evals = len(evaluations)
        total_evaluated += 1

        # Aggregate quality scores
        # "overall" is the weighted aggregate score written by skill-evaluate.py
        scores = [e.get("overall", 0) for e in evaluations
                  if isinstance(e, dict) and "overall" in e]
        avg_quality = round(sum(scores) / len(scores), 3) if scores else 0.0
        quality_sum += avg_quality

        # Find last evaluation date
        dates = [e.get("date", "") for e in evaluations
                 if isinstance(e, dict) and "date" in e]
        last_date = max(dates) if dates else None

        result_skills[skill_name] = {
            "total_evaluations": total_evals,
            "avg_quality": avg_quality,
            "min_quality": round(min(scores), 3) if scores else 0.0,
            "max_quality": round(max(scores), 3) if scores else 0.0,
            "last_evaluation_date": last_date,
        }

    avg_overall = round(quality_sum / total_evaluated, 3) if total_evaluated > 0 else 0.0

    output = {
        "skills": result_skills,
        "summary": {
            "total_evaluated": total_evaluated,
            "avg_quality": avg_overall,
        },
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))


def cmd_co_invocation(args):
    """Which skills are commonly used together."""
    world_data = read_yaml(WORLD_RELATIONS_PATH)
    log = world_data.get("co_invocation_log", [])
    if not isinstance(log, list):
        log = []

    if not log:
        print(json.dumps([], indent=2, ensure_ascii=False))
        return

    # Count skill pair frequencies
    pair_counts = Counter()
    for entry in log:
        skills = entry.get("skills", [])
        if not isinstance(skills, list) or len(skills) < 2:
            continue
        unique_skills = sorted(set(skills))
        for i in range(len(unique_skills)):
            for j in range(i + 1, len(unique_skills)):
                pair_counts[(unique_skills[i], unique_skills[j])] += 1

    total_entries = len(log)
    results = []
    for (skill_a, skill_b), count in pair_counts.most_common(20):
        pct = round(count / total_entries * 100, 1) if total_entries > 0 else 0.0
        results.append({
            "skill_a": skill_a,
            "skill_b": skill_b,
            "count": count,
            "pct": pct,
        })

    print(json.dumps(results, indent=2, ensure_ascii=False))


def cmd_coverage(args):
    """Which goal categories are covered by which skills."""
    records = read_jsonl(EXPERIENCE_PATH)

    # Filter to goal_execution records (or records with category + skill info)
    categories = defaultdict(lambda: {"skill_counts": Counter(), "skill_successes": Counter(), "total": 0})

    for rec in records:
        if not isinstance(rec, dict):
            continue

        # Look for category and skill fields in various record shapes
        category = rec.get("category", rec.get("goal_category", ""))
        if not category:
            continue

        skill = rec.get("skill", rec.get("primary_skill", ""))
        if not skill:
            # Try skills list
            skills = rec.get("skills", [])
            if isinstance(skills, list) and skills:
                skill = skills[0]
            else:
                continue

        categories[category]["total"] += 1
        categories[category]["skill_counts"][skill] += 1

        # Check for success
        outcome = rec.get("outcome", rec.get("result", ""))
        if isinstance(outcome, str) and outcome.lower() in ("success", "completed", "passed"):
            categories[category]["skill_successes"][skill] += 1
        elif isinstance(outcome, dict) and outcome.get("success"):
            categories[category]["skill_successes"][skill] += 1

    # Build output
    output_categories = {}
    for cat_name, cat_data in sorted(categories.items()):
        skills_list = []
        for skill, count in cat_data["skill_counts"].most_common():
            successes = cat_data["skill_successes"].get(skill, 0)
            success_rate = round(successes / count, 3) if count > 0 else 0.0
            skills_list.append({
                "skill": skill,
                "count": count,
                "success_rate": success_rate,
            })
        output_categories[cat_name] = {
            "skills": skills_list,
            "total_goals": cat_data["total"],
        }

    print(json.dumps({"categories": output_categories}, indent=2, ensure_ascii=False))


def cmd_recommendations(args):
    """Suggested actions based on analytics."""
    quality = read_yaml(QUALITY_PATH)
    gaps = read_yaml(SKILL_GAPS_PATH)
    relations = load_all_relations()

    forge = []
    retire = []
    improve = []
    substitute = []

    # --- Forge: gaps ready to forge ---
    gap_list = gaps.get("gaps", [])
    if not isinstance(gap_list, list):
        gap_list = []
    for gap in gap_list:
        if not isinstance(gap, dict):
            continue
        times = gap.get("times_encountered", 0)
        if times >= 3:
            forge.append({
                "skill": gap.get("name", gap.get("skill", "unknown")),
                "reason": "Capability gap encountered {} times".format(times),
                "evidence": gap.get("description", gap.get("evidence", "")),
            })

    # --- Retire / Improve: based on quality ---
    skills_data = quality.get("skills", {})
    if not isinstance(skills_data, dict):
        skills_data = {}

    skill_avg_quality = {}
    for skill_name, skill_info in skills_data.items():
        if not isinstance(skill_info, dict):
            continue
        evaluations = skill_info.get("evaluations", [])
        if not isinstance(evaluations, list) or not evaluations:
            continue
        # "overall" is the weighted aggregate score written by skill-evaluate.py
        scores = [e.get("overall", 0) for e in evaluations
                  if isinstance(e, dict) and "overall" in e]
        if not scores:
            continue
        avg = sum(scores) / len(scores)
        skill_avg_quality[skill_name] = avg

        if avg < 0.30:
            retire.append({
                "skill": skill_name,
                "reason": "Quality below 0.30 threshold (avg {})".format(round(avg, 3)),
                "evidence": "{} evaluations, avg quality {}".format(len(scores), round(avg, 3)),
            })
        elif avg < 0.50:
            improve.append({
                "skill": skill_name,
                "reason": "Quality between 0.30-0.50, improvement needed (avg {})".format(round(avg, 3)),
                "evidence": "{} evaluations, avg quality {}".format(len(scores), round(avg, 3)),
            })

    # --- Substitute: similar_to relations where alternative is better ---
    for rel in relations:
        if not isinstance(rel, dict):
            continue
        if rel.get("type") != "similar_to":
            continue
        source = rel.get("source", "")
        target = rel.get("target", "")
        source_q = skill_avg_quality.get(source)
        target_q = skill_avg_quality.get(target)

        if source_q is not None and target_q is not None:
            if target_q > source_q:
                substitute.append({
                    "skill": source,
                    "reason": "Similar skill '{}' has higher quality ({} vs {})".format(
                        target, round(target_q, 3), round(source_q, 3)),
                    "evidence": "Relation: {} --similar_to--> {}".format(source, target),
                })
            elif source_q > target_q:
                substitute.append({
                    "skill": target,
                    "reason": "Similar skill '{}' has higher quality ({} vs {})".format(
                        source, round(source_q, 3), round(target_q, 3)),
                    "evidence": "Relation: {} --similar_to--> {}".format(source, target),
                })

    output = {
        "forge": forge,
        "retire": retire,
        "improve": improve,
        "substitute": substitute,
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))


def cmd_trend(args):
    """Quality trend per skill over evaluations."""
    window = args.window
    quality = read_yaml(QUALITY_PATH)
    skills_data = quality.get("skills", {})
    if not isinstance(skills_data, dict):
        skills_data = {}

    results = []
    for skill_name, skill_info in sorted(skills_data.items()):
        if not isinstance(skill_info, dict):
            continue
        evaluations = skill_info.get("evaluations", [])
        if not isinstance(evaluations, list) or not evaluations:
            continue

        # Take last N evaluations
        recent = evaluations[-window:]
        # "overall" is the weighted aggregate score written by skill-evaluate.py
        scores = [e.get("overall", 0) for e in recent
                  if isinstance(e, dict) and "overall" in e]
        if len(scores) < 2:
            # Not enough data for a trend
            dates = [e.get("date", "") for e in recent
                     if isinstance(e, dict) and "date" in e]
            results.append({
                "skill": skill_name,
                "evaluations": len(scores),
                "oldest_date": min(dates) if dates else None,
                "newest_date": max(dates) if dates else None,
                "trend": "stable",
                "delta": 0.0,
            })
            continue

        # Simple linear trend: compare first half avg to second half avg
        mid = len(scores) // 2
        first_half = scores[:mid] if mid > 0 else scores[:1]
        second_half = scores[mid:]

        avg_first = sum(first_half) / len(first_half)
        avg_second = sum(second_half) / len(second_half)
        delta = round(avg_second - avg_first, 3)

        # Determine trend direction with a small tolerance
        tolerance = 0.05
        if delta > tolerance:
            trend = "improving"
        elif delta < -tolerance:
            trend = "declining"
        else:
            trend = "stable"

        dates = [e.get("date", "") for e in recent
                 if isinstance(e, dict) and "date" in e]

        results.append({
            "skill": skill_name,
            "evaluations": len(scores),
            "oldest_date": min(dates) if dates else None,
            "newest_date": max(dates) if dates else None,
            "trend": trend,
            "delta": delta,
        })

    print(json.dumps(results, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Skill analytics and reporting")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("reuse-report", help="Per-skill usage and quality report")
    sub.add_parser("co-invocation", help="Skill co-invocation patterns")
    sub.add_parser("coverage", help="Goal category skill coverage")
    sub.add_parser("recommendations", help="Forge/retire/improve suggestions")

    p_trend = sub.add_parser("trend", help="Quality trend per skill")
    p_trend.add_argument("--window", type=int, default=10)

    args = parser.parse_args()
    cmds = {
        "reuse-report": cmd_reuse_report,
        "co-invocation": cmd_co_invocation,
        "coverage": cmd_coverage,
        "recommendations": cmd_recommendations,
        "trend": cmd_trend,
    }
    cmds[args.command](args)


if __name__ == "__main__":
    main()
