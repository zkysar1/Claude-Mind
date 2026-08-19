#!/usr/bin/env python3
# domain-leak-exempt: framework learning-KPI infra — reads generic utilization counters.
"""Retrieval-utility report — the learning KPI flip (Phase 1d).

Part of the evaluative substrate; eval_harness.py is the keystone + in-code index of all seven.

WHY THIS EXISTS
---------------
The framework measures learning largely by ENCODING VOLUME ("a session with commits but no
encodings has FAILED"). The research pass argued the real test of learning is changed future
behaviour, and the honest proxy already lives in the data: every reasoning-bank and guardrail
record carries a `utilization` object (`retrieval_count`, `times_helpful`, `utilization_score`,
...). This module turns those EXISTING counters into a retrieval-hit-utility report, so the
learning gate can weight "was this knowledge later retrieved and useful?" over "how much did we
write?". An entry retrieved many times but never helpful — or never retrieved at all despite
age — is noise being paid for in retrieval budget forever; this surfaces it for curation.

DESIGN
------
Pure, hermetic, domain-free. Reads a list of records (or a JSONL store) that each may carry a
`utilization` dict, and reports store-level + per-entry signals. Records WITHOUT a utilization
object are counted separately (legacy / not-yet-tracked) rather than skewing the stats. No
import-time path resolution; the caller passes records or a path.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import List, Optional

# Sidecar counters (g-358-05). LAZY + guarded on purpose: this module's contract
# above is "pure, hermetic, no import-time path resolution", and it does no
# sys.path manipulation of its own, so a hard top-level import would break
# standalone importability for any caller that has not already put core/scripts
# on the path. Absent sidecar module => embedded-field behaviour, unchanged.
_SIDECAR = None  # None = not yet attempted, False = unavailable, else the module


def _sidecar():
    global _SIDECAR
    if _SIDECAR is None:
        try:
            import _utilization_store as _m  # type: ignore
            _SIDECAR = _m
        except ImportError:
            _SIDECAR = False
    return _SIDECAR or None


def _util(rec: dict, counters: Optional[dict] = None) -> Optional[dict]:
    """Counters for one record: sidecar first, embedded field second (g-358-05).

    The None return is LOAD-BEARING and is why this cannot just be
    `utilization_of(...)`: `n_tracked` counts records that HAVE a utilization
    object and is the denominator of every rate below. `utilization_of` returns
    `{}` for "absent", which would mark every record tracked and silently
    dilute hit_rate / retrieved_rate / mean_utilization_score. So the value
    comes from the shared helper, and the absent-vs-present-but-empty
    distinction is preserved here rather than duplicated from it.
    """
    m = _sidecar()
    if m is not None:
        u = m.utilization_of(rec, counters)
        if u:
            return u
    embedded = rec.get("utilization")
    return embedded if isinstance(embedded, dict) else None


def _num(x) -> float:
    """Coerce a counter value to a usable number. Real JSONL stores carry explicit
    `null`s (and occasionally stray strings); a report tool must degrade those to 0
    rather than crash the whole store's report on one malformed record. bool is
    excluded so True/False can't masquerade as 1/0 counters."""
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        return 0
    return x


def report(records: List[dict], high_exposure_min: int = 5,
           counters: Optional[dict] = None) -> dict:
    """Compute retrieval-utility statistics over a list of store records.

    Returns:
      n_total / n_tracked       : record counts (tracked = has a utilization obj)
      hit_rate                  : fraction of TRACKED entries with times_helpful > 0
      retrieved_rate            : fraction of TRACKED entries ever retrieved
      mean_utilization_score    : mean of utilization_score over tracked entries
      zero_hit_high_exposure    : [ids] retrieved >= high_exposure_min times but
                                  never helpful — the clearest "noise" candidates
      never_retrieved           : [ids] tracked but retrieval_count == 0 (dead weight)
    `high_exposure_min` is the retrieval count above which a still-unhelpful entry
    is considered confidently noise (vs. simply not-yet-encountered).
    """
    tracked = [(r, u) for r, u in ((r, _util(r, counters)) for r in records)
               if u is not None]
    n_total, n_tracked = len(records), len(tracked)
    if n_tracked == 0:
        return {"n_total": n_total, "n_tracked": 0, "hit_rate": None,
                "retrieved_rate": None, "mean_utilization_score": None,
                "zero_hit_high_exposure": [], "never_retrieved": [],
                "note": "no records carry a utilization object"}

    def _id(r):
        return r.get("id", "<no-id>")

    helpful = [r for r, u in tracked if _num(u.get("times_helpful")) > 0]
    retrieved = [r for r, u in tracked if _num(u.get("retrieval_count")) > 0]
    zero_hit_high_exposure = sorted(
        _id(r) for r, u in tracked
        if _num(u.get("retrieval_count")) >= high_exposure_min
        and _num(u.get("times_helpful")) == 0)
    never_retrieved = sorted(
        _id(r) for r, u in tracked if _num(u.get("retrieval_count")) == 0)
    scores = [float(_num(u.get("utilization_score"))) for _, u in tracked]
    return {
        "n_total": n_total,
        "n_tracked": n_tracked,
        "hit_rate": round(len(helpful) / n_tracked, 4),
        "retrieved_rate": round(len(retrieved) / n_tracked, 4),
        "mean_utilization_score": round(sum(scores) / n_tracked, 4),
        "zero_hit_high_exposure": zero_hit_high_exposure,
        "never_retrieved": never_retrieved,
    }


def load_records(path) -> List[dict]:
    """Load a JSONL store (one record per line; blanks/`#` comments skipped)."""
    out: List[dict] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(json.loads(line))
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Retrieval-utility report over a reasoning-bank/guardrails JSONL store.")
    ap.add_argument("--store", required=True, help="path to a JSONL store")
    ap.add_argument("--high-exposure-min", type=int, default=5)
    args = ap.parse_args(argv)
    # Sidecar counters live BESIDE the store, and the store kind is its stem —
    # so a fixture store in a tmp dir resolves to that dir's sidecar (or to no
    # sidecar at all, which load_counters returns as {}). Deliberately NOT
    # WORLD_DIR: --store takes an arbitrary path, and pinning the live world
    # here would report live counters against fixture records.
    counters = None
    m = _sidecar()
    if m is not None:
        store_path = Path(args.store)
        try:
            counters = m.load_counters(store_path.stem, store_path.parent)
        except Exception:
            counters = None
    print(json.dumps(report(load_records(args.store),
                            high_exposure_min=args.high_exposure_min,
                            counters=counters), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
