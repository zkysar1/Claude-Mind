#!/usr/bin/env python3
"""Strategy generation tracking for meta-strategy configurations.

Maps parameter configurations to performance. Each "generation" = a set of
active meta-strategy parameter values. When any parameter changes, close the
old generation and open a new one. Inspired by AutoContext's score trajectory.

Subcommands:
  snapshot  — capture current parameter values
  close     — close current generation with final metrics
  open      — start a new generation (auto-snapshots parameters)
  update    — update current generation metrics with a new goal
  status    — current generation info + peak comparison
  history   — list generations sorted by avg_learning_value
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

from _paths import META_DIR


GEN_PATH = META_DIR / "strategy-generations.yaml"

# Meta-strategy files to snapshot for parameter tracking.
# Keep in sync with meta-yaml.py _trigger_generation_transition.
STRATEGY_FILES = [
    "goal-selection-strategy.yaml",
    "reflection-strategy.yaml",
    "evolution-strategy.yaml",
    "aspiration-generation-strategy.yaml",
    "encoding-strategy.yaml",
    "skill-quality-strategy.yaml",
]


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


def ensure_state():
    """Ensure strategy-generations.yaml exists."""
    data = read_yaml(GEN_PATH)
    if "version" not in data:
        data = {
            "version": 1,
            "current_generation": 0,
            "generations": [],
            "peak_generation": None,
            "peak_score": 0.0,
        }
        write_yaml(GEN_PATH, data)
    return data


def capture_snapshot():
    """Capture current values from all meta-strategy files."""
    snapshot = {}
    for filename in STRATEGY_FILES:
        path = META_DIR / filename
        if not path.exists():
            continue
        data = read_yaml(path)
        # Flatten key parameters (weights, depth_allocation, etc.)
        prefix = filename.replace(".yaml", "").replace("-", "_")
        _flatten(data, prefix, snapshot)
    return snapshot


def _flatten(data, prefix, result, max_depth=2, depth=0):
    """Flatten a dict to dot-notation keys, limited depth."""
    if depth >= max_depth or not isinstance(data, dict):
        return
    for key, value in data.items():
        full_key = f"{prefix}.{key}"
        if isinstance(value, (int, float, str, bool, type(None))):
            result[full_key] = value
        elif isinstance(value, dict) and depth < max_depth - 1:
            _flatten(value, full_key, result, max_depth, depth + 1)


def cmd_snapshot(args):
    """Capture and print current parameter snapshot."""
    snapshot = capture_snapshot()
    print(json.dumps(snapshot, ensure_ascii=False, default=str))


def cmd_close(args):
    """Close the current generation with final metrics."""
    data = ensure_state()
    generations = data.get("generations", [])

    if not generations:
        print(json.dumps({"error": "No active generation to close"}))
        return

    current = generations[-1]
    if current.get("ended") is not None:
        print(json.dumps({"error": "Current generation already closed"}))
        return

    current["ended"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    # Apply provided metrics if any
    if args.metrics:
        metrics = json.loads(args.metrics)
        current.setdefault("metrics", {}).update(metrics)

    # Check and update peak (before single write)
    avg_lv = current.get("metrics", {}).get("avg_learning_value", 0.0)
    if avg_lv > data.get("peak_score", 0.0):
        data["peak_generation"] = current["generation"]
        data["peak_score"] = avg_lv

    write_yaml(GEN_PATH, data)

    print(json.dumps({
        "status": "closed",
        "generation": current["generation"],
        "goals_completed": current.get("goals_completed", 0),
        "avg_learning_value": avg_lv,
    }))


def cmd_open(args):
    """Start a new generation with current parameter snapshot."""
    data = ensure_state()
    generations = data.get("generations", [])

    # Close current if still open
    if generations and generations[-1].get("ended") is None:
        generations[-1]["ended"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    new_gen_num = data.get("current_generation", 0) + 1
    snapshot = capture_snapshot()

    new_gen = {
        "generation": new_gen_num,
        "started": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "ended": None,
        "goals_completed": 0,
        "parameter_snapshot": snapshot,
        "metrics": {
            "avg_learning_value": 0.0,
            "total_learning_value": 0.0,
            "avg_goal_completion_rate": 0.0,
            "pipeline_accuracy": 0.0,
        },
        "best_score": 0.0,
        "worst_score": 1.0,
    }

    generations.append(new_gen)
    data["generations"] = generations
    data["current_generation"] = new_gen_num
    write_yaml(GEN_PATH, data)

    print(json.dumps({
        "status": "opened",
        "generation": new_gen_num,
        "parameters_tracked": len(snapshot),
    }))


def cmd_update(args):
    """Update current generation metrics with a new goal's learning_value."""
    data = ensure_state()
    generations = data.get("generations", [])

    # Auto-open generation 1 if none exists yet (before any meta-set.sh call)
    if not generations or generations[-1].get("ended") is not None:
        new_num = data.get("current_generation", 0) + 1
        snapshot = capture_snapshot()
        generations.append({
            "generation": new_num,
            "started": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "ended": None,
            "goals_completed": 0,
            "parameter_snapshot": snapshot,
            "metrics": {"avg_learning_value": 0.0, "total_learning_value": 0.0},
            "best_score": 0.0,
            "worst_score": 1.0,
        })
        data["generations"] = generations
        data["current_generation"] = new_num

    current = generations[-1]

    learning_value = float(args.learning_value)
    current["goals_completed"] = current.get("goals_completed", 0) + 1

    # Update running metrics
    metrics = current.setdefault("metrics", {})
    total = metrics.get("total_learning_value", 0.0) + learning_value
    metrics["total_learning_value"] = total
    metrics["avg_learning_value"] = round(total / current["goals_completed"], 4)

    # Track best/worst
    current["best_score"] = max(current.get("best_score", 0.0), learning_value)
    current["worst_score"] = min(current.get("worst_score", 1.0), learning_value)

    # Update peak if needed (before single write)
    if metrics["avg_learning_value"] > data.get("peak_score", 0.0) and current["goals_completed"] >= 3:
        data["peak_generation"] = current["generation"]
        data["peak_score"] = metrics["avg_learning_value"]

    write_yaml(GEN_PATH, data)

    print(json.dumps({
        "status": "updated",
        "generation": current["generation"],
        "goals_completed": current["goals_completed"],
        "avg_learning_value": metrics["avg_learning_value"],
    }))


