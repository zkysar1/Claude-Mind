#!/usr/bin/env python3
"""S4b cross-pollination detector for /aspirations-strategic-scan.

Surfaces the reasoning-bank entry in a category that is RETRIEVED often but
credited helpful almost never -- it keeps surfacing and keeps not paying off
HERE, which is the transfer-learning signal S4b is supposed to emit.

Selection is by CATEGORY (independent of both age and utilization) and the
score is utilization_score_v2 (already opportunity-normalized). The predicate
this replaced sampled by RECENCY and scored times_helpful, a variable recency
suppresses by construction -- it admitted 100.0% of every window size tried.
Measurement and the rejected alternatives: g-115-3853, written up in
core/config/rationale/s4b-cross-pollination-recalibration.md.

Reads the store through the daemon (_rt), never the raw JSONL file, and never
via a bash subprocess (guard-580; sibling rationale in silent-gap-audit.py).

Output (JSON on stdout):
  scanned    -- entries the category returned, BEFORE any filter. Printed
                deliberately as the positive control for the two counts below:
                `candidates: 0` next to `scanned: 0` is a different statement
                from `candidates: 0` next to `scanned: 430` (guard-2298).
  mature     -- of those, entries with retrieval_count >= --maturity, i.e. the
                population that HAD an opportunity to be credited.
  candidates -- of the mature, entries scoring <= --ceiling.
  top        -- the strongest candidate, or null.

Exit 0 with `top: null` is a real negative, not a failure.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _rt  # noqa: E402  canonical Python -> daemon client

# MATURITY is the opportunity floor: it is what makes a low score mean "not
# paying off" rather than "too new to have been used". Without it, adding any
# score threshold to this detector inverts always-fires into never-fires --
# ZERO of the newest 400 entries have retrieval_count >= 3 (guard-1665).
DEFAULT_MATURITY = 3
# CEIL sits well inside the live spread: 29.0% of active entries are at or
# below it, 71.0% above, so both branches are reachable on the real corpus.
DEFAULT_CEILING = 0.05


def _util(rec):
    return rec.get("utilization") or {}


def scan(records, maturity=DEFAULT_MATURITY, ceiling=DEFAULT_CEILING):
    """Pure core: records -> the S4b verdict dict. Unit-testable without a daemon."""
    mature = [r for r in records
              if (_util(r).get("retrieval_count") or 0) >= maturity]
    cands = sorted(
        ((_util(r).get("utilization_score_v2") or 0.0),
         -(_util(r).get("retrieval_count") or 0),
         r.get("id"))
        for r in mature
        if (_util(r).get("utilization_score_v2") or 0) <= ceiling
    )
    top = None
    if cands:
        v2, neg_retr, rec_id = cands[0]
        top = {"id": rec_id, "v2": v2, "retrieval_count": -neg_retr}
    return {
        "scanned": len(records),
        "mature": len(mature),
        "candidates": len(cands),
        "top": top,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="S4b cross-pollination detector (reasoning bank).")
    ap.add_argument("--category", required=True,
                    help="reasoning-bank category to scan (use one OTHER than max_cat)")
    ap.add_argument("--maturity", type=int, default=DEFAULT_MATURITY,
                    help="minimum retrieval_count to be scoreable (default: %(default)s)")
    ap.add_argument("--ceiling", type=float, default=DEFAULT_CEILING,
                    help="max utilization_score_v2 to qualify (default: %(default)s)")
    args = ap.parse_args(argv)

    raw = _rt.rt_call("GET", "/v1/rb/read",
                      query={"category": args.category, "summary": "1"})
    records = _rt.tolerant_decode_list("scan-cross-pollination", raw)

    print(json.dumps(scan(records, args.maturity, args.ceiling)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
