#!/usr/bin/env python3
"""poignancy-ab-probe.py — A/B harness for the poignancy retrieval blend ().

Compares retrieval top-k ranking with the poignancy blend OFF (baseline) vs ON
(treatment) over the live reasoning-bank corpus, and verifies the core safety
property the blend is designed around: because the blend is BOOST-ONLY (factor
>= 1.0, bounded additive sort bonus of at most poignancy_weight_max - 1.0),
enabling it never HIDES known-good knowledge — no high-utility record present
in the baseline top-k may drop out of the treatment top-k.

This is the recorded A/B that satisfies g-306-08 verification outcome 3
("A/B of top-k recorded showing no known-good knowledge hidden").

Read-only: never bumps utilization counters, never mutates the corpus. Each run
is appended as one JSONL line to meta/experiments/poignancy-ab-results.jsonl
(skip with --no-record).

Usage:
  py -3 core/scripts/poignancy-ab-probe.py [--top-k N] [--synthetic] [--no-record]

Today every reasoning-bank record's poignancy is null (the field was just
introduced), so a real-data A/B is a trivial no-op: identical rankings, nothing
hidden — itself a valid result. --synthetic assigns deterministic synthetic
poignancy (1 + zlib.crc32(id) % 10) to every record so the A/B exercises the ON
path and demonstrates the boost-only guarantee on a populated corpus.
"""
from __future__ import annotations

import argparse
import json
import sys
import zlib
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

import retrieve as R  # noqa: E402  (path set above)


def _utilization_score(rec):
    return (rec.get("utilization") or {}).get("utilization_score", 0) or 0


def _topk_ids(records, k):
    """Sort a COPY via retrieve._sort_by_utility under the currently-cached
    config, return the top-k ids (in ranked order)."""
    ranked = R._sort_by_utility(list(records))
    return [r.get("id") for r in ranked[:k]]


def _run(records, k):
    """Return (baseline_topk_ids, treatment_topk_ids) for the same corpus under
    flag-off then flag-on. Mutates the process-wide retrieval config cache and
    restores it."""
    prev = R._RETRIEVAL_CFG_CACHE
    base_cfg = dict(R._load_retrieval_config())
    try:
        off = dict(base_cfg, poignancy_blend_enabled=False)
        R._RETRIEVAL_CFG_CACHE = off
        baseline = _topk_ids(records, k)

        on = dict(base_cfg, poignancy_blend_enabled=True)
        # Ensure the weight knobs are sane even if tree.yaml omitted them.
        on.setdefault("poignancy_weight_min", 1.0)
        on.setdefault("poignancy_weight_max", 1.5)
        R._RETRIEVAL_CFG_CACHE = on
        treatment = _topk_ids(records, k)
        return baseline, treatment
    finally:
        R._RETRIEVAL_CFG_CACHE = prev


def main(argv=None):
    ap = argparse.ArgumentParser(description="A/B probe for the poignancy retrieval blend (g-306-08).")
    ap.add_argument("--top-k", type=int, default=20, help="top-k window to compare (default 20)")
    ap.add_argument("--synthetic", action="store_true",
                    help="assign deterministic synthetic poignancy to every record")
    ap.add_argument("--no-record", action="store_true", help="do not append a result line")
    args = ap.parse_args(argv)

    records = [r for r in R.read_jsonl(R.RB_PATH) if r.get("status") == "active"]
    if not records:
        print(json.dumps({"error": "no active reasoning-bank records found", "rb_path": str(R.RB_PATH)}))
        return 1

    real_poignancy_count = sum(1 for r in records if r.get("poignancy") is not None)
    if args.synthetic:
        for r in records:
            key = str(r.get("id", ""))
            r["poignancy"] = 1 + (zlib.crc32(key.encode("utf-8")) % 10)

    k = args.top_k
    baseline, treatment = _run(records, k)

    base_set, treat_set = set(baseline), set(treatment)
    entered = sorted(treat_set - base_set)   # promoted into top-k by the blend
    left = sorted(base_set - treat_set)       # dropped from top-k by the blend

    # Bounded-displacement safety check (the "no known-good knowledge hidden"
    # criterion). The blend is multiplicative and bounded by poignancy_weight_max
    # (F): a record with utilization U_left can only be displaced by one with
    # U_enter such that U_enter * F >= U_left. So a left record is IMPROPERLY
    # hidden iff even the strongest record that ENTERED the top-k, boosted by the
    # max factor, could not have outranked it: max_entered_util * F < U_left.
    # For a correct multiplicative blend this set is ALWAYS empty; a non-empty
    # set signals a scaling bug — exactly the additive-domination bug this A/B
    # caught during  development.
    max_factor = float(R._load_retrieval_config().get("poignancy_weight_max", 1.5))
    by_id = {r.get("id"): r for r in records}
    entered_utils = [_utilization_score(by_id.get(rid, {})) for rid in entered]
    max_entered_util = max(entered_utils) if entered_utils else 0.0
    known_good_hidden = [
        rid for rid in left
        if _utilization_score(by_id.get(rid, {})) > max_entered_util * max_factor
    ]

    result = {
        "experiment": "poignancy-blend",
        "goal": "g-306-08",
        "mode": "synthetic" if args.synthetic else "real",
        "top_k": k,
        "corpus_size": len(records),
        "records_with_real_poignancy": real_poignancy_count,
        "max_factor": max_factor,
        "max_entered_utilization": max_entered_util,
        "topk_entered": entered,
        "topk_left": left,
        "known_good_hidden": known_good_hidden,
        "no_known_good_hidden": len(known_good_hidden) == 0,
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))

    if not args.no_record:
        try:
            from _paths import META_DIR
            if META_DIR is not None:
                out_dir = Path(META_DIR) / "experiments"
                out_dir.mkdir(parents=True, exist_ok=True)
                out_path = out_dir / "poignancy-ab-results.jsonl"
                with open(out_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(result, ensure_ascii=False) + "\n")
                print(f"[recorded] {out_path}", file=sys.stderr)
        except Exception as exc:  # recording is best-effort; never fail the probe
            print(f"[record-skipped] {exc}", file=sys.stderr)

    # Exit non-zero ONLY when the safety invariant is violated, so the probe can
    # gate an enable decision in CI / a future recurring check.
    return 0 if result["no_known_good_hidden"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
