#!/usr/bin/env python3
"""spark-fire-dedup.py — consumer-side dedup for the pending_phase_6_spark
sentinel (g-115-1203).

Problem: recurring-close.sh writes the pending_phase_6_spark sentinel
unconditionally at end-of-script AND emits a stdout outcome-aware imperative.
On the FAST path (the LLM sees the stdout inline), the LLM fires
Skill(aspirations-spark) in-turn (fire #1), then re-enters the loop where
aspirations/SKILL.md Phase -0.5c.2 reads the still-set sentinel and fires
Skill(aspirations-spark) AGAIN for the same goal (fire #2). The double-fire is
idempotent but wasteful: it pollutes the spark times_asked metrics (diluting
yield_rate) and writes duplicate evolution-log entries. The sentinel is only
NEEDED on the bg-timeout path (the LLM never sees the stdout); on the fast path
it is redundant.

Fix (shape (a) from the originating finding): track a wm slot
`spark_fired_session = {goal_id: fired_at_iso}`. Both fire paths funnel through
aspirations-spark, which `record`s its firing. Phase -0.5c.2 `check`s before
firing: if the goal was sparked within the dedup window (default 5 min), it
SKIPS the re-fire and just clears the sentinel.

PURE stdin->stdout — this script does NO wm I/O of its own. The caller (a
single Bash-tool pipeline) does the wm read/write via the canonical wm-read.sh
/ wm-set.sh wrappers and pipes the value THROUGH this script:

    record:  wm-read.sh <slot> --json | spark-fire-dedup.py record <gid> | wm-set.sh <slot>
    check:   wm-read.sh <slot> --json | spark-fire-dedup.py check  <gid>

This is deliberate: a python process must NOT spawn `bash wm-*.sh` via
subprocess on this platform — that hangs (the Windows python->bash subprocess
hang, rb-225 / rb-247; the wm slot getter/setter are daemon wrappers invoked
through bash). Keeping the script pure (read map from stdin, emit result to
stdout) sidesteps the hang entirely while leaving the wm I/O on the bash side
where it already works. The pure-function core (recently_fired /
prune_and_record) is unit-tested in test_pending_phase_6_spark_sentinel.py.

Fail-open at every layer: empty/`null`/malformed stdin, a missing entry, or an
unparseable timestamp all default to "fire" (never suppress a legitimate spark
— a redundant fire is wasteful but a suppressed one loses learning).

Cross-refs:
  - g-115-1203 (this Apply), g-115-1174 (sentinel origin), rb-428 (lifecycle)
  - rb-225 / rb-247 (Windows python->bash subprocess hang — why this is pure)
  - .claude/skills/aspirations/SKILL.md Phase -0.5c.2 (check consumer)
  - .claude/skills/aspirations-spark/SKILL.md (record producer)
  - core/scripts/recurring-close.sh (sentinel + stdout imperative writer)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta

SLOT = "spark_fired_session"  # documented here for callers; this script is slot-agnostic
# Consumption-based dedup (4 / rb-1674): the PRIMARY path compares the
# recorded fire time against the sentinel's set_at (passed via --sentinel-set-at).
# DEFAULT_WINDOW_MIN is now only the FALLBACK for callers that cannot supply a
# set_at; it is aligned to the sentinel's 60-min TTL (Phase -0.5c.2 expires
# sentinels older than 60min) rather than the original 5 min, which was shorter
# than the bg-timeout wall-clock between record (aspirations-spark Step 0.5) and
# check (next loop entry) and false-fired an already-fired deep spark.
DEFAULT_WINDOW_MIN = 60
# Entries must SURVIVE from record-time to the consuming check, which on the
# bg-timeout path can be up to the sentinel's full ~60-min lifetime away. The
# prior 10-min prune dropped the fired entry before a same-close check could
# find it (on any iteration where an intervening spark fired and pruned). 90 =
# 60-min TTL + slack so the set_at comparison always has the entry to compare.
DEFAULT_PRUNE_MIN = 90
#  /  / rb-2615: a PROACTIVELY-fired Phase-6 spark records its
# fire BEFORE the bg recurring-close finishes writing the sentinel's set_at
# (observed: fire 01:55:14 vs set_at 02:01:25 -> ~6m11s early, because the LLM
# fires Skill(aspirations-spark) off the stdout imperative while recurring-close
# is still backgrounded). The strict `fired_at >= set_at` skip test therefore
# read a proactive fire as a PRIOR-close fire and false-fired the spark next
# iteration. MAX_BG_CLOSE_DURATION_MIN widens the consumption window's LOWER
# bound by this much so a proactive fire still counts as THIS close's
# consumption. 10 covers the observed 6m11s with margin and stays well under any
# recurring interval (hours), so it never suppresses a genuine SECOND deep-close
# of the same goal.
MAX_BG_CLOSE_DURATION_MIN = 10


def _parse_dt(value):
    """Parse an ISO timestamp; return None on any failure (guard-420 pattern:
    a bad timestamp must never raise — it degrades to 'not recently fired')."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def recently_fired(fired_map, goal_id, now, window_minutes=DEFAULT_WINDOW_MIN):
    """True iff goal_id was recorded within window_minutes before `now`.

    Fail-safe: a non-dict map, a missing entry, an unparseable timestamp, or a
    future/negative delta all return False (fire), never True — the dedup must
    never suppress a spark on bad data.
    """
    if not isinstance(fired_map, dict):
        return False
    ts = _parse_dt(fired_map.get(goal_id))
    if ts is None:
        return False
    delta = (now - ts).total_seconds()
    # Negative delta (clock skew / future stamp) is NOT "recent" — fire.
    return 0 <= delta < window_minutes * 60


