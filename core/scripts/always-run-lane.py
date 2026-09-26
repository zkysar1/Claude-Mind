#!/usr/bin/env python3
"""always-run-lane.py -- out-of-competition dispatch for recurring goals that must
not depend on winning the goal-selector draw (g-353-137).

WHY. A stochastic ranker with an exploration-noise term cannot guarantee that a
given recurring goal fires on its cadence. Measured under g-353-132: a 36h
owner-directive check-in sat at noise-free rank 71-181 with its whole score
signal inside one noise width, so it fired only when it happened to win the
draw. Tuning weights or priority to make it win is the sub-noise-width move that
measurement already falsified. The guard-1895 corollary names the structural
remedy: REMOVE the item from the competition. This script is that lane.

CONTRACT. A goal opts in with a `dispatch_lane` field:
    "always-run"          -- any agent's lane may pin it
    "always-run:<agent>"  -- only that agent's lane pins it
Per call the lane names AT MOST ONE goal: the most-overdue opted-in goal that is
recurring, status pending, unclaimed (or already claimed by this agent), carries
no defer_reason, and whose interval has elapsed (never achieved counts as due).

IT NEVER OVERRIDES AN ELIGIBILITY GATE. aspirations-select honors the pin ONLY
when goal-selector.sh ALSO lists the goal as a candidate, so a blocked, deferred
or precondition-gated goal stays gated. The lane replaces the SCORE, never the
gates.

Exit 0 always (fail-open). Stdout is one JSON object:
    {"pin": <goal_id|null>, "source": ..., "overdue_x": ..., "scanned": N,
     "due": [...], "error": <str, only on failure>}
A lane failure must never block selection -- the scorer path is the fallback.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys

LANE = "always-run"
TS_FMT = "%Y-%m-%dT%H:%M:%S"


def lane_owner(value) -> tuple[bool, str | None]:
    """(opted_in, owner). owner None means any agent may pin the goal."""
    if not isinstance(value, str):
        return False, None
    if value == LANE:
        return True, None
    if value.startswith(LANE + ":") and value[len(LANE) + 1:]:
        return True, value[len(LANE) + 1:]
    return False, None


def overdue_multiple(goal: dict, now: _dt.datetime) -> float | None:
    """elapsed / interval. inf when never achieved. None when unscorable."""
    try:
        interval = float(goal.get("interval_hours") or 0)
    except (TypeError, ValueError):
        return None
    if interval <= 0:
        return None
    last = goal.get("lastAchievedAt")
    if not last:
        return float("inf")
    try:
        then = _dt.datetime.strptime(str(last)[:19], TS_FMT)
    except ValueError:
        return None
    return (now - then).total_seconds() / 3600.0 / interval


def decide(goals: list[dict], now: _dt.datetime, agent: str) -> dict:
    """Pure: pick the one goal the lane pins, plus the due set for the log."""
    due = []
    for g in goals:
        opted, owner = lane_owner(g.get("dispatch_lane"))
        if not opted or (owner is not None and owner != agent):
            continue
        if not g.get("recurring") or g.get("status") != "pending":
            continue
        claimant = g.get("claimed_by")
        if claimant and claimant != agent:
            continue
        if g.get("defer_reason"):
            continue
        x = overdue_multiple(g, now)
        if x is None or x < 1.0:
            continue
        due.append({"id": g.get("id"), "source": g.get("source"), "overdue_x": x})
    due.sort(key=lambda d: d["overdue_x"], reverse=True)
    top = due[0] if due else None
    return {
        "pin": top["id"] if top else None,
        "source": top["source"] if top else None,
        "overdue_x": (None if not top else
                      ("never" if top["overdue_x"] == float("inf")
                       else round(top["overdue_x"], 2))),
        "due": [d["id"] for d in due],
    }


def _query(value: str) -> list[dict]:
    """Goals whose dispatch_lane equals `value`, both queues (union endpoint)."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _runtime_bash import BASH
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aspirations-query.sh")
    p = subprocess.run([BASH, script, "--goal-field", "dispatch_lane", value, "--full"],
                       capture_output=True, text=True, timeout=120)
    out = (p.stdout or "").strip()
    # The endpoint refuses a field NO goal carries yet (400 unknown_goal_field,
    # reported on stderr with empty stdout). That is a genuine empty, not a fault.
    if "unknown_goal_field" in (p.stderr or "") + out:
        return []
    if not out:
        raise RuntimeError(f"aspirations-query returned no bytes (rc={p.returncode})")
    data = json.loads(out)
    if isinstance(data, dict):
        # 400 unknown_goal_field = no goal carries the field yet: a real empty.
        if data.get("error") == "unknown_goal_field" or "unknown_goal_field" in json.dumps(data):
            return []
        raise RuntimeError(f"aspirations-query error: {json.dumps(data)[:200]}")
    return [g for g in data if isinstance(g, dict)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--agent", default=os.environ.get("MIND_AGENT", ""))
    ap.add_argument("--now", default=None, help="ISO timestamp (tests)")
    args = ap.parse_args(argv)
    now = (_dt.datetime.strptime(args.now[:19], TS_FMT) if args.now
           else _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None, microsecond=0))
    try:
        if not args.agent:
            raise RuntimeError("no agent (set MIND_AGENT or pass --agent)")
        goals = _query(LANE) + _query(f"{LANE}:{args.agent}")
        seen, uniq = set(), []
        for g in goals:
            key = (g.get("id"), g.get("source"))
            if key not in seen:
                seen.add(key)
                uniq.append(g)
        result = decide(uniq, now, args.agent)
        result["scanned"] = len(uniq)
    except Exception as exc:  # fail-open: the scorer path is the fallback
        print(f"[always-run-lane] {exc}", file=sys.stderr)
        result = {"pin": None, "source": None, "overdue_x": None, "due": [],
                  "scanned": 0, "error": str(exc)[:300]}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
