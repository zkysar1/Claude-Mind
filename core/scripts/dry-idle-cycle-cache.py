#!/usr/bin/env python3
"""dry-idle-cycle-cache.py -- fast-path short-circuit for consecutive DRY cycles
(g-115-2084-d, Layer 4 of the dry-idle backoff).

When the autonomous loop is in a persistent DRY trough -- zero executable goals
AND quiescence denied/na (the mutually-exclusive sibling of the quiescence
all-blocked state, see _dry_idle.py) -- each loop re-entry otherwise re-runs the
full precheck / select / create-aspiration / re-select chain only to re-derive
"still dry, sleep the backoff curve". This script is the FAST PATH the
orchestrator (aspirations/SKILL.md Phase -0.5e.0b) calls right AFTER the
quiescence-cycle short-circuit and BEFORE idle-tick.sh: it re-validates the last
dry-idle-tick decision (still dry, goal count unchanged, no timer elapsed, no
pending wake signal, under the consecutive-short-circuit cap) WITHOUT loading the
heavy skill chain, and on a HIT emits a sleep directive so the loop re-sleeps the
same dry-curve backoff directly.

Contract (mirrors quiescence-cycle-cache.py / idle-tick.sh):
  Exit 0 + empty stdout : cache MISS -- proceed to idle-tick / full skill.
  Exit 0 + stdout text  : "=== DRY-IDLE CACHE HIT ===" directive -- the caller
                          MUST NOT load the skill; emit the one sleep tool call.

Why this is the DRY twin of quiescence-cycle-cache.py -- and where it diverges:
  - WRITER: dry-idle-tick.py is the single writer (via write_baseline_cache on a
    dry=true tick / delete_cache on a non-dry tick), exactly as quiescence-gate.py
    is the single writer of quiescence-last-cycle.json on the approved path. This
    script only increments cycle_count on a HIT.
  - SLEEP MODEL DIVERGES: the quiescence cache caps every short-circuit at 600s
    (SLEEP_CAP_S) to preserve a health-check cadence, because a quiescence sleep
    is "longer than B7" and 600s already satisfies that. The DRY cache instead
    honors the FULL exponential curve (up to max_seconds=7200s) because long
    sleeps under a stable-empty queue ARE the point of the dry-idle backoff
    (Layers 1-3). Sleeping the curve is only sound if we never oversleep the
    moment a goal becomes executable, so soundness comes from capping the sleep
    at earliest_wake_at -- the soonest defer-timeout / recurring-interval /
    blocker-expiry across the queue (the DRY analog of quiescence's per-ref
    expires_at check). interruptible-sleep.sh already wakes early on the
    SIGNAL-driven wake events (board-activity, blocker-cleared, ...); the timer
    horizon covers the wake events that have no signal file.

Safety (why a short-circuit cannot strand executable work or mask drift):
  - dry_active gate: only fires while loop_state.signals.dry_idle.streak > 0 (a
    dry period is genuinely in progress). Cheap WM read; avoids the queue scan
    during productive work.
  - staleness gate: the cached decision must be recent relative to the sleep it
    scheduled (anchor = last_hit_at or written_at; stale if older than
    sleep_seconds + DRY_STALE_MARGIN_S). A productive interlude leaves a stale
    cache that MISSes rather than short-circuiting on hours-old state.
  - goal-count guard: MORE total goals than at write time -> new work may be
    executable -> MISS.
  - timer horizon: if the soonest defer/recurring/blocker timer is elapsed OR
    within MIN_SHORTCIRCUIT_S, a goal may now be executable -> MISS (and the
    emitted sleep is capped at that horizon so we never oversleep it).
  - baseline-timer horizon (g-115-3033): the freshly-rescanned horizon above can
    silently shift LATER when the soonest goal DROPS OUT of the scan between write
    and check -- notably a recurring goal whose next-due ELAPSES (future-only guard
    g-115-3018 drops the now-past due-time). So the STORED baseline earliest_wake_at
    (the soonest becomes-executable moment recorded when the sleep started) is ALSO
    checked: once it has arrived, a goal may now be executable -> MISS. Catches the
    elapsed-recurring false HIT the fresh-scan check alone missed.
  - pending wake signal (blocker-cleared / pq-resolved / email-received /
    board-activity / goal-claim-released) -> MISS.
  - cap (default 3): after N consecutive short-circuits, force one full cycle so
    slow drift the cheap checks miss is still caught.

Every path fails open to a MISS (empty stdout, exit 0): a bug here can only fall
back to the normal full cycle, never wrongly short-circuit. The queue scan reads
the LOCAL aspirations mirror (same source as quiescence-gate._total_goal_count);
on own-cloud a stale mirror can at worst delay new-work detection by one cap run,
which the cap + the interruptible-sleep wake signals bound.
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))
from _paths import AGENT_DIR  # noqa: E402
from _idle_cache_common import (  # noqa: E402
    wake_timer_elapsed,
    authoritative_earliest_wake_at,
)
from _wake_timers import (  # noqa: E402
    _parse_iso, _add_hours, _goal_wake_time, _iter_goals,
    scan_queue as _scan_queue,
    DEFAULT_DEFER_TIMEOUT_H as _DEFAULT_DEFER_TIMEOUT_H,
    ABSTENTION_TIMEOUT_H as _ABSTENTION_TIMEOUT_H,
    DEFAULT_RECURRING_INTERVAL_H as _DEFAULT_RECURRING_INTERVAL_H,
    MIN_FLOOR_S as MIN_SHORTCIRCUIT_S,
)

# MUST equal the name dry-idle-tick.py writes via write_baseline_cache(). A drift
# only causes a fail-open MISS here (the safe direction), never a wrong
# short-circuit.
CACHE_NAME = "dry-idle-last-cycle.json"
DEFAULT_CAP = 3
# A dry decision older than its scheduled sleep + this margin is stale: a
# productive interlude ran and the streak has not yet been reset by the next
# dry-idle-tick. 15min of slack absorbs a slow harness re-entry on a long curve
# sleep while still catching a genuine interlude.
DRY_STALE_MARGIN_S = 900
# Defer to idle-tick.sh's checkpoint logic while a fresh dry sleep is still
# mid-flight (blocked_sleep_until has more than this many seconds remaining).
DEFER_REMAINING_S = 60
# MIN_SHORTCIRCUIT_S imported from _wake_timers (as MIN_FLOOR_S alias) above.

# Wake signal files (all under <agent>/session/) that imply executable work MAY
# now exist. Superset of quiescence's blocker-only set: in the DRY state,
# partner board activity and claim releases are exactly the signals that can
# create claimable work (interruptible-sleep.sh keeps them at exit-2 under
# DRY_SLEEP=1 -- they are NOT demoted the way they are under QUIESCENCE_SLEEP).
DRY_WAKE_SIGNAL_FILES = (
    "blocker-cleared", "pq-resolved", "email-received",
    "board-activity", "goal-claim-released",
)

# _DEFAULT_DEFER_TIMEOUT_H, _ABSTENTION_TIMEOUT_H, _DEFAULT_RECURRING_INTERVAL_H
# imported from _wake_timers above.


# --- cache file I/O (mirrors quiescence-cycle-cache._read_cache/_write_cache) --

def _cache_path():
    if AGENT_DIR is None:
        return None
    return AGENT_DIR / "session" / CACHE_NAME


def _read_cache():
    p = _cache_path()
    if p is None or not p.exists():
        return None
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _write_cache(cache):
    p = _cache_path()
    if p is None:
        return
    tmp = p.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(cache, ensure_ascii=False, default=str),
                       encoding="utf-8")
        tmp.replace(p)
    except OSError as e:
        print(f"[dry-idle-cycle-cache] write failed: {e}", file=sys.stderr)


def delete_cache():
    """Invalidate the cache. Called by dry-idle-tick.py on a NON-dry tick so a
    stale baseline can never license a short-circuit after the dry period ends.
    Fail-soft."""
    p = _cache_path()
    if p is None:
        return
    try:
        p.unlink(missing_ok=True)
    except OSError as e:
        print(f"[dry-idle-cycle-cache] delete failed: {e}", file=sys.stderr)


# _parse_iso, _add_hours imported from _wake_timers above.

# --- dry-state signal + timers (the queue scan) ------------------------------

def _dry_signal():
    """Return loop_state.signals.dry_idle (dict), or {} on any error.

    Reuses quiescence-gate._wm_read_loop_state -- the canonical, corruption-
    tolerant, daemon-routed loop_state reader (no second implementation)."""
    try:
        import importlib
        qg = importlib.import_module("quiescence-gate")
        loop_state = qg._wm_read_loop_state()
    except Exception:
        return {}
    sig = ((loop_state.get("signals") or {}).get("dry_idle")) or {}
    return sig if isinstance(sig, dict) else {}


def _blocked_sleep_remaining(now):
    """Seconds remaining on blocked_sleep_until, or None if unset/unparseable."""
    try:
        import _rt
        raw = _rt.wm_read(slot="blocked_sleep_until")
    except Exception:
        return None
    wake = _parse_iso(raw)
    if wake is None:
        return None
    return (wake - now).total_seconds()


def _pending_wake_signal():
    if AGENT_DIR is None:
        return False
    sess = AGENT_DIR / "session"
    return any((sess / name).exists() for name in DRY_WAKE_SIGNAL_FILES)


# _goal_wake_time, _iter_goals, _scan_queue imported from _wake_timers above.


# --- WRITER (called by dry-idle-tick.py) -------------------------------------

def write_baseline_cache(sleep_seconds, streak, now):
    """Establish the dry short-circuit baseline. dry-idle-tick.py calls this on a
    dry=true tick (the DRY analog of quiescence-gate._write_cycle_cache on the
    approved path). cycle_count=0 resets the consecutive-short-circuit counter on
    every fresh dry decision. Fail-soft: a scan/write error leaves no cache and
    the next fast-path check simply MISSes."""
    try:
        goal_count, earliest_wake_at = _scan_queue(now)
    except Exception as e:
        print(f"[dry-idle-cycle-cache] baseline scan failed: {e}", file=sys.stderr)
        return
    _write_cache({
        "goal_count": goal_count,
        "earliest_wake_at": earliest_wake_at,
        "sleep_seconds": int(sleep_seconds),
        "streak": int(streak),
        "cycle_count": 0,
        "written_at": now.isoformat(timespec="seconds"),
        "last_hit_at": None,
    })


# --- pure decision (mirrors quiescence-cycle-cache.evaluate_cache) -----------

def _is_stale(anchor_iso, sleep_seconds, now):
    """True iff the cached decision is older than its scheduled sleep + margin.
    A missing/unparseable anchor is treated as stale (safe direction)."""
    anchor = _parse_iso(anchor_iso)
    if anchor is None:
        return True
    budget = (int(sleep_seconds) if isinstance(sleep_seconds, int) else 0) + DRY_STALE_MARGIN_S
    return (now - anchor).total_seconds() > budget


def evaluate_cache(cache, dry_active, stale, blocked_remaining,
                   current_goal_count, current_earliest_wake_at,
                   pending_signal, now, cap=DEFAULT_CAP):
    """Pure decision. Returns (decision, reason) where decision is 'hit'|'miss'.

    Ordered cheap-to-expensive (mirrors quiescence-cycle-cache.evaluate_cache) so
    the caller can gate the expensive queue scan behind the cheap MISSes."""
    if cache is None:
        return ("miss", "no-cache")
    if not dry_active:
        return ("miss", "not-in-dry")
    if stale:
        return ("miss", "stale-dry")
    if blocked_remaining is not None and blocked_remaining > DEFER_REMAINING_S:
        return ("miss", "blocked-sleep-active")
    cycle_count = int(cache.get("cycle_count", 0) or 0)
    if cycle_count >= cap:
        return ("miss", f"cap-reached:{cycle_count}>={cap}")
    cached_count = cache.get("goal_count")
    if (isinstance(cached_count, int) and isinstance(current_goal_count, int)
            and current_goal_count > cached_count):
        return ("miss", f"new-work:{current_goal_count}>{cached_count}")
    # Baseline-timer-elapsed + fresh-timer checks (), via the shared
    # _idle_cache_common.wake_timer_elapsed helper (facet-1, ). The FRESH
    # current_earliest_wake_at can silently MISS when the soonest goal drops out of
    # the queue scan between write and check -- the load-bearing case being a
    # recurring goal whose next-due ELAPSES mid-sleep: the future-only guard
    # () drops the now-past due from the fresh rescan (jumps to a LATER
    # goal or None), goal_count is unchanged (the recurring goal was always
    # present), so the new-work guard is silent too -> false HIT that sleeps through
    # the now-due recurring goal (observed 2026-07-24, alpha msg-4248: dry cache
    # reported "queue empty" while goal-selector returned  executable for
    # ~10min). So the STORED baseline earliest_wake_at (the soonest
    # becomes-executable moment recorded when this dry sleep STARTED) is checked
    # FIRST; once it has arrived a goal MAY now be executable regardless of the
    # fresh scan. Shared LOGIC only (): each cache passes its own margin.
    if wake_timer_elapsed(cache.get("earliest_wake_at"), now, MIN_SHORTCIRCUIT_S):
        return ("miss", "baseline-timer-elapsed")
    if wake_timer_elapsed(current_earliest_wake_at, now, MIN_SHORTCIRCUIT_S):
        return ("miss", "timer-imminent-or-elapsed")
    if pending_signal:
        return ("miss", "pending-wake-signal")
    return ("hit", "ok")


def _hit_sleep_seconds(cache, current_earliest_wake_at, now):
    """The sleep the HIT directive emits: the cached dry-curve value, but never
    past the next timer horizon (evaluate_cache has already guaranteed that
    horizon is >= MIN_SHORTCIRCUIT_S away)."""
    sleep_seconds = int(cache.get("sleep_seconds") or 0)
    if sleep_seconds < MIN_SHORTCIRCUIT_S:
        sleep_seconds = MIN_SHORTCIRCUIT_S
    wake = _parse_iso(current_earliest_wake_at)
    if wake is not None:
        secs = int((wake - now).total_seconds())
        if MIN_SHORTCIRCUIT_S <= secs < sleep_seconds:
            sleep_seconds = secs
    return sleep_seconds


def _emit_hit_directive(cache, current_earliest_wake_at, now, cap):
    sleep_seconds = _hit_sleep_seconds(cache, current_earliest_wake_at, now)
    agent = os.environ.get("MIND_AGENT", "") or "unknown"
    short = int(cache.get("cycle_count", 0) or 0)
    streak = cache.get("streak", "?")
    print(
        "=== DRY-IDLE CACHE HIT ===\n"
        f"Consecutive dry cycle (streak {streak}, short-circuit {short}/{cap}).\n"
        "Executable queue still empty, goal count unchanged, no timer elapsed, no\n"
        "pending wake signal -- skipping the precheck/select/create-aspiration\n"
        "reload for this cycle.\n"
        "DO NOT load Skill(aspirations). DO NOT run selection or execution.\n"
        "Emit exactly ONE tool call:\n"
        f"  Bash(\"MIND_AGENT={agent} DRY_SLEEP=1 bash core/scripts/interruptible-sleep.sh {sleep_seconds}\", run_in_background=true)\n"
        "When the harness notifies you of its exit, call Skill('aspirations') with args='loop'.\n"
        f"After {cap} consecutive short-circuits a full cycle is forced for drift detection.\n"
        "================="
    )


# --- check subcommand --------------------------------------------------------

def cmd_check(_args):
    now = datetime.now()
    cap = int(os.environ.get("DRY_IDLE_CACHE_CAP", DEFAULT_CAP))

    cache = _read_cache()
    if cache is None:
        return 0  # cheap miss -- no cache

    # Cheap gate 1: are we genuinely in a dry period? (avoids the queue scan)
    dry_active = int(_dry_signal().get("streak", 0) or 0) > 0
    if not dry_active:
        return 0

    # Cheap gate 1b: staleness -- a productive interlude leaves a stale baseline.
    stale = _is_stale(cache.get("last_hit_at") or cache.get("written_at"),
                      cache.get("sleep_seconds"), now)
    if stale:
        return 0

    # Cheap gate 2: defer to idle-tick while a fresh dry sleep is mid-flight.
    blocked_remaining = _blocked_sleep_remaining(now)
    if blocked_remaining is not None and blocked_remaining > DEFER_REMAINING_S:
        return 0

    # Cheap gate 3: cap reached -- force a full cycle for drift detection.
    if int(cache.get("cycle_count", 0) or 0) >= cap:
        return 0

    # Expensive: single queue scan for the new-work + timer-horizon checks.
    try:
        current_goal_count, current_earliest_wake_at = _scan_queue(now)
    except Exception:
        return 0  # fail-open: queue unreadable -> miss -> normal full cycle

    pending_signal = _pending_wake_signal()
    decision, _reason = evaluate_cache(
        cache, dry_active, stale, blocked_remaining, current_goal_count,
        current_earliest_wake_at, pending_signal, now, cap)
    if decision != "hit":
        return 0

    # facet-2 authoritative recheck (; guard-1139 / ) -- see
    # quiescence-cycle-cache.cmd_check for the full rationale. Every cheap LOCAL
    # gate HIT (would sleep); the local aspirations mirror can lag the store under
    # own-cloud, so verify the earliest wake against the AUTHORITATIVE store before
    # sleeping. R2 latency: this S3 read runs ONLY on the HIT path, never every
    # cycle. Fail-open to MISS on an unreadable store, matching the `_scan_queue`
    # `except -> return 0` full-cycle fail-open above (guard-1139: never sleep on a
    # local-only decision when the authoritative check could not run).
    try:
        auth_wake = authoritative_earliest_wake_at(now)
    except Exception:
        return 0  # authoritative store unreadable -> MISS -> normal full cycle
    if wake_timer_elapsed(auth_wake, now, MIN_SHORTCIRCUIT_S):
        return 0  # store shows an imminent/elapsed wake the local mirror missed

    # HIT confirmed authoritatively -- increment the consecutive-short-circuit
    # counter and re-sleep.
    cache["cycle_count"] = int(cache.get("cycle_count", 0) or 0) + 1
    cache["last_hit_at"] = now.isoformat(timespec="seconds")
    _write_cache(cache)
    _emit_hit_directive(cache, current_earliest_wake_at, now, cap)
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Dry-idle-cycle fast-path short-circuit cache (g-115-2084-d)")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("check", help="Evaluate the cache; emit a dry-sleep directive on a hit.")
    args = parser.parse_args()
    if args.cmd == "check":
        sys.exit(cmd_check(args))
    parser.print_help()
    sys.exit(0)


if __name__ == "__main__":
    main()
