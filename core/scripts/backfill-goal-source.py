#!/usr/bin/env python3
"""One-shot backfill: set goal.goal_source on every goal in
world/aspirations.jsonl and <agent>/aspirations.jsonl, inferring the value
from origin_signal prefix per the mapping in
core/config/conventions/goal-schemas.md "Auto-derivation from origin_signal".

Part of g-305-01 (US-01) — adds the framework-vs-domain attribution field.

Safety:
  - Dry-run by default (prints summary of what would change). Pass --apply
    to write.
  - Skips goals that already have a goal_source (idempotent, safe to re-run).
  - Goals whose origin_signal does not map cleanly are left untouched
    (goal_source stays null) — they contribute 0 to drift denominators.
  - Writes via the standard _fileops path so changelog + history snapshots
    fire. NO direct file writes.

Usage:
  python backfill-goal-source.py                   # dry run both sources
  python backfill-goal-source.py --apply           # write both
  python backfill-goal-source.py --apply --source world
  python backfill-goal-source.py --apply --source agent
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import _paths  # type: ignore  # noqa: E402
from _goal_source import infer as _infer_goal_source_from_signal  # noqa: E402
from _fileops import locked_write_jsonl  # noqa: E402


def _read_jsonl(path: Path):
    items = []
    if not path.is_file():
        return items
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"[backfill] WARN: invalid JSON line in {path}: {e}",
                      file=sys.stderr)
    return items


def _backfill_source(source: str, path: Path, apply: bool, counters: dict):
    items = _read_jsonl(path)
    if not items:
        print(f"[backfill:{source}] no aspirations at {path}")
        counters[source] = {"changed": 0, "already": 0, "unmapped": 0,
                            "distribution": {}, "unmapped_signals": {}}
        return

    changed = 0
    already = 0
    unmapped = 0
    unmapped_signals: dict[str, int] = {}
    distribution: dict[str, int] = {}

    for asp in items:
        for g in asp.get("goals") or []:
            existing = g.get("goal_source")
            if existing:
                already += 1
                distribution[existing] = distribution.get(existing, 0) + 1
                continue
            origin = g.get("origin_signal")
            inferred = _infer_goal_source_from_signal(origin)
            if inferred is None:
                unmapped += 1
                key = origin or "<missing>"
                # Bucket detailed origin signals to the prefix to keep the
                # unmapped report scannable (otherwise every unique
                # decomposition:g-NNN-NN becomes a separate row).
                if ":" in key:
                    key = key.split(":", 1)[0] + ":*"
                unmapped_signals[key] = unmapped_signals.get(key, 0) + 1
                continue
            g["goal_source"] = inferred
            changed += 1
            distribution[inferred] = distribution.get(inferred, 0) + 1

    counters[source] = {
        "changed": changed,
        "already": already,
        "unmapped": unmapped,
        "distribution": distribution,
        "unmapped_signals": unmapped_signals,
    }

    if apply and changed > 0:
        locked_write_jsonl(path, items)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true",
                   help="write changes (default: dry-run)")
    p.add_argument("--source", choices=["world", "agent", "both"], default="both",
                   help="which aspirations file to backfill")
    args = p.parse_args()

    world_path = Path(_paths.WORLD_DIR) / "aspirations.jsonl"
    agent_path = Path(_paths.AGENT_DIR) / "aspirations.jsonl"

    counters: dict = {}
    if args.source in ("world", "both"):
        _backfill_source("world", world_path, args.apply, counters)
    if args.source in ("agent", "both"):
        _backfill_source("agent", agent_path, args.apply, counters)

    print(json.dumps({
        "apply": args.apply,
        "world_path": str(world_path),
        "agent_path": str(agent_path),
        "results": counters,
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
