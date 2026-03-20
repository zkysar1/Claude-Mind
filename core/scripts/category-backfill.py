#!/usr/bin/env python3
"""One-time migration: assign categories to all goals missing them.

Reads aspirations.jsonl, runs category-suggest for each goal without a category,
and updates the goals with the best-matching tree node key.

Usage:
    category-backfill.sh [--dry-run]
"""

import argparse
import json
import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Import suggest() from category-suggest.py (hyphenated name needs importlib)
SCRIPT_DIR = Path(__file__).resolve().parent
import importlib.util
_spec = importlib.util.spec_from_file_location("category_suggest", SCRIPT_DIR / "category-suggest.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
suggest = _mod.suggest

from _paths import MIND_DIR

ASP_PATH = MIND_DIR / "aspirations.jsonl"


def read_jsonl(path):
    p = Path(path)
    if not p.exists():
        return []
    items = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s:
                items.append(json.loads(s))
    return items


def write_jsonl(path, items):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(p) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=True) + "\n")
    os.replace(str(tmp), str(p))


def main():
    parser = argparse.ArgumentParser(description="Backfill categories on goals")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print assignments without writing")
    args = parser.parse_args()

    aspirations = read_jsonl(ASP_PATH)
    if not aspirations:
        print("No aspirations found.")
        return

    categorized = 0
    uncategorized = 0
    skipped = 0

    for asp in aspirations:
        for goal in asp.get("goals", []):
            existing = goal.get("category")
            if existing and existing != "uncategorized":
                skipped += 1
                continue

            title = goal.get("title", "")
            desc = goal.get("description", "")
            text = f"{title}. {desc}"

            matches = suggest(text, top_n=1)
            if matches and matches[0]["score"] > 0:
                cat = matches[0]["key"]
                categorized += 1
            else:
                cat = "uncategorized"
                uncategorized += 1

            if args.dry_run:
                score = matches[0]["score"] if matches else 0
                print(f"  {goal.get('id', '?'):12s} → {cat:40s} (score={score:.1f}) | {title[:60]}")
            else:
                goal["category"] = cat

    if not args.dry_run:
        write_jsonl(ASP_PATH, aspirations)
        print(f"Written to {ASP_PATH}")

    print(f"\nSummary: {categorized} categorized, {uncategorized} uncategorized, {skipped} already had category")


if __name__ == "__main__":
    main()
