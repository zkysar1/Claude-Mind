#!/usr/bin/env python3
"""goal-close-risk-tier.py — CLI for the close-review risk-tier classifier ().

READ-ONLY. Prints the tier a goal would be classified at. The enforcement half lives
in close-review-gate.py; this exists so the reviewer skill, a human, or a test can ask
"what tier is this goal?" without going near a close.

  py -3 core/scripts/goal-close-risk-tier.py --goal g-357-40 --source world
  py -3 core/scripts/goal-close-risk-tier.py --goal g-357-40 --files core/scripts/x.py

rc 0 always (read-only tool; a classification is not a verdict). Tier is in stdout JSON.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from goal_close_risk_tier import classify  # noqa: E402
from _runtime_bash import bash_cmd  # noqa: E402  guard-580/581


def load_goal(goal_id: str, source: str) -> dict:
    """Fetch the goal record via the canonical query wrapper. Returns {} on any
    failure — the caller degrades to tier 1 rather than guessing (guard-142)."""
    script = Path(__file__).resolve().parent / "aspirations-query.sh"
    if not script.is_file():
        return {}
    try:
        # bash_cmd(script, *args) — script is the FIRST POSITIONAL, never a list
        # (a list makes Path(...).as_posix() raise, which the except swallows into
        # a silent "record unavailable"). Same fix as close-review-gate.py.
        out = subprocess.run(
            bash_cmd(script, "--goal-field", "goal_id", goal_id, "--full"),
            capture_output=True, text=True, timeout=120,
        )
    except Exception:
        return {}
    if out.returncode != 0 or not out.stdout.strip():
        return {}
    try:
        recs = json.loads(out.stdout)
    except Exception:
        return {}
    if isinstance(recs, list) and recs and isinstance(recs[0], dict):
        return recs[0]
    return {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--goal", required=True, help="goal id")
    ap.add_argument("--source", default="world", choices=("world", "agent"))
    ap.add_argument("--files", nargs="*", default=None,
                    help="files touched by this goal (space separated)")
    ap.add_argument("--artifacts-count", type=int, default=None)
    ap.add_argument("--first-of-aspiration", action="store_true")
    ap.add_argument("--goal-json", default=None,
                    help="path to a JSON goal record; bypasses the store read (tests)")
    args = ap.parse_args(argv)

    if args.goal_json:
        try:
            goal = json.loads(Path(args.goal_json).read_text(encoding="utf-8"))
        except Exception:
            goal = {}
    else:
        goal = load_goal(args.goal, args.source)

    if not goal:
        print(json.dumps({
            "goal_id": args.goal, "tier": 1, "reasons": ["goal record unavailable — fail-to-tier-1"],
            "triggers": {}, "degraded": True,
        }, indent=2))
        return 0

    result = classify(
        goal,
        files_touched=args.files,
        artifacts_count=args.artifacts_count,
        is_first_of_aspiration=args.first_of_aspiration,
    )
    result["goal_id"] = args.goal
    result["degraded"] = False
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
