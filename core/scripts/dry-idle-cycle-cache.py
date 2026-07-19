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
from datetime import datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))
from _paths import AGENT_DIR  # noqa: E402

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
# Floor on a short-circuit sleep. Below this the re-entry cost outweighs the
# savings, so MISS and let the full cycle handle imminent timer work rather than
# emit a sub-minute bg sleep.
MIN_SHORTCIRCUIT_S = 60

# Wake signal files (all under <agent>/session/) that imply executable work MAY
# now exist. Superset of quiescence's blocker-only set: in the DRY state,
# partner board activity and claim releases are exactly the signals that can
# create claimable work (interruptible-sleep.sh keeps them at exit-2 under
# DRY_SLEEP=1 -- they are NOT demoted the way they are under QUIESCENCE_SLEEP).
DRY_WAKE_SIGNAL_FILES = (
    "blocker-cleared", "pq-resolved", "email-received",
    "board-activity", "goal-claim-released",
)

# Default defer TTL when a deferred goal carries no explicit deferred_until
# (mirrors capability-gate / quiescence-gate defer_reason_timeout_hours=120).
_DEFAULT_DEFER_TIMEOUT_H = 120
# Default abstention TTL (aspirations-select Phase 2.55 abstention_timeout_hours).
_ABSTENTION_TIMEOUT_H = 72
# Default recurring interval when a recurring goal names neither interval_hours
# nor remind_days (mirrors aspirations-select's `OR 24` fallback).
_DEFAULT_RECURRING_INTERVAL_H = 24


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


# --- time helpers ------------------------------------------------------------

def _parse_iso(val):
    """Tolerant ISO parse -> naive datetime, or None. Strips a trailing Z."""
    if not val:
        return None
    s = str(val).strip().strip('"')
    if not s or s == "null":
        return None
    try:
        return datetime.fromisoformat(s.rstrip("Z"))
    except (ValueError, TypeError):
        return None


def _add_hours(dt, hours):
    try:
        return dt + timedelta(hours=float(hours))
    except (TypeError, ValueError, OverflowError):
        return None


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


def _goal_wake_time(goal, now):
    """The soonest time THIS goal could become executable, or None if it carries
    no timer. Covers the four timer-gated 'becomes executable' events that have
    no wake-signal file: defer timeout, recurring interval, blocker expiry,
    abstention expiry. Purely defensive .get()s -- an unparseable field yields
    None for that lane, never a crash."""
    candidates = []
    status = str(goal.get("status") or "").lower()
    recurring = bool(goal.get("recurring"))

    # Deferred (an explicit deferred_until wins; else set_at + timeout hours).
    if goal.get("defer_reason") and status not in ("completed", "skipped", "expired"):
        du = _parse_iso(goal.get("deferred_until"))
        if du is not None:
            candidates.append(du)
        else:
            set_at = _parse_iso(goal.get("defer_reason_set_at"))
            if set_at is not None:
                timeout_h = goal.get("defer_reason_timeout") or _DEFAULT_DEFER_TIMEOUT_H
                w = _add_hours(set_at, timeout_h)
                if w is not None:
                    candidates.append(w)

    # Recurring interval: next-due = lastAchievedAt + interval. A never-run
    # recurring goal (no lastAchievedAt) carries no computable timer -- skip it
    # (the cap backstops); if it were already executable the selector would not
    # have returned dry.
    if recurring:
        last = _parse_iso(goal.get("lastAchievedAt"))
        if last is not None:
            interval_h = goal.get("interval_hours")
            if not interval_h:
                remind_days = goal.get("remind_days")
                interval_h = (remind_days * 24) if remind_days else _DEFAULT_RECURRING_INTERVAL_H
            w = _add_hours(last, interval_h)
            if w is not None:
                candidates.append(w)

    # Blocked with a typed blocker_ref expiry.
    br = goal.get("blocker_ref")
    if isinstance(br, dict):
        exp = _parse_iso(br.get("expires_at"))
        if exp is not None:
            candidates.append(exp)

    # Abstention expiry (abstained_at + 72h). Long horizon; the cap dominates,
    # but including it keeps the timer set faithful to the selector's gates.
    ab = _parse_iso(goal.get("abstained_at"))
    if ab is not None:
        w = _add_hours(ab, _ABSTENTION_TIMEOUT_H)
        if w is not None:
            candidates.append(w)

    return min(candidates) if candidates else None


def _iter_goals(asps):
    for asp in asps:
        if isinstance(asp, dict):
            for g in asp.get("goals", []) or []:
                if isinstance(g, dict):
                    yield g


def _scan_queue(now):
    """Single load of world + agent aspirations -> (goal_count, earliest_wake_at).

    goal_count is the total-goals new-work detector (mirrors
    quiescence-gate._total_goal_count). earliest_wake_at is the soonest timer
    across the queue (ISO string) or None. Raises on load failure so cmd_check
    fails open to a MISS; write_baseline_cache catches so a scan failure at
    write time simply leaves no cache (next check MISSes)."""
    import importlib
    qg = importlib.import_module("quiescence-gate")
    from _paths import WORLD_DIR

    asps = []
    asps.extend(qg._load_aspirations_from(
        AGENT_DIR / "aspirations.jsonl" if AGENT_DIR else None))
    asps.extend(qg._load_aspirations_from(
        WORLD_DIR / "aspirations.jsonl" if WORLD_DIR else None))

    goal_count = 0
    earliest = None
    for g in _iter_goals(asps):
        goal_count += 1
        w = _goal_wake_time(g, now)
        if w is not None and (earliest is None or w < earliest):
            earliest = w
    return goal_count, (earliest.isoformat(timespec="seconds") if earliest else None)


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
    wake = _parse_iso(current_earliest_wake_at)
    if wake is not None and (now + timedelta(seconds=MIN_SHORTCIRCUIT_S)) >= wake:
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

    # HIT -- increment the consecutive-short-circuit counter and re-sleep.
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
