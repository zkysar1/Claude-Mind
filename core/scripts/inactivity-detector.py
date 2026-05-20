#!/usr/bin/env python3
"""Detect goal-completion silence across world + agent queues.

Reads all aspiration files (live + archive), finds the latest goal
`completed_date` across all goals, and if `now - latest > silence_hours`
files an Investigate goal asking what's blocking forward motion.

Origin: LifingPolls plan item 3 (2026-05-08).

Distinct from stall-goal-filer.py (which detects loop-hook BLOCK
streaks indicating the loop is crashing) — this measures whether
ANY work is landing, regardless of loop health.

Usage:
  py -3 core/scripts/inactivity-detector.py [--silence-hours N]
                                             [--target-asp asp-001]
                                             [--dry-run]

Self-dedup: if an Investigate goal with title containing "inactivity"
exists in the target aspiration with status pending or in-progress,
the detector skips. Prevents flooding when the agent is genuinely idle
and signals are accumulating.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from _paths import WORLD_DIR, AGENT_DIR  # noqa: E402
import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)

DEFAULT_SILENCE_HOURS = 6
DEFAULT_TARGET_ASP = ""   # framework-maintenance home
DEFAULT_PRIORITY = "MEDIUM"
DEFAULT_CATEGORY = "framework-maintenance"


def _iter_files():
    if WORLD_DIR is not None:
        yield WORLD_DIR / "aspirations.jsonl"
        yield WORLD_DIR / "aspirations-archive.jsonl"
    if AGENT_DIR is not None:
        yield AGENT_DIR / "aspirations.jsonl"
        yield AGENT_DIR / "aspirations-archive.jsonl"


def _latest_goal_completion() -> tuple[datetime | None, str | None]:
    """Scan all aspiration files for the most-recent goal completed_date.

    Returns (latest_dt, source_goal_id). Either may be None if nothing found.
    """
    latest_dt = None
    latest_id = None
    for path in _iter_files():
        if not path.exists():
            continue
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    asp = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for g in asp.get("goals", []):
                    cd = g.get("completed_date")
                    if not cd:
                        continue
                    try:
                        dt = datetime.fromisoformat(str(cd))
                    except (ValueError, TypeError):
                        continue
                    if latest_dt is None or dt > latest_dt:
                        latest_dt = dt
                        latest_id = g.get("id")
    return latest_dt, latest_id


def _existing_inactivity_investigate(asp_id: str) -> str | None:
    """Return goal_id of any pending/in-progress Investigate referencing
    inactivity in the target aspiration."""
    if WORLD_DIR is None:
        return None
    paths = [WORLD_DIR / "aspirations.jsonl"]
    if AGENT_DIR is not None:
        paths.append(AGENT_DIR / "aspirations.jsonl")
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                asp = json.loads(line)
            except json.JSONDecodeError:
                continue
            if asp.get("id") != asp_id:
                continue
            for g in asp.get("goals", []):
                title = (g.get("title") or "").lower()
                if "inactivity" not in title:
                    continue
                if g.get("status") in ("pending", "in-progress"):
                    return g.get("id")
    return None


def _file_investigate(asp_id: str, silence_hours: float, latest_id: str | None,
                      dry_run: bool) -> tuple[bool, str]:
    title = (f"Investigate: Inactivity — no goal completions in "
             f"{silence_hours:.1f}h")
    description = (
        f"No goal completions detected across world + agent queues in the "
        f"last {silence_hours:.1f} hours"
        f"{f' (most recent: {latest_id})' if latest_id else ''}.\n\n"
        f"Possible causes:\n"
        f"  - Loop blocked at a phase or sleeping past expected wake\n"
        f"  - All goals deferred or claimed elsewhere\n"
        f"  - Selector saturation (cargo-cult / class-balance penalties "
        f"blocking eligible work)\n"
        f"  - Infrastructure dependency unavailable\n"
        f"  - Agent session ended without /stop (stale RUNNING state)\n\n"
        f"Diagnose and produce a short root-cause note. If structural "
        f"(framework drift / config), file a follow-up Idea. If transient "
        f"(in-flight work just hadn't completed yet), close routine."
    )
    payload = {
        "title": title,
        "description": description,
        "priority": DEFAULT_PRIORITY,
        "category": DEFAULT_CATEGORY,
        "participants": ["agent"],
        "verification": {
            "outcomes": ["Root-cause note recorded; transient or "
                         "structural classification made"],
            "checks": [], "preconditions": [],
        },
        "origin_signal": "investigate:inactivity-silence",
    }
    if dry_run:
        print(f"[dry-run] would file Investigate on {asp_id}: {title}")
        return True, "dry-run"
    try:
        record = _rt.aspirations_add_goal(asp_id, payload)
        return True, record.get("id") or "filed"
    except _rt.RtError as e:
        return False, (e.body or str(e)).strip()[:200]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--silence-hours", type=float, default=DEFAULT_SILENCE_HOURS,
                   help=f"Silence threshold (default {DEFAULT_SILENCE_HOURS}h)")
    p.add_argument("--target-asp", default=DEFAULT_TARGET_ASP,
                   help=f"Aspiration to file Investigate on "
                        f"(default {DEFAULT_TARGET_ASP})")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    latest_dt, latest_id = _latest_goal_completion()
    now = datetime.now()
    if latest_dt is None:
        print("[inactivity] no goal completions found anywhere — fresh world?")
        return 0

    silent_hours = (now - latest_dt).total_seconds() / 3600.0
    print(f"[inactivity] latest completion: {latest_id} at "
          f"{latest_dt.isoformat(timespec='seconds')} "
          f"({silent_hours:.2f}h ago)")

    if silent_hours < args.silence_hours:
        return 0

    # Self-dedup: don't file if a recent inactivity Investigate is open
    existing = _existing_inactivity_investigate(args.target_asp)
    if existing:
        print(f"[inactivity] dedup hit — {existing} already pending on "
              f"{args.target_asp}; skipping")
        return 0

    ok, msg = _file_investigate(args.target_asp, silent_hours,
                                  latest_id, args.dry_run)
    if ok:
        print(f"[inactivity] filed Investigate on {args.target_asp}: {msg}")
        return 0
    print(f"[inactivity] WARN: filing failed: {msg}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
