#!/usr/bin/env python3
"""Backpressure gate for meta-strategy changes.

Monitors meta-strategy changes and auto-reverts when performance regresses.
Inspired by AutoContext's backpressure gate system.

Subcommands:
  monitor   — create a new monitor for a meta-strategy change
  check     — check all active monitors against a new learning_value
  graduate  — stop monitoring a change (performance sustained)
  status    — list all active monitors and rollback history
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


def load_config():
    """Load backpressure config from core/config/meta.yaml."""
    meta_config = CONFIG_DIR / "meta.yaml"
    if not meta_config.exists():
        return {
            "regression_window": 5,
            "graduation_window": 15,
            "baseline_tolerance": -0.10,
            "max_active_monitors": 5,
        }
    with open(meta_config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return config.get("strategy_schemas", {}).get("backpressure", {
        "regression_window": 5,
        "graduation_window": 15,
        "baseline_tolerance": -0.10,
        "max_active_monitors": 5,
    })


BP_PATH = META_DIR / "backpressure.yaml"


def ensure_state():
    """Ensure backpressure.yaml exists with valid structure."""
    data = read_yaml(BP_PATH)
    if "version" not in data:
        data = {
            "version": 1,
            "active_monitors": [],
            "rollback_history": [],
        }
        write_yaml(BP_PATH, data)
    return data


def cmd_monitor(args):
    """Create a new monitor for a meta-strategy change."""
    config = load_config()
    data = ensure_state()

    monitors = data.get("active_monitors", [])

    # Enforce max_active_monitors
    if len(monitors) >= config.get("max_active_monitors", 5):
        evicted_id = monitors[0].get("meta_change_id", "?")
        monitors.pop(0)
        print(f"BACKPRESSURE: Evicted oldest monitor {evicted_id} (cap reached)", file=sys.stderr)

    monitor = {
        "meta_change_id": args.change_id,
        "strategy_file": args.file,
        "field": args.field,
        "old_value": _parse_value(args.old),
        "new_value": _parse_value(args.new),
        "baseline_imp_k": float(args.baseline),
        "goals_since_change": 0,
        "imp_k_samples": [],
        "consecutive_below_baseline": 0,
        "consecutive_above_baseline": 0,
        "status": "monitoring",
        "created": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }

    monitors.append(monitor)
    data["active_monitors"] = monitors
    write_yaml(BP_PATH, data)

    print(json.dumps({"status": "created", "meta_change_id": args.change_id}))


def cmd_check(args):
    """Check all active monitors against a new learning_value.

    Returns JSON with any rollback actions needed.
    """
    config = load_config()
    data = ensure_state()

    learning_value = float(args.learning_value)
    regression_window = config.get("regression_window", 5)
    graduation_window = config.get("graduation_window", 15)
    baseline_tolerance = config.get("baseline_tolerance", -0.10)

    rollback_actions = []
    graduated = []
    monitors = data.get("active_monitors", [])

    for monitor in monitors:
        if monitor["status"] != "monitoring":
            continue

        monitor["goals_since_change"] += 1
        monitor["imp_k_samples"].append(learning_value)

        baseline = monitor["baseline_imp_k"]
        threshold = baseline + baseline_tolerance

        if learning_value < threshold:
            monitor["consecutive_below_baseline"] += 1
            monitor["consecutive_above_baseline"] = 0
        else:
            monitor["consecutive_below_baseline"] = 0
            monitor["consecutive_above_baseline"] += 1

        # Check for regression → rollback
        if monitor["consecutive_below_baseline"] >= regression_window:
            monitor["status"] = "rolled_back"
            # Count total goals for cooldown tracking
            vel = read_yaml(META_DIR / "improvement-velocity.yaml")
            total_goals_now = len(vel.get("entries", []))
            rollback = {
                "meta_change_id": monitor["meta_change_id"],
                "strategy_file": monitor["strategy_file"],
                "field": monitor["field"],
                "rollback_to": monitor["old_value"],
                "failed_value": monitor["new_value"],  # the value that caused regression
                "rolled_back_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "reason": f"{monitor['consecutive_below_baseline']} consecutive goals below baseline ({baseline:.4f} + tolerance {baseline_tolerance})",
                "imp_k_at_rollback": learning_value,
                "goals_measured": monitor["goals_since_change"],
                "total_goals_at_rollback": total_goals_now,
            }
            rollback_actions.append(rollback)
            data.setdefault("rollback_history", []).append(rollback)

        # Check for graduation → sustained improvement
        elif monitor["consecutive_above_baseline"] >= graduation_window:
            monitor["status"] = "graduated"
            graduated.append(monitor["meta_change_id"])

    # Remove rolled-back and graduated monitors from active list
    data["active_monitors"] = [m for m in monitors if m["status"] == "monitoring"]
    write_yaml(BP_PATH, data)

    # Only report dead end candidates for fields that were JUST rolled back in
    # this check run. Avoids repeated registration on every subsequent call.
    newly_rolled_fields = {f"{a['strategy_file']}:{a['field']}" for a in rollback_actions}
    dead_end_candidates = _check_dead_end_candidates(data, newly_rolled_fields) if newly_rolled_fields else []

    result = {
        "rollback_actions": rollback_actions,
        "graduated": graduated,
        "dead_end_candidates": dead_end_candidates,
        "active_monitors_count": len(data["active_monitors"]),
    }
    print(json.dumps(result, ensure_ascii=False, default=str))


def _check_dead_end_candidates(data, newly_rolled_fields):
    """Check if a newly-rolled-back field has been rolled back 2+ times total."""
    history = data.get("rollback_history", [])
    from collections import Counter
    field_rollbacks = Counter()
    for rb in history:
        key = f"{rb['strategy_file']}:{rb['field']}"
        field_rollbacks[key] += 1

    candidates = []
    for key in newly_rolled_fields:
        count = field_rollbacks.get(key, 0)
        if count >= 2:
            file_part, field_part = key.split(":", 1)
            evidence = [rb["meta_change_id"] for rb in history
                        if f"{rb['strategy_file']}:{rb['field']}" == key]
            failed_values = [rb.get("failed_value") for rb in history
                             if f"{rb['strategy_file']}:{rb['field']}" == key
                             and rb.get("failed_value") is not None]
            candidates.append({
                "strategy_file": file_part,
                "field": field_part,
                "rollback_count": count,
                "evidence": evidence,
                "failed_values": failed_values,
            })
    return candidates


def cmd_graduate(args):
    """Graduate a monitor — stop monitoring."""
    data = ensure_state()
    monitors = data.get("active_monitors", [])

    found = False
    for monitor in monitors:
        if monitor["meta_change_id"] == args.change_id:
            monitor["status"] = "graduated"
            found = True
            break

    if not found:
        print(json.dumps({"error": f"Monitor {args.change_id} not found"}))
        return

    data["active_monitors"] = [m for m in monitors if m["status"] == "monitoring"]
    write_yaml(BP_PATH, data)
    print(json.dumps({"status": "graduated", "meta_change_id": args.change_id}))


def cmd_status(args):
    """List all active monitors and rollback history."""
    data = ensure_state()
    result = {
        "active_monitors": data.get("active_monitors", []),
        "rollback_history": data.get("rollback_history", []),
        "active_count": len(data.get("active_monitors", [])),
        "total_rollbacks": len(data.get("rollback_history", [])),
    }
    print(json.dumps(result, ensure_ascii=False, default=str))


def cmd_cooldown_check(args):
    """Check if a field is in cooldown (rolled back within last N goals)."""
    data = ensure_state()
    history = data.get("rollback_history", [])
    cooldown_window = int(args.window) if args.window else 20

    vel = read_yaml(META_DIR / "improvement-velocity.yaml")
    total_goals = len(vel.get("entries", []))

    in_cooldown = []
    for rb in history:
        rollback_at = rb.get("total_goals_at_rollback")
        if rollback_at is None:
            continue  # legacy record without tracking — skip
        goals_since_rollback = total_goals - rollback_at
        if goals_since_rollback < cooldown_window:
            in_cooldown.append({
                "strategy_file": rb["strategy_file"],
                "field": rb["field"],
                "rolled_back_at": rb["rolled_back_at"],
                "goals_since_rollback": goals_since_rollback,
            })

    result = {
        "in_cooldown": in_cooldown,
        "cooldown_window": cooldown_window,
    }
    print(json.dumps(result, ensure_ascii=False, default=str))


def _parse_value(raw):
    """Auto-detect type from string."""
    if raw == "null":
        return None
    if raw == "true":
        return True
    if raw == "false":
        return False
    try:
        return int(raw)
    except (ValueError, TypeError):
        pass
    try:
        return float(raw)
    except (ValueError, TypeError):
        pass
    return raw


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description="Backpressure gate for meta-strategy changes")
    sub = parser.add_subparsers(dest="command", required=True)

    p_mon = sub.add_parser("monitor", help="Create a new monitor")
    p_mon.add_argument("--change-id", required=True, help="Meta change ID (mc-NNN)")
    p_mon.add_argument("--file", required=True, help="Strategy file (relative to meta/)")
    p_mon.add_argument("--field", required=True, help="Dot-notation field path")
    p_mon.add_argument("--old", required=True, help="Old value before change")
    p_mon.add_argument("--new", required=True, help="New value after change")
    p_mon.add_argument("--baseline", required=True, help="Current imp@k baseline")

    p_check = sub.add_parser("check", help="Check monitors against a learning value")
    p_check.add_argument("--learning-value", required=True, help="Learning value from Step 8.8")

    p_grad = sub.add_parser("graduate", help="Graduate a monitor")
    p_grad.add_argument("--change-id", required=True, help="Meta change ID to graduate")

    sub.add_parser("status", help="List monitors and rollback history")

    p_cool = sub.add_parser("cooldown-check", help="Check if fields are in cooldown")
    p_cool.add_argument("--window", default="20", help="Cooldown window in goals (default: 20)")

    return parser


DISPATCH = {
    "monitor": cmd_monitor,
    "check": cmd_check,
    "graduate": cmd_graduate,
    "status": cmd_status,
    "cooldown-check": cmd_cooldown_check,
}


def main():
    parser = build_parser()
    args = parser.parse_args()
    DISPATCH[args.command](args)


if __name__ == "__main__":
    main()