def cmd_status(args):
    """Current generation info + peak comparison."""
    data = ensure_state()
    generations = data.get("generations", [])

    current = generations[-1] if generations else None
    result = {
        "current_generation": data.get("current_generation", 0),
        "current_goals": current.get("goals_completed", 0) if current else 0,
        "current_avg_lv": current.get("metrics", {}).get("avg_learning_value", 0.0) if current else 0.0,
        "peak_generation": data.get("peak_generation"),
        "peak_score": data.get("peak_score", 0.0),
        "total_generations": len(generations),
    }
    print(json.dumps(result, ensure_ascii=False, default=str))


def cmd_history(args):
    """List generations sorted by avg_learning_value."""
    data = ensure_state()
    generations = data.get("generations", [])

    # Sort by avg_learning_value descending
    sorted_gens = sorted(
        generations,
        key=lambda g: g.get("metrics", {}).get("avg_learning_value", 0.0),
        reverse=True,
    )

    top_n = int(args.top) if args.top else len(sorted_gens)
    sorted_gens = sorted_gens[:top_n]

    result = []
    for gen in sorted_gens:
        result.append({
            "generation": gen["generation"],
            "started": gen.get("started"),
            "ended": gen.get("ended"),
            "goals_completed": gen.get("goals_completed", 0),
            "avg_learning_value": gen.get("metrics", {}).get("avg_learning_value", 0.0),
            "best_score": gen.get("best_score", 0.0),
        })

    print(json.dumps(result, ensure_ascii=False, default=str))


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description="Strategy generation tracking")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("snapshot", help="Capture current parameter values")

    p_close = sub.add_parser("close", help="Close current generation")
    p_close.add_argument("--metrics", default=None, help="Final metrics as JSON")

    sub.add_parser("open", help="Start a new generation")

    p_update = sub.add_parser("update", help="Update current generation with a goal's learning_value")
    p_update.add_argument("--learning-value", required=True, help="Learning value (0-1)")

    sub.add_parser("status", help="Current generation + peak comparison")

    p_hist = sub.add_parser("history", help="List generations by performance")
    p_hist.add_argument("--top", default=None, help="Show top N generations")

    return parser


DISPATCH = {
    "snapshot": cmd_snapshot,
    "close": cmd_close,
    "open": cmd_open,
    "update": cmd_update,
    "status": cmd_status,
    "history": cmd_history,
}


def main():
    parser = build_parser()
    args = parser.parse_args()
    DISPATCH[args.command](args)


if __name__ == "__main__":
    main()
