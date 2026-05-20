#!/usr/bin/env python3
"""Strategy application consumer — closes the strategy→execution feedback loop.

Until this script existed, heuristics in meta/*-strategy.yaml were write-only:
the agent generated them during reflection/evolution but no execution code path
ever read them, so `times_applied` stayed at 0 forever (g-001-113 / g-001-117).

Wired in by execute-protocol-digest.md Intelligent Retrieval Step 4b:
given a goal's category and title/description keywords, returns applicable
heuristics and (with --increment) bumps their times_applied counter.

Usage:
  strategy-apply.py --goal-category <cat> --goal-keywords "kw1,kw2,..." \
                    [--phase selection|generation|any] [--increment] [--json]

  strategy-apply.py --migrate     # one-shot: backfill times_applied:0 on
                                  # all heuristics missing the field.

Output (stdout, JSON):
  {"matched":[{"id","description","file","phase","times_applied"}],"count":N}
"""

import argparse
import json
import re
import sys
from pathlib import Path

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
from _gate_log import log as _gate_log
from _fileops import locked_write_yaml


# (filename, heuristics_field, phase_name)
STRATEGY_FILES = [
    ("goal-selection-strategy.yaml", "selection_heuristics", "selection"),
    ("aspiration-generation-strategy.yaml", "generation_heuristics", "generation"),
]


def load(file_tuple):
    path = META_DIR / file_tuple[0]
    if not path.exists():
        return path, None, []
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    heuristics = data.get(file_tuple[1], []) or []
    return path, data, heuristics


def tokenize(text):
    return re.findall(r"[a-z][a-z0-9-]{2,}", (text or "").lower())


def keyword_match(heuristic, keyword_tokens):
    if not keyword_tokens:
        return False
    desc_tokens = set(tokenize(heuristic.get("description", "")))
    return any(kw in desc_tokens for kw in keyword_tokens)


def run_match(phase_filter, keyword_tokens, increment):
    matched = []
    for file_tuple in STRATEGY_FILES:
        if phase_filter != "any" and phase_filter != file_tuple[2]:
            continue
        path, data, heuristics = load(file_tuple)
        if not heuristics:
            continue

        dirty = False
        for h in heuristics:
            if not keyword_match(h, keyword_tokens):
                continue
            if "times_applied" not in h:
                h["times_applied"] = 0
                dirty = True
            if increment:
                h["times_applied"] = int(h.get("times_applied", 0)) + 1
                dirty = True
            matched.append({
                "id": h.get("id"),
                "description": (h.get("description") or "")[:180],
                "file": file_tuple[0],
                "phase": file_tuple[2],
                "times_applied": h.get("times_applied", 0),
            })

        if dirty:
            data[file_tuple[1]] = heuristics
            locked_write_yaml(path, data)

    return matched


def run_migrate():
    migrated = 0
    for file_tuple in STRATEGY_FILES:
        path, data, heuristics = load(file_tuple)
        if not heuristics:
            continue
        dirty = False
        for h in heuristics:
            if "times_applied" not in h:
                h["times_applied"] = 0
                dirty = True
                migrated += 1
        if dirty:
            data[file_tuple[1]] = heuristics
            locked_write_yaml(path, data)
    return migrated


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--goal-category", default="")
    p.add_argument("--goal-keywords", default="",
                   help="Comma-separated tokens to match against heuristic descriptions.")
    p.add_argument("--phase", default="any", choices=["selection", "generation", "any"])
    p.add_argument("--increment", action="store_true",
                   help="Bump times_applied on each matched heuristic.")
    p.add_argument("--migrate", action="store_true",
                   help="Backfill times_applied:0 on all heuristics missing the field.")
    args = p.parse_args()

    if args.migrate:
        n = run_migrate()
        print(json.dumps({"migrated": n}))
        return

    kw_raw = [k.strip() for k in args.goal_keywords.split(",") if k.strip()]
    if args.goal_category:
        kw_raw.append(args.goal_category)
    keyword_tokens = []
    for kw in kw_raw:
        keyword_tokens.extend(tokenize(kw))
    keyword_tokens = list(dict.fromkeys(keyword_tokens))  # dedupe, preserve order

    matched = run_match(args.phase, keyword_tokens, args.increment)
    # gate_id MUST match core/config/gates.yaml id.
    # No block branch — strategy-apply suggests heuristics, never refuses.
    _gate_log("strategy-apply",
              "pass" if matched else "noop",
              caller="strategy-apply.py:main",
              trigger_matched=(matched[0].get("id") if matched else None),
              payload=",".join(keyword_tokens)[:200],
              extra={"match_count": len(matched), "phase": args.phase,
                     "category": args.goal_category or None})
    print(json.dumps({"matched": matched, "count": len(matched),
                      "keyword_tokens": keyword_tokens}, indent=2))


if __name__ == "__main__":
    main()
