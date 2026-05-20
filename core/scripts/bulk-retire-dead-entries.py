#!/usr/bin/env python3
"""bulk-retire-dead-entries.py — one-shot curation tool for the
reasoning-bank and guardrail stores.

Identifies and (with --apply) retires entries that have accumulated
significant retrieval volume but produced zero helpful signal:

    status == "active"
    AND retrieval_count >= --min-retrievals (default 100)
    AND times_helpful + times_cited == 0
    AND age >= --min-age-days (default 30)
    AND retirement is not already pending (no retirement_date set)

Default mode is DRY-RUN — prints candidate counts, samples, and what would
change. Pass --apply to actually flip status to "retired" with a recorded
`retirement_date` and `retirement_reason`. Retirement is reversible: a
follow-up `update-field <id> status active` revives the entry.

Background:
  Knowledge-system audit (2026-05-09) found ~75% of RB and ~69% of
  guardrails had utilization_score=0 — never produced a helpful event after
  retrieval. P0 #1 (retrieve.py result-filter, commit 81eb405) stops new
  retrievals from inflating retrieval_count on category-mismatched entries.
  P0 #3 (this script) retires the historical pile-up.

  Criteria intentionally avoid `utilization_score` and `times_skipped`
  because they are inflated by pre-2026-04-23 bulk-load behavior
  (skipped > retrieved by 2-3x is documented in the audit). `times_helpful`
  and `times_cited` only increment when an entry is genuinely used; they
  cannot be inflated the same way.

Usage:
  py -3 core/scripts/bulk-retire-dead-entries.py                    # dry-run
  py -3 core/scripts/bulk-retire-dead-entries.py --apply            # mutate
  py -3 core/scripts/bulk-retire-dead-entries.py --store rb         # only RB
  py -3 core/scripts/bulk-retire-dead-entries.py --min-retrievals 50 \
      --apply --reason "audit-2026-05-09-bulk-retire"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _paths import WORLD_DIR  # noqa: E402

RB_PATH = WORLD_DIR / "reasoning-bank.jsonl"
GUARD_PATH = WORLD_DIR / "guardrails.jsonl"


def _parse_created(rec):
    """Parse the `created` field (date or ISO datetime). Returns date or None."""
    val = rec.get("created", "") or ""
    if not val:
        return None
    try:
        if "T" in val:
            return datetime.strptime(val[:10], "%Y-%m-%d").date()
        return datetime.strptime(val, "%Y-%m-%d").date()
    except Exception:
        return None


def _is_candidate(rec, min_retrievals, min_age_days, today):
    if rec.get("status") != "active":
        return False
    # Already pending retirement (or in some other lifecycle state)
    if rec.get("retirement_date"):
        return False
    util = rec.get("utilization") or {}
    rc = util.get("retrieval_count", 0) or 0
    helpful = util.get("times_helpful", 0) or 0
    cited = util.get("times_cited", 0) or 0
    if rc < min_retrievals:
        return False
    if (helpful + cited) > 0:
        return False
    created = _parse_created(rec)
    if created is None:
        return False
    age = (today - created).days
    if age < min_age_days:
        return False
    return True


def _read_jsonl(path):
    if not Path(path).exists():
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _summarize(records, candidates, store_label, out=sys.stdout):
    """Print counts + a sample of candidates so a human can sanity-check."""
    total_active = sum(1 for r in records if r.get("status") == "active")
    print(f"\n=== {store_label} ===", file=out)
    print(f"  Total records:         {len(records)}", file=out)
    print(f"  Active:                {total_active}", file=out)
    print(f"  Retirement candidates: {len(candidates)} "
          f"({len(candidates) / max(total_active, 1) * 100:.1f}% of active)",
          file=out)
    if not candidates:
        return
    # Show top 5 by retrieval_count
    samples = sorted(
        candidates,
        key=lambda r: (r.get("utilization") or {}).get("retrieval_count", 0),
        reverse=True,
    )[:5]
    print(f"  Top 5 candidates by retrieval_count:", file=out)
    for r in samples:
        util = r.get("utilization") or {}
        title_or_rule = r.get("title") or r.get("rule", "")
        title_or_rule = (title_or_rule or "")[:60]
        print(f"    {r.get('id', '?'):10s}  "
              f"rc={util.get('retrieval_count', 0):4d}  "
              f"helpful={util.get('times_helpful', 0)}  "
              f"cited={util.get('times_cited', 0)}  "
              f"created={r.get('created', '?')}  "
              f"cat={r.get('category', '?')[:25]:25s}  "
              f"{title_or_rule}", file=out)


def _apply_retirement(path, candidate_ids, reason, today_iso, store_label):
    """Flip status to retired for matching IDs. Uses locked_modify_jsonl so
    concurrent writers cannot clobber the change."""
    if not candidate_ids:
        print(f"  {store_label}: no candidates, skipping write.", file=sys.stderr)
        return 0

    from _fileops import locked_modify_jsonl
    candidate_set = set(candidate_ids)
    counter = {"changed": 0}

    def _modifier(records):
        for rec in records:
            if rec.get("id") in candidate_set and rec.get("status") == "active":
                rec["status"] = "retired"
                rec["retirement_date"] = today_iso
                rec["retirement_reason"] = reason
                counter["changed"] += 1
        return records

    locked_modify_jsonl(path, _modifier)
    print(f"  {store_label}: retired {counter['changed']} entries.",
          file=sys.stderr)
    return counter["changed"]


def main():
    p = argparse.ArgumentParser(
        description="Bulk-retire dead reasoning-bank / guardrail entries.",
        epilog="Default is DRY-RUN. Pass --apply to mutate.",
    )
    p.add_argument("--store", choices=["rb", "guard", "both"], default="both",
                   help="Which store(s) to process (default: both)")
    p.add_argument("--min-retrievals", type=int, default=100,
                   help="Only retire entries with retrieval_count >= this "
                        "(default 100). Higher values are more conservative.")
    p.add_argument("--min-age-days", type=int, default=30,
                   help="Only retire entries created at least this many days "
                        "ago (default 30). Protects new entries from "
                        "premature retirement before they accumulate signal.")
    p.add_argument("--reason", type=str,
                   default="audit-2026-05-09-bulk-retire-dead-entries",
                   help="String written to retirement_reason field.")
    p.add_argument("--apply", action="store_true",
                   help="Actually mutate the JSONL files. Without --apply "
                        "this is a dry-run.")
    args = p.parse_args()

    today = date.today()
    today_iso = today.isoformat()

    print(f"Bulk retire criteria:", file=sys.stderr)
    print(f"  status == active", file=sys.stderr)
    print(f"  AND retirement_date is unset", file=sys.stderr)
    print(f"  AND retrieval_count >= {args.min_retrievals}", file=sys.stderr)
    print(f"  AND times_helpful + times_cited == 0", file=sys.stderr)
    print(f"  AND age >= {args.min_age_days} days "
          f"(created on or before {today - timedelta(days=args.min_age_days)})",
          file=sys.stderr)

    rb_changed = 0
    guard_changed = 0

    if args.store in ("rb", "both"):
        rb = _read_jsonl(RB_PATH)
        rb_candidates = [r for r in rb
                         if _is_candidate(r, args.min_retrievals,
                                          args.min_age_days, today)]
        _summarize(rb, rb_candidates, "Reasoning Bank")
        if args.apply and rb_candidates:
            rb_changed = _apply_retirement(
                RB_PATH, [r["id"] for r in rb_candidates],
                args.reason, today_iso, "rb",
            )

    if args.store in ("guard", "both"):
        guard = _read_jsonl(GUARD_PATH)
        guard_candidates = [r for r in guard
                            if _is_candidate(r, args.min_retrievals,
                                             args.min_age_days, today)]
        _summarize(guard, guard_candidates, "Guardrails")
        if args.apply and guard_candidates:
            guard_changed = _apply_retirement(
                GUARD_PATH, [r["id"] for r in guard_candidates],
                args.reason, today_iso, "guard",
            )

    print(file=sys.stderr)
    if args.apply:
        print(f"Applied: rb={rb_changed} guard={guard_changed}",
              file=sys.stderr)
    else:
        print("DRY-RUN — no files modified. Pass --apply to retire.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
