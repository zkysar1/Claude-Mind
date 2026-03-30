#!/usr/bin/env python3
"""A/B experiment lifecycle management for meta-strategies.

Subcommands:
  create  — create a new experiment (baseline vs variant)
  status  — check experiment status
  resolve — resolve experiment (adopt variant or revert to baseline)
  list    — list active or completed experiments
"""

import argparse
import json
import sys
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

from _paths import META_DIR, CONFIG_DIR


def read_yaml(path):
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if data is not None else {}


def write_yaml(path, data):
    """Atomically write YAML with locking and history."""
    from _fileops import locked_write_yaml
    locked_write_yaml(path, data)


def next_id(experiments):
    """Generate next experiment ID."""
    existing = [e.get("id", "") for e in experiments]
    max_num = 0
    for eid in existing:
        if eid.startswith("exp-meta-"):
            try:
                num = int(eid.split("-")[-1])
                max_num = max(max_num, num)
            except ValueError:
                pass
    return f"exp-meta-{max_num + 1:03d}"


def cmd_create(args):
    """Create a new A/B experiment."""
    active = read_yaml(META_DIR / "experiments" / "active-experiments.yaml")
    experiments = active.get("experiments", [])

    # Check max concurrent
    config = read_yaml(CONFIG_DIR / "meta.yaml")
    max_concurrent = config.get("experiments", {}).get("max_concurrent", 1)
    if len(experiments) >= max_concurrent:
        print(json.dumps({"error": f"Max {max_concurrent} concurrent experiments"}), file=sys.stderr)
        sys.exit(1)

    exp_id = next_id(experiments)
    experiment = {
        "id": exp_id,
        "created": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "strategy_file": args.strategy,
        "field": args.field,
        "baseline_value": float(args.baseline),
        "variant_value": float(args.variant),
        "status": "active",
        "phase": "baseline",
        "total_goals": 0,
        "metrics": {"baseline": [], "variant": []},
    }

    experiments.append(experiment)
    active["experiments"] = experiments
    write_yaml(META_DIR / "experiments" / "active-experiments.yaml", active)

    print(json.dumps({"status": "created", "id": exp_id, "strategy": args.strategy, "field": args.field}))


def cmd_status(args):
    """Check experiment status."""
    active = read_yaml(META_DIR / "experiments" / "active-experiments.yaml")
    experiments = active.get("experiments", [])

    if args.id:
        for exp in experiments:
            if exp["id"] == args.id:
                print(json.dumps(exp, ensure_ascii=False, default=str))
                return
        print(json.dumps({"error": f"Experiment {args.id} not found"}), file=sys.stderr)
        sys.exit(1)
    else:
        print(json.dumps({"active_experiments": len(experiments), "experiments": experiments}, ensure_ascii=False, default=str))


def cmd_resolve(args):
    """Resolve an experiment: adopt variant or revert to baseline."""
    active = read_yaml(META_DIR / "experiments" / "active-experiments.yaml")
    completed = read_yaml(META_DIR / "experiments" / "completed-experiments.yaml")
    experiments = active.get("experiments", [])
    completed_list = completed.get("experiments", [])

    target = None
    remaining = []
    for exp in experiments:
        if exp["id"] == args.id:
            target = exp
        else:
            remaining.append(exp)

    if not target:
        print(json.dumps({"error": f"Experiment {args.id} not found"}), file=sys.stderr)
        sys.exit(1)

    # Compute result
    baseline_metrics = target.get("metrics", {}).get("baseline", [])
    variant_metrics = target.get("metrics", {}).get("variant", [])

    if baseline_metrics and variant_metrics:
        baseline_avg = sum(baseline_metrics) / len(baseline_metrics)
        variant_avg = sum(variant_metrics) / len(variant_metrics)
        delta = variant_avg - baseline_avg
    else:
        delta = 0.0

    config = read_yaml(CONFIG_DIR / "meta.yaml")
    threshold = config.get("experiments", {}).get("significance_threshold", 0.05)

    if delta > threshold:
        outcome = "adopted"
    elif delta < -threshold:
        outcome = "reverted"
    else:
        outcome = "inconclusive"

    target["resolved"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    target["outcome"] = outcome
    target["delta"] = round(delta, 6)
    target["status"] = "resolved"

    completed_list.append(target)
    completed["experiments"] = completed_list
    active["experiments"] = remaining

    write_yaml(META_DIR / "experiments" / "active-experiments.yaml", active)
    write_yaml(META_DIR / "experiments" / "completed-experiments.yaml", completed)

    print(json.dumps({"status": "resolved", "id": args.id, "outcome": outcome, "delta": round(delta, 6)}))


def cmd_list(args):
    """List experiments."""
    if args.completed:
        data = read_yaml(META_DIR / "experiments" / "completed-experiments.yaml")
        experiments = data.get("experiments", [])
    else:
        data = read_yaml(META_DIR / "experiments" / "active-experiments.yaml")
        experiments = data.get("experiments", [])

    print(json.dumps({"count": len(experiments), "experiments": experiments}, ensure_ascii=False, default=str))


def build_parser():
    parser = argparse.ArgumentParser(description="A/B experiment management")
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="Create experiment")
    p_create.add_argument("--strategy", required=True)
    p_create.add_argument("--field", required=True)
    p_create.add_argument("--baseline", required=True)
    p_create.add_argument("--variant", required=True)

    p_status = sub.add_parser("status", help="Check status")
    p_status.add_argument("--id", default=None)

    p_resolve = sub.add_parser("resolve", help="Resolve experiment")
    p_resolve.add_argument("--id", required=True)

    p_list = sub.add_parser("list", help="List experiments")
    p_list.add_argument("--completed", action="store_true")

    return parser


DISPATCH = {
    "create": cmd_create,
    "status": cmd_status,
    "resolve": cmd_resolve,
    "list": cmd_list,
}


def main():
    parser = build_parser()
    args = parser.parse_args()
    DISPATCH[args.command](args)


if __name__ == "__main__":
    main()
