#!/usr/bin/env python3
"""mirror_health — detect a silent own-cloud mirror wedge ().

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
  pull-failing — not wedged, but the last full PULL (owncloud_sync
            pull_sweep, ~every 10min) failed on >=1 file: peer writes to those
            files are not reaching this box (g-115-11323 — 14 deep tree nodes
            failed every pull for five days while this probe said healthy).
            Read from RUNTIME_DIR/owncloud-pull-errors.json; ignored when that
            is older than DEFAULT_PULL_MAX_AGE_MIN. The `pull_errors` key names
            the paths on every verdict, wedged included.

Exit codes: 0 healthy, 1 wedged or pull-failing, 2 unknown. Advisory/display-first — the
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
# The pull runs every OWNCLOUD_PULL_EVERY_N sweeps (default 5 x 2min = 10min).
# 18x that, so a slowed cadence still reports, while a file left by a pull that
# was since switched off stops being read as current.
DEFAULT_PULL_MAX_AGE_MIN = 180.0

_EXIT = {"healthy": 0, "wedged": 1, "pull-failing": 1, "unknown": 2}


def _state_dir() -> Path:
    """Mirror owncloud_sync._runtime_dir (RUNTIME_DIR-aware)."""
    rd = os.environ.get("RUNTIME_DIR")
    return Path(rd) if rd else (
        Path(__file__).resolve().parents[2] / "mind_api" / "state")


def streaks_path() -> Path:
    """Mirror owncloud_sync._conflict_streaks_path."""
    return _state_dir() / "owncloud-conflict-streaks.json"


def pull_errors_path() -> Path:
    """Mirror owncloud_sync._pull_errors_path."""
    return _state_dir() / "owncloud-pull-errors.json"


def _pull_fields(pull, pull_age_min, pull_max_age_min) -> dict:
    """The pull half of every verdict. `pull` is the parsed pull-errors file or
    None. A file older than pull_max_age_min contributes nothing: the pull that
    wrote it is no longer running, and old failures are not current ones."""
    fresh = (isinstance(pull, dict) and pull_age_min is not None
             and pull_age_min <= pull_max_age_min)
    count = pull.get("errors", 0) if fresh else 0
    paths = pull.get("error_paths", []) if fresh else []
    return {"pull_error_count": count if isinstance(count, int) else 0,
            "pull_errors": paths if isinstance(paths, list) else [],
            "pull_age_min": (round(pull_age_min, 1)
                             if pull_age_min is not None else None)}


def classify(streaks, age_min, threshold=DEFAULT_THRESHOLD,
             max_age_min=DEFAULT_MAX_AGE_MIN, *, pull=None, pull_age_min=None,
             pull_max_age_min=DEFAULT_PULL_MAX_AGE_MIN) -> dict:
    """Pure classification. streaks: dict|None (None = file absent/unreadable);
    age_min: float|None minutes since the file was last rewritten. pull /
    pull_age_min: the same pair for the pull-errors file."""
    pf = _pull_fields(pull, pull_age_min, pull_max_age_min)
    if streaks is None or age_min is None:
        return {"verdict": "unknown", "reason": "streaks file absent/unreadable",
                "wedged_count": 0, "files": {}, "age_min": age_min, **pf}
    if age_min > max_age_min:
        return {"verdict": "unknown",
                "reason": f"streaks file {age_min:.0f}min old (> {max_age_min:.0f}min)"
                          " — sweep not running; no live signal",
                "wedged_count": 0, "files": {}, "age_min": round(age_min, 1), **pf}
    wedged = {k: v for k, v in streaks.items()
              if isinstance(v, int) and v >= threshold}
    if wedged:
        return {"verdict": "wedged",
                "reason": f"{len(wedged)} file(s) both-diverged for >= {threshold}"
                          " consecutive sweeps — mirror refresh frozen, reads stale",
                "wedged_count": len(wedged), "files": wedged,
                "age_min": round(age_min, 1), **pf}
    if pf["pull_error_count"] > 0:
        return {"verdict": "pull-failing",
                "reason": f"{pf['pull_error_count']} file(s) failed on the last pull"
                          f" ({pf['pull_age_min']:.0f}min ago) — peer writes to"
                          " them are not reaching this box",
                "wedged_count": 0, "files": {}, "age_min": round(age_min, 1), **pf}
    sub = {k: v for k, v in streaks.items() if isinstance(v, int)}
    # Say which it is: a clean pull, or no pull to judge (guard-963).
    pull_fresh = (isinstance(pull, dict) and pull_age_min is not None
                  and pull_age_min <= pull_max_age_min)
    return {"verdict": "healthy",
            "reason": "no persistent both-diverged conflicts"
                      + (f" ({len(sub)} sub-threshold transient(s))" if sub else "")
                      + ("; last pull clean" if pull_fresh else
                         f"; pull unmeasured (no pull record in {pull_max_age_min:.0f}min)"),
            "wedged_count": 0, "files": {}, "age_min": round(age_min, 1), **pf}


def _read_json(p: Path):
    """(parsed dict or None, age in minutes or None). Never raises."""
    try:
        st = p.stat()
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    if not isinstance(raw, dict):
        return None, None
    return raw, (time.time() - st.st_mtime) / 60.0


def probe(threshold=DEFAULT_THRESHOLD, max_age_min=DEFAULT_MAX_AGE_MIN) -> dict:
    """Read the live streaks and pull-errors artifacts and classify. Never raises."""
    if os.environ.get("STORAGE_BACKEND", "own-cloud") != "own-cloud":
        return {"verdict": "unknown", "reason": "not an own-cloud box (probe n/a)",
                "wedged_count": 0, "files": {}, "age_min": None,
                **_pull_fields(None, None, DEFAULT_PULL_MAX_AGE_MIN)}
    streaks, age_min = _read_json(streaks_path())
    pull, pull_age_min = _read_json(pull_errors_path())
    return classify(streaks, age_min, threshold, max_age_min,
                    pull=pull, pull_age_min=pull_age_min)


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
        if v.get("pull_error_count"):
            if v["verdict"] != "pull-failing":
                print(f"  and {v['pull_error_count']} file(s) failed on the"
                      " last pull:")
            for e in v.get("pull_errors") or []:
                if isinstance(e, dict):
                    print(f"  pull {e.get('phase')}  {e.get('path')}: "
                          f"{e.get('exc')}: {e.get('msg')}")
    return _EXIT.get(v["verdict"], 2)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
