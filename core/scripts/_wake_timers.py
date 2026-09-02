#!/usr/bin/env python3
"""_wake_timers.py -- shared timer-horizon computation for idle/quiescence caches.

Extracted from dry-idle-cycle-cache.py (D1 fix) so both dry-idle-cycle-cache.py
and quiescence-gate.py / quiescence-cycle-cache.py share one implementation of
the "soonest time a goal becomes executable" computation.

Functions:
  _parse_iso(val)            -- tolerant ISO parse -> naive datetime | None
  _add_hours(dt, hours)      -- dt + timedelta(hours=hours) | None
  _goal_wake_time(goal, now) -- soonest wake time for a single goal | None
  _iter_goals(asps)          -- yield goal dicts from aspiration list
  scan_queue(now)            -- (goal_count, earliest_wake_at_iso | None)
"""

from datetime import datetime, timedelta

# Default defer TTL when a deferred goal carries no explicit deferred_until
# (mirrors capability-gate / quiescence-gate defer_reason_timeout_hours=120).
DEFAULT_DEFER_TIMEOUT_H = 120
# Default abstention TTL (aspirations-select Phase 2.55 abstention_timeout_hours).
ABSTENTION_TIMEOUT_H = 72
# Default recurring interval when a recurring goal names neither interval_hours
# nor remind_days (mirrors aspirations-select's `OR 24` fallback).
DEFAULT_RECURRING_INTERVAL_H = 24
# Floor below which we do not cap, to prevent busy-spin.
MIN_FLOOR_S = 60


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