def fired_in_consumption_window(fired_map, goal_id, set_at,
                                lookback_minutes=MAX_BG_CLOSE_DURATION_MIN,
                                lookahead_minutes=DEFAULT_WINDOW_MIN):
    """Consumption-based dedup (4 / rb-1674; window widened by
    g-306-80 / rb-2615). True iff goal_id was recorded within the consumption
    window [set_at - lookback_minutes, set_at + lookahead_minutes] bracketing
    the sentinel's creation time `set_at`.

    A spark fired IN RESPONSE to this close lands inside the window on either
    side of set_at:
      - NORMAL path: the close writes set_at, THEN the LLM fires → fired_at >=
        set_at (the original g-115-1404 case).
      - PROACTIVE path (g-306-80): the LLM fires off recurring-close.sh's stdout
        imperative WHILE the bg close is still writing set_at → fired_at can be
        up to ~MAX_BG_CLOSE_DURATION_MIN BEFORE set_at. The strict `>= set_at`
        test mis-read this as a prior close and false-fired (incident g-115-399:
        fire 01:55:14 vs set_at 02:01:25). The lower bound now catches it.
    A spark from a genuine PREVIOUS close lands well before the lower bound
    (recurring intervals are hours) or has no entry at all → fire. The upper
    bound (set_at + TTL) keeps the match scoped to the sentinel's lifetime; a
    far-future fire (clock skew / a stale entry beyond the sentinel's life) does
    not count as this close's consumption.

    Fail-safe: a non-dict map, a None set_at, a missing entry, or an unparseable
    fired timestamp all return False (fire), never True — the dedup must never
    suppress a spark on bad data (a redundant fire is the safe failure
    direction; a suppressed legitimate spark loses learning).
    """
    if not isinstance(fired_map, dict) or set_at is None:
        return False
    ts = _parse_dt(fired_map.get(goal_id))
    if ts is None:
        return False
    lo = set_at - timedelta(minutes=lookback_minutes)
    hi = set_at + timedelta(minutes=lookahead_minutes)
    return lo <= ts <= hi


