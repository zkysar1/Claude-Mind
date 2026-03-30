#!/usr/bin/env python3
"""Improvement velocity (imp@k) computation and tracking.

Computes the rate of learning improvement over rolling windows.
imp@k = (metric_after - metric_before) / k goals

Subcommands:
  compute   — compute current imp@k for a metric over a window
  snapshot  — record a learning_value entry for a goal
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


def cmd_compute(args):
    """Compute improvement velocity over a window."""
    vel = read_yaml(META_DIR / "improvement-velocity.yaml")
    entries = vel.get("entries", [])

    window = args.window
    if len(entries) < window:
        result = {
            "metric": args.metric,
            "window": window,
            "imp_at_k": 0.0,
            "direction": "insufficient_data",
            "entries_available": len(entries),
        }
    else:
        recent = entries[-window:]
        older = entries[-(window * 2):-window] if len(entries) >= window * 2 else entries[:len(entries) - window]

        if older:
            recent_avg = sum(e.get("learning_value", 0) for e in recent) / len(recent)
            older_avg = sum(e.get("learning_value", 0) for e in older) / len(older)
            imp = (recent_avg - older_avg) / window
        else:
            recent_avg = sum(e.get("learning_value", 0) for e in recent) / len(recent)
            imp = recent_avg / window

        direction = "improving" if imp > 0.001 else ("declining" if imp < -0.001 else "stable")
        result = {
            "metric": args.metric,
            "window": window,
            "imp_at_k": round(imp, 6),
            "direction": direction,
            "recent_avg": round(recent_avg, 4),
        }

    print(json.dumps(result, ensure_ascii=False))


def cmd_snapshot(args):
    """Record a learning_value entry for a goal."""
    vel = read_yaml(META_DIR / "improvement-velocity.yaml")
    if "entries" not in vel:
        vel["entries"] = []

    entry = {
        "goal_id": args.goal_id,
        "date": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "learning_value": float(args.learning_value),
    }
    if args.category:
        entry["category"] = args.category
    if args.active_changes:
        entry["active_meta_changes"] = [c.strip() for c in args.active_changes.split(",") if c.strip()]

    vel["entries"].append(entry)

    # Recompute rolling averages
    entries = vel["entries"]
    for w in [5, 10, 20]:
        key = f"window_{w}"
        if len(entries) >= w:
            avg = sum(e.get("learning_value", 0) for e in entries[-w:]) / w
            vel.setdefault("rolling_averages", {})[key] = round(avg, 4)
        else:
            vel.setdefault("rolling_averages", {})[key] = 0.0

    write_yaml(META_DIR / "improvement-velocity.yaml", vel)
    print(json.dumps({"status": "recorded", "goal_id": args.goal_id, "learning_value": entry["learning_value"]}))


def build_parser():
    parser = argparse.ArgumentParser(description="Improvement velocity (imp@k)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_compute = sub.add_parser("compute", help="Compute imp@k for a metric")
    p_compute.add_argument("--window", type=int, required=True, help="Rolling window size (k)")
    p_compute.add_argument("--metric", required=True, help="Metric name to compute")

    p_snap = sub.add_parser("snapshot", help="Record learning value for a goal")
    p_snap.add_argument("--goal-id", required=True, help="Goal ID")
    p_snap.add_argument("--learning-value", required=True, help="Learning value (0-1)")
    p_snap.add_argument("--category", default="", help="Goal category")
    p_snap.add_argument("--active-changes", default="", help="Comma-separated mc-NNN IDs of active backpressure monitors")

    return parser


DISPATCH = {
    "compute": cmd_compute,
    "snapshot": cmd_snapshot,
}


def main():
    parser = build_parser()
    args = parser.parse_args()
    DISPATCH[args.command](args)


if __name__ == "__main__":
    main()
