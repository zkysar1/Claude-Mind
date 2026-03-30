#!/usr/bin/env python3
"""Skill quality evaluation across five dimensions.

Records per-skill quality evaluations (safety, completeness, executability,
maintainability, cost_awareness), maintains rolling aggregates, and reports
on underperforming skills. Dimension weights are agent-tunable via
meta/skill-quality-strategy.yaml.
"""

import argparse
import json
import sys
from datetime import datetime

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

from _paths import META_DIR


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

QUALITY_PATH = META_DIR / "skill-quality.yaml"
STRATEGY_PATH = META_DIR / "skill-quality-strategy.yaml"
ROLLING_WINDOW = 20  # Keep last 20 evaluations per skill
GRADE_MAP = {"good": 1.0, "average": 0.5, "poor": 0.0}
DIMENSIONS = ["safety", "completeness", "executability", "maintainability", "cost_awareness"]

DEFAULT_WEIGHTS = {
    "safety": 0.30,
    "completeness": 0.25,
    "executability": 0.20,
    "maintainability": 0.15,
    "cost_awareness": 0.10,
}


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


def write_yaml(path, data):
    """Atomically write data as YAML."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    tmp.replace(path)


def load_weights():
    """Load dimension weights from meta/skill-quality-strategy.yaml.

    Returns dict of dimension -> weight. Falls back to defaults if file
    is missing or dimension_weights key is absent.
    """
    data = read_yaml(STRATEGY_PATH)
    raw = data.get("dimension_weights")
    if not isinstance(raw, dict):
        return dict(DEFAULT_WEIGHTS)
    weights = {}
    for dim in DIMENSIONS:
        weights[dim] = float(raw.get(dim, DEFAULT_WEIGHTS.get(dim, 0.0)))
    return weights


def compute_overall(scores, weights):
    """Compute weighted overall score from dimension scores and weights.

    Both scores and weights are dicts keyed by dimension name.
    """
    total = 0.0
    for dim in DIMENSIONS:
        total += scores.get(dim, 0.0) * weights.get(dim, 0.0)
    return round(total, 4)


def compute_aggregate(evaluations, weights):
    """Compute aggregate scores from a list of evaluation entries.

    Returns dict with each dimension's mean plus overall.
    """
    if not evaluations:
        return {dim: 0.0 for dim in DIMENSIONS + ["overall"]}

    count = len(evaluations)
    sums = {dim: 0.0 for dim in DIMENSIONS}
    for entry in evaluations:
        for dim in DIMENSIONS:
            sums[dim] += entry.get(dim, 0.0)

    means = {dim: round(sums[dim] / count, 4) for dim in DIMENSIONS}
    means["overall"] = compute_overall(means, weights)
    return means


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_score(args):
    """Record a quality evaluation for a skill execution."""
    data = read_yaml(QUALITY_PATH)
    weights = load_weights()

    # Map grade strings to numeric values
    scores = {
        "safety": GRADE_MAP[args.safety],
        "completeness": GRADE_MAP[args.completeness],
        "executability": GRADE_MAP[args.executability],
        "maintainability": GRADE_MAP[args.maintainability],
        "cost_awareness": GRADE_MAP[args.cost_awareness],
    }
    overall = compute_overall(scores, weights)

    # Build evaluation entry
    entry = {
        "goal_id": args.goal,
        "date": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "safety": scores["safety"],
        "completeness": scores["completeness"],
        "executability": scores["executability"],
        "maintainability": scores["maintainability"],
        "cost_awareness": scores["cost_awareness"],
        "overall": overall,
    }

    # Ensure skills dict exists
    if "skills" not in data:
        data["skills"] = {}

    skill_name = args.skill
    if skill_name not in data["skills"]:
        data["skills"][skill_name] = {
            "evaluations": [],
            "aggregate": {},
            "total_evaluations": 0,
        }

    skill_data = data["skills"][skill_name]

    # Append to evaluations (FIFO, cap at ROLLING_WINDOW)
    evals = skill_data.get("evaluations", [])
    evals.append(entry)
    if len(evals) > ROLLING_WINDOW:
        evals = evals[-ROLLING_WINDOW:]
    skill_data["evaluations"] = evals

    # Recompute aggregate from current evaluations
    skill_data["aggregate"] = compute_aggregate(evals, weights)

    # Increment total_evaluations
    skill_data["total_evaluations"] = skill_data.get("total_evaluations", 0) + 1

    # Update root-level timestamp
    data["last_updated"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    write_yaml(QUALITY_PATH, data)

    print("Scored {skill}: overall {overall:.2f} (S:{s} C:{c} E:{e} M:{m} $:{ca})".format(
        skill=skill_name,
        overall=overall,
        s=args.safety,
        c=args.completeness,
        e=args.executability,
        m=args.maintainability,
        ca=args.cost_awareness,
    ))


def cmd_read(args):
    """Read quality data."""
    data = read_yaml(QUALITY_PATH)
    skills = data.get("skills", {})

    if args.skill:
        # Specific skill
        skill_data = skills.get(args.skill)
        if skill_data is None:
            print(json.dumps({"error": "Skill '{}' not found".format(args.skill)},
                             indent=2, ensure_ascii=False))
            return
        print(json.dumps(skill_data, indent=2, ensure_ascii=False))
        return

    if args.all and args.summary:
        # Summary table: one entry per skill
        summary = []
        for name, sdata in skills.items():
            agg = sdata.get("aggregate", {})
            summary.append({
                "skill": name,
                "total_evaluations": sdata.get("total_evaluations", 0),
                "safety": agg.get("safety", 0.0),
                "completeness": agg.get("completeness", 0.0),
                "executability": agg.get("executability", 0.0),
                "maintainability": agg.get("maintainability", 0.0),
                "cost_awareness": agg.get("cost_awareness", 0.0),
                "overall": agg.get("overall", 0.0),
            })
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return

    # Default: entire file
    print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_report(args):
    """Full quality report across all skills."""
    data = read_yaml(QUALITY_PATH)
    skills = data.get("skills", {})

    if not skills:
        print(json.dumps({
            "skills": {},
            "summary": {
                "total_skills_evaluated": 0,
                "avg_overall": 0.0,
                "min_overall": 0.0,
                "max_overall": 0.0,
            },
            "alerts": [],
        }, indent=2, ensure_ascii=False))
        return

    # Build skills aggregate dict
    skills_agg = {}
    overalls = []
    alerts = []

    for name, sdata in skills.items():
        agg = sdata.get("aggregate", {})
        skills_agg[name] = agg
        overall = agg.get("overall", 0.0)
        overalls.append(overall)

        # Check for dimensions below 0.30
        low_dims = [dim for dim in DIMENSIONS if agg.get(dim, 0.0) < 0.30]
        if low_dims:
            alerts.append({
                "skill": name,
                "dimensions_below_030": low_dims,
                "values": {dim: agg.get(dim, 0.0) for dim in low_dims},
            })

    result = {
        "skills": skills_agg,
        "summary": {
            "total_skills_evaluated": len(skills),
            "avg_overall": round(sum(overalls) / len(overalls), 4) if overalls else 0.0,
            "min_overall": round(min(overalls), 4) if overalls else 0.0,
            "max_overall": round(max(overalls), 4) if overalls else 0.0,
        },
        "alerts": alerts,
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_underperforming(args):
    """Skills below threshold on any dimension."""
    data = read_yaml(QUALITY_PATH)
    skills = data.get("skills", {})
    threshold = args.threshold

    results = []
    for name, sdata in skills.items():
        agg = sdata.get("aggregate", {})
        overall = agg.get("overall", 0.0)

        # Check each dimension against threshold
        dims_below = []
        for dim in DIMENSIONS:
            val = agg.get(dim, 0.0)
            if val < threshold:
                dims_below.append({"dimension": dim, "value": val})

        # Include if overall < threshold OR any individual dimension < threshold
        if overall < threshold or dims_below:
            results.append({
                "skill": name,
                "overall": overall,
                "dimensions_below": dims_below,
                "total_evaluations": sdata.get("total_evaluations", 0),
            })

    # Sort by overall ascending (worst first)
    results.sort(key=lambda x: x["overall"])

    print(json.dumps(results, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Skill quality evaluation (five dimensions)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_score = sub.add_parser("score", help="Record skill quality evaluation")
    p_score.add_argument("--skill", required=True)
    p_score.add_argument("--goal", required=True)
    p_score.add_argument("--safety", required=True, choices=["good", "average", "poor"])
    p_score.add_argument("--completeness", required=True, choices=["good", "average", "poor"])
    p_score.add_argument("--executability", required=True, choices=["good", "average", "poor"])
    p_score.add_argument("--maintainability", required=True, choices=["good", "average", "poor"])
    p_score.add_argument("--cost-awareness", required=True, choices=["good", "average", "poor"])

    p_read = sub.add_parser("read", help="Read quality data")
    p_read.add_argument("--skill")
    p_read.add_argument("--all", action="store_true")
    p_read.add_argument("--summary", action="store_true")

    sub.add_parser("report", help="Full quality report")

    p_under = sub.add_parser("underperforming", help="Skills below quality threshold")
    p_under.add_argument("--threshold", type=float, default=0.50)

    args = parser.parse_args()
    cmds = {"score": cmd_score, "read": cmd_read, "report": cmd_report, "underperforming": cmd_underperforming}
    cmds[args.command](args)


if __name__ == "__main__":
    main()
