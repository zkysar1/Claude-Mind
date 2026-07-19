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
        raw = f.read()
    # OneDrive sync-corruption guard (1): a transiently un-synced replica of a
    # shared meta hot-file reads as NUL bytes -- full zero-fill, or (on a partial block
    # sync) a real-YAML prefix + NUL suffix. The mechanism is a sync-write race on a
    # frequently rewritten file, NOT Files-On-Demand dehydration: all meta files are
    # pinned ("always keep on this device"), census-verified 0 dehydration-prone of 40,237
    # (1) -- so do not chase a dehydration fix, it is already maxed out. Both
    # forms make yaml.safe_load raise
    # a cryptic "#x0000 ReaderError". Translate to an actionable message so the next reader
    # does not burn an investigation. Behavior is UNCHANGED -- the read still raises, so the
    # write path (cmd_snapshot) aborts and the file is NOT overwritten (no clobber); only the
    # message is clearer and names the recovery path. Recovery is assured by the .history
    # snapshots written on every prior successful write.
    if chr(0) in raw:
        raise ValueError(
            f"{path.name}: {raw.count(chr(0))} NUL byte(s) detected -- transient OneDrive "
            f"sync corruption of a shared meta hot-file. NOT overwriting (read aborts, no "
            f"clobber). Usually transient: re-read after OneDrive re-syncs, or restore a "
            f"known-good version via `py -3 core/scripts/history.py restore '{path}' <version>` "
            f"(list versions with `history.py list '{path}'`). (g-115-1271)"
        )
    data = yaml.safe_load(raw)
    return data if data is not None else {}


def write_yaml(path, data):
    """Atomically write YAML with locking and history."""
    from _fileops import locked_write_yaml
    locked_write_yaml(path, data)


def validate_velocity_structure(data, label=""):
    """Validate structural integrity of improvement-velocity data."""
    prefix = f"[{label}] " if label else ""
    if not isinstance(data, dict):
        raise ValueError(f"{prefix}Expected dict, got {type(data).__name__}")
    allowed_keys = {"entries", "rolling_averages"}
    unexpected = set(data.keys()) - allowed_keys
    if unexpected:
        raise ValueError(f"{prefix}Unexpected top-level keys: {unexpected}")
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        raise ValueError(f"{prefix}'entries' is {type(entries).__name__}, expected list")
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"{prefix}entries[{i}] is {type(entry).__name__}, expected dict")
        if "goal_id" not in entry:
            raise ValueError(f"{prefix}entries[{i}] missing 'goal_id'")
        if "learning_value" not in entry:
            raise ValueError(f"{prefix}entries[{i}] missing 'learning_value'")
    ra = data.get("rolling_averages")
    if ra is not None and not isinstance(ra, dict):
        raise ValueError(f"{prefix}'rolling_averages' is {type(ra).__name__}, expected dict")


def cmd_compute(args):
    """Compute improvement velocity over a window."""
    vel = read_yaml(META_DIR / "improvement-velocity.yaml")
    entries = vel.get("entries", [])

    window = args.window
    if len(entries) < window:
        result = {
            "series": "learning_value",
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
            "series": "learning_value",
            "window": window,
            "imp_at_k": round(imp, 6),
            "direction": direction,
            "recent_avg": round(recent_avg, 4),
        }

    # Non-default label: echo it but say plainly the computation ignored it
    # (1 — the old output echoed the label as "metric", implying
    # per-metric series that never existed).
    if args.metric != "learning_value":
        result["metric_label"] = args.metric
        result["note"] = ("metric is a caller label; computation always "
                          "runs over the single learning_value series")

    print(json.dumps(result, ensure_ascii=False))


def cmd_snapshot(args):
    """Record a learning_value entry for a goal."""
    vel = read_yaml(META_DIR / "improvement-velocity.yaml")
    validate_velocity_structure(vel, "post-read")

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

    # Validate structure before write
    validate_velocity_structure(vel, "pre-write")

    write_yaml(META_DIR / "improvement-velocity.yaml", vel)
    print(json.dumps({"status": "recorded", "goal_id": args.goal_id, "learning_value": entry["learning_value"]}))


def build_parser():
    parser = argparse.ArgumentParser(description="Improvement velocity (imp@k)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_compute = sub.add_parser("compute", help="Compute imp@k over the learning_value series")
    p_compute.add_argument("--window", type=int, required=True, help="Rolling window size (k)")
    # 1: there is exactly ONE series in the store (per-goal
    # learning_value snapshots). The old required --metric was decorative —
    # any name produced identical output, which misled the evolve Step 0.7
    # caller into believing pipeline_accuracy and goal_completion_rate were
    # independently tracked. Optional caller label now; output names the real
    # series and warns on non-default labels.
    p_compute.add_argument("--metric", default="learning_value",
                           help="Caller label only — computation ALWAYS runs "
                                "over the single learning_value series")

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
