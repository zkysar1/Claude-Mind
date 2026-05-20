#!/usr/bin/env python3
"""One-shot backfill: set goal.work_class on every goal in
world/aspirations.jsonl and <agent>/aspirations.jsonl using the
category-to-work_class mapping from core/config/work-class-mapping.yaml.

Part of g-244-07 remediation. Before this runs, goal-selector criterion 7e
contributes 0.0 to every candidate because `goal.get("work_class")` returns
None universally. After this runs, goals with a mapped category carry a
work_class tag and the class-balance bonus produces real signal.

Safety:
  - Dry-run by default (prints summary of what would change). Pass --apply
    to write.
  - Skips goals that already have a work_class (idempotent, safe to re-run).
  - Writes via the standard _fileops path so changelog + history snapshots
    fire. NO direct file writes.
  - Uses per-file locking via _fileops acquire_lock/release_lock.

Usage:
  python backfill-work-class.py                 # dry run both sources
  python backfill-work-class.py --apply         # write both
  python backfill-work-class.py --apply --source world
  python backfill-work-class.py --apply --source agent
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import _paths  # type: ignore  # noqa: E402
from _work_class import resolve as resolve_work_class  # noqa: E402
from _fileops import locked_write_jsonl  # noqa: E402


def _read_jsonl(path: Path):
    """Read a JSONL file. Each line is one aspiration record."""
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
    """Walk the aspirations file, resolve work_class for each goal, write
    back atomically under lock when --apply is set."""
    items = _read_jsonl(path)
    if not items:
        print(f"[backfill:{source}] no aspirations at {path}")
        return

    changed = 0
    already = 0
    unmapped = 0
    unmapped_cats: dict[str, int] = {}
    by_class: dict[str, int] = {}

    for asp in items:
        for g in asp.get("goals") or []:
            existing = g.get("work_class")
            if existing:
                already += 1
                by_class[existing] = by_class.get(existing, 0) + 1
                continue
            category = g.get("category") or ""
            wc = resolve_work_class(category)
            if wc == "unclassified":
                unmapped += 1
                key = category or "<missing>"
                unmapped_cats[key] = unmapped_cats.get(key, 0) + 1
            g["work_class"] = wc
            changed += 1
            by_class[wc] = by_class.get(wc, 0) + 1

    counters[source] = {
        "changed": changed,
        "already": already,
        "unmapped_resolved_to_default": unmapped,
        "unmapped_categories": unmapped_cats,
        "distribution": by_class,
    }

    if apply and changed > 0:
        # locked_write_jsonl handles lock + history snapshot + atomic rename
        # + changelog append internally. Re-reading under its lock is not
        # possible from here, so we pass the in-memory items we built above.
        # Race risk: if another process appends a new aspiration between our
        # read and this write, it gets clobbered. Mitigated by running the
        # backfill once as a one-shot while no other writers are active.
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
