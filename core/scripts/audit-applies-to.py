#!/usr/bin/env python3
"""audit-applies-to.py — categorical audit of reasoning-bank `applies_to`
field. Surfaces "leakers" — entries where the field is missing or
inconsistent with what the category clearly implies.

Background:
  Audit on 2026-05-10 found 299/620 active reasoning-bank entries (48%)
  with no `applies_to` field. That field controls cross-domain surfacing
  via `retrieve.py`: entries with `framework` or `any` are universal
  meta-lessons (visible regardless of agent's current domain), while
  `domain` and `specific` (or absent → treated as `specific`) are
  domain-scoped only. A genuine framework lesson left without
  `applies_to` is a LEAKER in the cross-agent direction — the lesson
  fails to surface universally when it should.

Categorical heuristics (single source of truth — change here, not in
forks):

  CATEGORY contains "framework"  →  applies_to = "framework"
  CATEGORY in DOMAIN_PREFIXES    →  applies_to = "domain"
  CATEGORY in METHODOLOGY_TERMS  →  applies_to = "any"
  else                           →  uncertain (no recommendation)

The framework rule is the highest-confidence auto-fix candidate (the
category literally names itself "framework-*"). Domain and any rules are
lower confidence — recommended but not auto-applied without explicit
flag.

Usage:
  py -3 core/scripts/audit-applies-to.py            # dry-run summary
  py -3 core/scripts/audit-applies-to.py --apply-framework
  py -3 core/scripts/audit-applies-to.py --apply-domain
  py -3 core/scripts/audit-applies-to.py --apply-any
  py -3 core/scripts/audit-applies-to.py --apply-all   # all three
  py -3 core/scripts/audit-applies-to.py --show <bucket>  # list IDs
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _paths import WORLD_DIR  # noqa: E402
from _world_config import load_world_config as _load_world_config  # noqa: E402

RB_PATH = WORLD_DIR / "reasoning-bank.jsonl"

# DOMAIN_PREFIXES moved to world/config/applies-to-rules.yaml on 2026-05-18
# (Phase 2.5 of packaging plan). The legacy hardcoded tuple lived here until
# the overlay seed; a fresh deployment without the overlay file gets an empty
# tuple and every entry falls into "uncertain" instead of being auto-classified
# as "domain" (no false positives). Note: "platform" intentionally NOT a prefix
# in the the framework overlay — "platform-friction" is a framework portability lesson.
def _domain_prefixes():
    cfg = _load_world_config(
        "applies-to-rules",
        default={"domain_prefixes": []},
    )
    return tuple(p for p in (cfg.get("domain_prefixes") or []) if isinstance(p, str))

DOMAIN_PREFIXES = _domain_prefixes()

METHODOLOGY_TERMS = {
    "code", "code-quality", "code-review",
    "hypothesis-accuracy", "hypothesis-calibration",
    "review-process", "review-discipline",
    "agent-methodology",
    "verification-methodology", "investigation-methodology",
    "investigation", "investigation-protocol",
    "coordination",
    "self-improvement",
    "system-behavior",
    "tree-maintenance-patterns",
    "testing",
    "observability",
    "diagnosis", "diagnostics", "diagnostic-reasoning",
    "infrastructure-verification", "infrastructure-monitoring",
    "design-patterns",
    "metric-design", "measurement-design",
    "state-machine-design", "circuit-breaker-design",
    "deployment-discipline",
    "scanner-authoring",
    "agent-config-override-layer",
    "multi-agent", "multi-agent-handoff",
}


def classify(category):
    """Return ('framework' | 'domain' | 'any' | None, confidence_label)."""
    if not category:
        return None, "no-category"
    cat = category.lower()
    if "framework" in cat:
        return "framework", "framework-prefix"
    for prefix in DOMAIN_PREFIXES:
        if cat == prefix or cat.startswith(prefix + "-") or cat.startswith(prefix + "_"):
            return "domain", f"domain-prefix:{prefix}"
    if cat in METHODOLOGY_TERMS:
        return "any", "methodology-term"
    return None, "uncertain"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply-framework", action="store_true",
                    help="Apply framework-* reassignments to None-applies entries.")
    ap.add_argument("--apply-domain", action="store_true",
                    help="Apply domain-prefix reassignments to None-applies entries.")
    ap.add_argument("--apply-any", action="store_true",
                    help="Apply methodology-term reassignments to None-applies entries.")
    ap.add_argument("--apply-all", action="store_true",
                    help="Apply all three. Equivalent to --apply-framework --apply-domain --apply-any.")
    ap.add_argument("--include-retired", action="store_true",
                    help="Also process retired records (default: active only). "
                         "Retired records also need applies_to under the new "
                         "required-field rule; without this they fail validation "
                         "if anyone unretires them.")
    ap.add_argument("--show", choices=["framework", "domain", "any", "uncertain"],
                    help="List IDs and titles of entries classified into this bucket.")
    args = ap.parse_args()

    if args.apply_all:
        args.apply_framework = True
        args.apply_domain = True
        args.apply_any = True

    if not RB_PATH.exists():
        print(f"ERROR: reasoning-bank.jsonl not found at {RB_PATH}", file=sys.stderr)
        return 1

    # First pass: classify every None-applies entry. By default scope to
    # active records (the retrieval surface); --include-retired widens to
    # also catch retired leakers (closes the un-retire footgun).
    valid_statuses = {"active", "retired"} if args.include_retired else {"active"}
    none_entries = []
    with open(RB_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("status") not in valid_statuses:
                continue
            if rec.get("applies_to") is not None:
                continue
            recommendation, why = classify(rec.get("category", ""))
            none_entries.append((rec, recommendation, why))

    bucket_counts = Counter(r for _, r, _ in none_entries)
    bucket_entries = defaultdict(list)
    for rec, r, why in none_entries:
        bucket_entries[r].append((rec, why))

    scope = "active+retired" if args.include_retired else "active"
    print("=" * 78)
    print(f"applies_to leakers audit ({scope}) — "
          f"{len(none_entries)} entries with applies_to=None")
    print("=" * 78)
    print(f"  framework recommended:   {bucket_counts['framework']}")
    print(f"  domain recommended:      {bucket_counts['domain']}")
    print(f"  any recommended:         {bucket_counts['any']}")
    print(f"  uncertain (no auto-rec): {bucket_counts[None]}")

    if args.show:
        bucket = "framework" if args.show == "framework" else \
                 "domain" if args.show == "domain" else \
                 "any" if args.show == "any" else None
        rows = bucket_entries[bucket]
        print(f"\n--- {args.show} bucket ({len(rows)} entries) ---")
        for rec, why in rows:
            cat = rec.get("category", "<no-cat>")
            title = (rec.get("title", "") or "")[:90]
            print(f"  {rec['id']:8s} | {cat:30s} | {why:30s} | {title}")
        return 0

    do_apply = args.apply_framework or args.apply_domain or args.apply_any

    if not do_apply:
        print("\nDRY-RUN — no records modified. Pass --apply-framework / "
              "--apply-domain / --apply-any / --apply-all to mutate.")
        print("Use --show framework / --show domain / --show any / --show uncertain "
              "to inspect bucket contents.")
        return 0

    targets = set()
    if args.apply_framework:
        targets.add("framework")
    if args.apply_domain:
        targets.add("domain")
    if args.apply_any:
        targets.add("any")

    from _fileops import locked_modify_jsonl

    to_update = {rec["id"]: rec_target
                 for rec, rec_target, _ in none_entries
                 if rec_target in targets}
    print(f"\nApplying: {len(to_update)} record(s) → {sorted(targets)}")

    def _modifier(items):
        for i, item in enumerate(items):
            if item.get("id") in to_update:
                item["applies_to"] = to_update[item["id"]]
                items[i] = item
        return items

    locked_modify_jsonl(RB_PATH, _modifier)
    print(f"DONE. Updated {len(to_update)} record(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