def _goal_wake_time(goal, now):
    """The soonest time THIS goal could become executable, or None if it carries
    no timer. Covers five timer-gated 'becomes executable' events that have
    no wake-signal file: defer timeout, recurring interval, blocker expiry,
    abstention expiry, hypothesis-gate resolves_no_earlier_than. Purely
    defensive .get()s -- an unparseable field yields None for that lane,
    never a crash."""
    candidates = []
    status = str(goal.get("status") or "").lower()
    recurring = bool(goal.get("recurring"))

    # Terminal goals have no future 'becomes executable' event -- no wake timer
    # in ANY lane. The defer lane already guarded this inline; the recurring/
    # blocker/abstention/hypothesis lanes did NOT, so a completed/skipped/
    # expired/retired goal with a lingering lastAchievedAt / blocker_ref /
    # abstained_at / precondition contributed a stale PAST wake-time. That
    # pinned earliest_wake_at to the past and floored quiescence sleep at
    # MIN_FLOOR_S (observed: retired , skipped , completed
    #  all scored a months-old wake). .
    if status in ("completed", "skipped", "expired", "retired"):
        return None

    # Deferred -- ONLY while pending (a deferred goal is pending-but-soft-gated).
    # defer_reason LINGERS after blocked/in-progress transitions (observed :
    # status=blocked, foxtrot, yet a vestigial defer fired a 4.4d-past wake). Same
    # status-gated lingering-field class as the blocker/abstention lanes below.
    #  (guard-1389).
    # +candidate — §11b/ (world/conventions/goal-intake-management.md):
    # row 7's invisible half. :69 and :127 are a terminal skip and a `blocked`
    # test respectively, both safe untouched.
    if goal.get("defer_reason") and status in ("pending", "candidate"):
        du = _parse_iso(goal.get("deferred_until"))
        if du is not None:
            candidates.append(du)
        else:
            set_at = _parse_iso(goal.get("defer_reason_set_at"))
            if set_at is not None:
                timeout_h = goal.get("defer_reason_timeout") or DEFAULT_DEFER_TIMEOUT_H
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
                interval_h = (remind_days * 24) if remind_days else DEFAULT_RECURRING_INTERVAL_H
            w = _add_hours(last, interval_h)
            # FUTURE-ONLY (): a recurring goal whose next-due is in the
            # PAST is either executable NOW (the selector picks it -- the loop is
            # not dry) or currently gated by another lane (abstention/precond/
            # defer/blocker), which supplies the real FUTURE wake. Emitting a PAST
            # recurring-due can only floor quiescence at MIN_FLOOR_S while the goal
            # is actually gated until a LATER time (observed , echo-abstained
            # until 07-25 yet recurring-due 07-20 pinned the floor; 
            # precond-gated). The recurring lane is the one status-agnostic lane with
            # no natural "currently blocking?" guard -- `w > now` IS that guard.
            # Full conjunctive-gate precision (emit max(recurring-due, gate-release))
            # is deferred as a SAFE residual: skipping the past due-time already
            # removes the HARM (the floor); the future gate lane provides the wake,
            # and a None (no future lane) safely falls to the quiescence cap.
            if w is not None and w > now:
                candidates.append(w)

    # Blocked with a typed blocker_ref expiry -- ONLY while the goal is CURRENTLY
    # blocked. blocker_ref LINGERS after a goal un-blocks (blocked -> pending):
    # observed  + , both pending + recurring and achieved today,
    # yet each carried a MAY blocker_ref.expires_at that pinned earliest_wake_at
    # to the past and floored quiescence at MIN_FLOOR_S. A pending goal is ALREADY
    # an executable candidate -- its stale blocker expiry is not a future 'becomes
    # executable' event. Gate on status == "blocked" so only a LIVE blocker
    # contributes its expiry wake. One of four status-gated lanes in this class
    # (terminal guard + defer/abstention lanes are the siblings).  (guard-1389).
    br = goal.get("blocker_ref")
    if isinstance(br, dict) and status == "blocked":
        exp = _parse_iso(br.get("expires_at"))
        if exp is not None:
            candidates.append(exp)

    # Abstention expiry (abstained_at + 72h) -- ONLY while the goal is CURRENTLY
    # abstained. abstained_at LINGERS after a goal un-abstains (observed:
    #  pending + recurring, achieved today, but a May abstained_at still
    # set), so an unguarded check produced a spurious PAST abstention wake for a
    # goal whose real (recurring) wake is in the future -- flooring quiescence at
    # MIN_FLOOR_S. Gate on status == "abstained" so only a LIVE abstention
    # contributes its 72h-expiry wake. .
    ab = _parse_iso(goal.get("abstained_at"))
    if ab is not None and status == "abstained":
        w = _add_hours(ab, ABSTENTION_TIMEOUT_H)
        if w is not None:
            candidates.append(w)

    # Hypothesis-gate precondition with a resolves_no_earlier_than date.
    # These goals carry a precondition dict (or list) with a
    # resolves_no_earlier_than field from the gating hypothesis.
    preconds = goal.get("preconditions")
    if isinstance(preconds, dict):
        preconds = [preconds]
    if isinstance(preconds, list):
        for pc in preconds:
            if isinstance(pc, dict):
                rnb = _parse_iso(pc.get("resolves_no_earlier_than"))
                if rnb is not None:
                    candidates.append(rnb)

    # FUTURE-ONLY backstop -- generalizes 's recurring-lane `w > now`
    # guard to ALL lanes (defer/blocker/abstention/hypothesis-precondition). A
    # PAST candidate is never a "becomes executable" FUTURE event: the goal is
    # either executable NOW (its owner's selector picks it up) or gated by another
    # lane that supplies the real future wake. Emitting a past time only floors
    # quiescence at MIN_FLOOR_S. Before this, an unguarded past wake pinned
    # earliest_wake_at -- observed both as a precondition_unmet defer 14d past
    # (, deferred_until=None, defer_reason_set_at+120h=2026-07-10, flooring
    # EVERY quiescent sleep to 60s ~30x) and as a past blocker_ref.expires_at in a
    # bystander agent's cross-lane goal (2026-07-24, ~240s past). This single
    # global filter is the source of truth for every lane (the recurring inline
    # `w > now` is now redundant but harmless). Conjunctive-gate precision (emit
    # max over co-gating lanes) stays the SAFE deferred residual. Convergent
    # parallel fix reconciled at merge:  +  (-gen).
    # Lineage: /3016/3018 + guard-1389.
    future = [c for c in candidates if c > now]
    return min(future) if future else None


def _iter_goals(asps):
    for asp in asps:
        if isinstance(asp, dict):
            for g in asp.get("goals", []) or []:
                if isinstance(g, dict):
                    yield g


def scan_queue(now):
    """Single load of world + agent aspirations -> (goal_count, earliest_wake_at).

    goal_count is the total-goals new-work detector (mirrors
    quiescence-gate._total_goal_count). earliest_wake_at is the soonest timer
    across the queue (ISO string) or None. Raises on load failure so the caller
    fails open to a MISS; write_baseline_cache catches so a scan failure at
    write time simply leaves no cache (next check MISSes)."""
    import importlib
    qg = importlib.import_module("quiescence-gate")
    from _paths import WORLD_DIR, AGENT_DIR

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