def prune_and_record(fired_map, goal_id, now, prune_minutes=DEFAULT_PRUNE_MIN):
    """Return a NEW map with goal_id=now added and entries older than
    prune_minutes dropped. Bounds the slot's size across a session and discards
    unparseable entries. Does not mutate the input. (wm-set REPLACES the slot,
    so a pruned entry is genuinely gone — verified g-115-1203.)"""
    out = {}
    if isinstance(fired_map, dict):
        for gid, ts_str in fired_map.items():
            ts = _parse_dt(ts_str)
            if ts is None:
                continue  # drop unparseable / stale-shaped entries
            if 0 <= (now - ts).total_seconds() < prune_minutes * 60:
                out[gid] = ts_str
    out[goal_id] = now.isoformat(timespec="seconds")
    return out


def _now():
    return datetime.now()


def _read_stdin_map():
    """Read the current spark_fired_session map from stdin (JSON). Treats
    empty / 'null' / malformed input as {} — fail-open (the wm slot getter
    prints 'null' for an unset slot)."""
    try:
        raw = sys.stdin.read().strip()
        if not raw or raw == "null":
            return {}
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except Exception:
        return {}


def cmd_record(args):
    fired = _read_stdin_map()
    updated = prune_and_record(fired, args.goal_id, _now())
    # Emit the FULL new map to stdout for the caller to pipe into wm-set.sh
    # (which REPLACES the slot — so the emitted map is authoritative).
    sys.stdout.write(json.dumps(updated))
    return 0


def cmd_check(args):
    fired = _read_stdin_map()
    set_at = _parse_dt(getattr(args, "sentinel_set_at", None))
    if set_at is not None:
        # PRIMARY path (4): consumption-based — is the recorded fire
        # inside THIS sentinel's consumption window? The window brackets set_at
        # on both sides (): [set_at - MAX_BG_CLOSE_DURATION_MIN,
        # set_at + TTL], so a proactive fire that lands just BEFORE set_at still
        # counts as this-close consumption. Robust against the bg-timeout
        # wall-clock that broke the 5-min window (rb-1674) AND the proactive
        # before-set_at fire that broke the strict >= test (rb-2615).
        deduped = fired_in_consumption_window(fired, args.goal_id, set_at)
    else:
        # FALLBACK (no sentinel set_at supplied): time-window heuristic.
        deduped = recently_fired(fired, args.goal_id, _now(), args.window_min)
    if deduped:
        print("skip")  # already fired for this close — caller dedups (clears sentinel, no re-fire)
        return 1
    print("fire")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="pending_phase_6_spark fire dedup (g-115-1203)")
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("record", help="stdin=current map JSON; stdout=new map with goal_id stamped")
    pr.add_argument("goal_id")
    pr.set_defaults(func=cmd_record)
    pc = sub.add_parser("check", help="stdin=current map JSON; stdout=fire|skip (exit 1=skip, 0=fire)")
    pc.add_argument("goal_id")
    pc.add_argument("--window-min", type=int, default=DEFAULT_WINDOW_MIN,
                    help="fallback time-window minutes, used only when --sentinel-set-at is absent")
    pc.add_argument("--sentinel-set-at", default=None,
                    help="ISO timestamp of the sentinel's creation (set_at). When supplied, dedup is "
                         "consumption-based (skip iff fired_at >= set_at) instead of the --window-min "
                         "heuristic — robust against bg-timeout wall-clock (g-115-1404 / rb-1674).")
    pc.set_defaults(func=cmd_check)
    args = p.parse_args(argv)
    try:
        return args.func(args)
    except Exception as e:
        # Fail-open: never block/suppress a spark on a dedup bug. A redundant
        # fire is the safe failure direction; a suppressed legitimate spark
        # loses learning. For `record`, still emit a valid map so the wm-set
        # pipe never receives garbage.
        print(f"[spark-fire-dedup] WARN: {e}", file=sys.stderr)
        cmd = getattr(args, "cmd", None)
        if cmd == "check":
            print("fire")
        elif cmd == "record":
            try:
                sys.stdout.write(json.dumps({args.goal_id: _now().isoformat(timespec="seconds")}))
            except Exception:
                sys.stdout.write("{}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
