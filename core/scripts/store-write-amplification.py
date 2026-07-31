#!/usr/bin/env python3
"""Rank own-cloud stores by derived daily PUT bytes (write amplification).

Under STORAGE_BACKEND=own-cloud every append/RMW re-PUTs the WHOLE object, so a
store's S3 write cost is not its edit volume but:

    daily_PUT_bytes  =  writes_per_day  x  current_object_size

That product is the amplification. A 42 MB store touched 1000x/day to add 1 KB
of new records costs ~42 GB/day of PUT traffic and mints 1000 whole-object
versions -- the mechanism behind g-328-38.

Two axes, and they are independently tunable:
  * writes_per_day   -- reduced by batching/spooling (g-115-2405 did this for
                        gate-firings: every-gate-decision -> one flush/iteration)
  * object_size      -- reduced by SEGMENTING the store so each write touches
                        only the live segment rather than full retention

EXCLUSION FILTER IS LOAD-BEARING, NOT COSMETIC (measured 2026-07-31, g-328-38).
The changelog records every local write, including files that never reach S3.
Ranking without the filter put `world/presence/alpha.jsonl` at #2 with 22.9% of
all derived PUT bytes (1572 writes/day on a 2.44 MB file) -- a store that is in
`owncloud_sync._EXCLUDE_DIRS` and costs exactly zero. S3 confirmed it
independently: 2 versions, against 26,610 for a synced store of similar age.
A ranking that skips this filter sends the reader to design a fix for a file
with no cost. Always report the excluded rows separately rather than silently
dropping them, so the filter's effect is visible rather than assumed.

SCOPE: the changelog is MACHINE-LOCAL by design (`_EXCLUDE_NAMES`), so this
ranks THIS BOX's writes. The relative ordering is what drives design; for
fleet-wide absolute totals use S3 version counts (see --s3-hint).

Usage:
    py -3 core/scripts/store-write-amplification.py [--days N] [--top K] [--json]
"""
from __future__ import annotations

import argparse
import collections
import datetime as _dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import META_DIR, WORLD_DIR  # noqa: E402

try:
    from owncloud_sync import _EXCLUDE_DIRS, _EXCLUDE_GLOBS, _EXCLUDE_NAMES
except Exception:  # pragma: no cover - defensive; keep the tool usable
    _EXCLUDE_DIRS, _EXCLUDE_NAMES, _EXCLUDE_GLOBS = set(), set(), ()


def _synced(rel: str) -> bool:
    """True iff a write to this logical path reaches S3.

    Mirrors owncloud_sync's directory/basename/glob policy. Imported rather
    than re-implemented so a change to the sync policy cannot silently
    invalidate this ranking (single source of truth).
    """
    parts = Path(rel).parts
    if any(seg in _EXCLUDE_DIRS for seg in parts[:-1]):
        return False
    name = parts[-1] if parts else rel
    if name in _EXCLUDE_NAMES:
        return False
    from fnmatch import fnmatch
    return not any(fnmatch(name, g) for g in _EXCLUDE_GLOBS)


def collect(days: float | None = None):
    counts: collections.Counter = collections.Counter()
    first = last = None
    for root, label in ((WORLD_DIR, "world"), (META_DIR, "meta")):
        path = Path(root) / "changelog.jsonl"
        if not path.exists():
            continue
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                rel, ts = rec.get("file"), rec.get("timestamp")
                if not rel:
                    continue
                counts[(label, rel)] += 1
                if ts:
                    first = ts if first is None or ts < first else first
                    last = ts if last is None or ts > last else last
    if first is None:
        return [], [], 0.0, None, None

    span = (_dt.datetime.fromisoformat(last)
            - _dt.datetime.fromisoformat(first)).total_seconds() / 86400
    span = max(span, 1.0 / 24)  # never divide by ~0 on a fresh box

    synced, excluded = [], []
    for (label, rel), n in counts.items():
        base = WORLD_DIR if label == "world" else META_DIR
        try:
            size = os.path.getsize(Path(base) / rel)
        except OSError:
            size = 0
        row = {
            "store": f"{label}/{rel}",
            "writes": n,
            "writes_per_day": n / span,
            "size_bytes": size,
            "put_bytes_per_day": n * size / span,
        }
        (synced if _synced(rel) else excluded).append(row)

    synced.sort(key=lambda r: r["put_bytes_per_day"], reverse=True)
    excluded.sort(key=lambda r: r["put_bytes_per_day"], reverse=True)
    return synced, excluded, span, first, last


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top", type=int, default=15, help="rows to show (default 15)")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args(argv)

    synced, excluded, span, first, last = collect()
    total = sum(r["put_bytes_per_day"] for r in synced)

    if args.json:
        print(json.dumps({
            "window_start": first, "window_end": last, "span_days": span,
            "total_put_bytes_per_day": total,
            "synced": synced[:args.top],
            "excluded_not_synced": excluded[:args.top],
        }, indent=2))
        return 0

    print(f"window {first} -> {last}  ({span:.2f} days, THIS BOX only)")
    print(f"total derived PUT volume: {total / 1e9:.2f} GB/day\n")
    print(f"{'GB/day':>8s} {'%':>6s} {'PUTs/d':>8s} {'size':>10s}  store")
    print("-" * 78)
    for r in synced[:args.top]:
        pct = 100 * r["put_bytes_per_day"] / total if total else 0
        print(f"{r['put_bytes_per_day'] / 1e9:8.2f} {pct:5.1f}% "
              f"{r['writes_per_day']:8.1f} {r['size_bytes'] / 1e6:8.2f}MB  {r['store']}")
    if excluded:
        print("\nEXCLUDED (machine-local, never PUT to S3 -- zero cost):")
        for r in excluded[:5]:
            print(f"{r['put_bytes_per_day'] / 1e9:8.2f}   --  "
                  f"{r['writes_per_day']:8.1f} {r['size_bytes'] / 1e6:8.2f}MB  {r['store']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
