#!/usr/bin/env python3
"""mirror_health — detect a silent own-cloud mirror wedge (9).

A both-diverged conflict freezes a file's mirror refresh (owncloud_backend
_overwrite_decision -> no_clobber): the sweep skips the file every pass and
consumers silently read stale data with no surfaced signal. Observed cost:
~21h of days-stale world reads across 30 files on this box (2026-07-16..18,
repaired by g-115-2548) — the same mirror-lie class liveness-check.sh guards
against on the read side (rb-3150 / guard-980).

The sync layer ALREADY maintains the live wedge state: every real (non-dry)
sweep rewrites RUNTIME_DIR/owncloud-conflict-streaks.json to exactly the
CURRENT conflict set ({rel_path: consecutive_sweep_count}) — a path that
stops conflicting drops out on the next sweep (owncloud_sync.py
_update_conflict_streaks). This probe CLASSIFIES that artifact instead of
grepping spawn.log (which is unbounded, rotation-fragile, and historical):

  healthy — streaks file fresh and zero entries at/over --threshold
  wedged  — >=1 entry with streak >= threshold (default 3, matching
            owncloud_sync._CONFLICT_STREAK_THRESHOLD)
  unknown — streaks file absent OR older than --max-age-min (default 30;
            sweeps rewrite it ~every 2min, so a 30min-old file means the
            sweep is not running — absence of signal, not health). A box
            not on STORAGE_BACKEND=own-cloud is also "unknown" (probe n/a).

Exit codes: 0 healthy, 1 wedged, 2 unknown. Advisory/display-first — the
REPAIR is the g-115-2548 protocol (/reconcile-owncloud-conflicts); this
probe only makes the condition visible. Consumers: mirror-health.sh (CLI),
/prime Phase 2 display line, agent-watchdog MirrorWedgeProbe (files a
deduped Investigate goal after N consecutive wedged ticks).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

DEFAULT_THRESHOLD = 3       # align with owncloud_sync._CONFLICT_STREAK_THRESHOLD
DEFAULT_MAX_AGE_MIN = 30.0  # sweeps run ~2min; 30min stale = sweep not running

_EXIT = {"healthy": 0, "wedged": 1, "unknown": 2}


def streaks_path() -> Path:
    """Mirror owncloud_sync._conflict_streaks_path (RUNTIME_DIR-aware)."""
    rd = os.environ.get("RUNTIME_DIR")
    base = Path(rd) if rd else (
        Path(__file__).resolve().parents[2] / "mind_api" / "state")
    return base / "owncloud-conflict-streaks.json"


def classify(streaks, age_min, threshold=DEFAULT_THRESHOLD,
             max_age_min=DEFAULT_MAX_AGE_MIN) -> dict:
    """Pure classification. streaks: dict|None (None = file absent/unreadable);
    age_min: float|None minutes since the file was last rewritten."""
    if streaks is None or age_min is None:
        return {"verdict": "unknown", "reason": "streaks file absent/unreadable",
                "wedged_count": 0, "files": {}, "age_min": age_min}
    if age_min > max_age_min:
        return {"verdict": "unknown",
                "reason": f"streaks file {age_min:.0f}min old (> {max_age_min:.0f}min)"
                          " — sweep not running; no live signal",
                "wedged_count": 0, "files": {}, "age_min": round(age_min, 1)}
    wedged = {k: v for k, v in streaks.items()
              if isinstance(v, int) and v >= threshold}
    if wedged:
        return {"verdict": "wedged",
                "reason": f"{len(wedged)} file(s) both-diverged for >= {threshold}"
                          " consecutive sweeps — mirror refresh frozen, reads stale",
                "wedged_count": len(wedged), "files": wedged,
                "age_min": round(age_min, 1)}
    sub = {k: v for k, v in streaks.items() if isinstance(v, int)}
    return {"verdict": "healthy",
            "reason": "no persistent both-diverged conflicts"
                      + (f" ({len(sub)} sub-threshold transient(s))" if sub else ""),
            "wedged_count": 0, "files": {}, "age_min": round(age_min, 1)}


def probe(threshold=DEFAULT_THRESHOLD, max_age_min=DEFAULT_MAX_AGE_MIN) -> dict:
    """Read the live streaks artifact and classify. Never raises."""
    if os.environ.get("STORAGE_BACKEND", "own-cloud") != "own-cloud":
        return {"verdict": "unknown", "reason": "not an own-cloud box (probe n/a)",
                "wedged_count": 0, "files": {}, "age_min": None}
    p = streaks_path()
    try:
        st = p.stat()
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raw = None
    except (OSError, ValueError):
        return classify(None, None, threshold, max_age_min)
    age_min = (time.time() - st.st_mtime) / 60.0
    return classify(raw, age_min, threshold, max_age_min)


def main(argv) -> int:
    threshold, max_age, as_json = DEFAULT_THRESHOLD, DEFAULT_MAX_AGE_MIN, False
    it = iter(argv)
    for a in it:
        if a == "--json":
            as_json = True
        elif a == "--threshold":
            threshold = int(next(it, DEFAULT_THRESHOLD))
        elif a == "--max-age-min":
            max_age = float(next(it, DEFAULT_MAX_AGE_MIN))
        elif a in ("-h", "--help"):
            print(__doc__)
            return 0
    v = probe(threshold, max_age)
    if as_json:
        print(json.dumps(v, ensure_ascii=False))
    else:
        print(f"mirror-health: {v['verdict']} — {v['reason']}")
        for f, n in sorted((v.get("files") or {}).items()):
            print(f"  {n:>3} sweeps  {f}")
    return _EXIT.get(v["verdict"], 2)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
