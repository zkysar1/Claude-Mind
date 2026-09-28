"""Long-hold predicate: SSOT shared by the goal selector and the claim endpoint ().

A WORKER BODY KEEPS ITS GOAL FOR AS LONG AS IT IS STILL WORKING IT. A Body's claim
carries two clocks and, until this module, both consumers read the wrong one. The
per-SID ``in_flight_bodies`` row stamps ``claimed_at`` ONCE, at claim time, and no
writer ever refreshes it. The selector aged a sibling's claim out after
``multi_agent.claim_timeout_hours`` (4) and the claim endpoint after
``runner_heartbeat.stale_minutes`` (60), both measured on that one stamp. Goals on
the local-inference fleet run 6-17 h per Body (measured 2026-09-27), so a Body that
was demonstrably alive and on its goal became takeable hours before it could finish.
Each take-over is ledgered (``claim-cross-session-takeover`` in the world
override-bypass ledger: 76 rows from 2026-08-20 to 2026-09-27). A row names the
displaced session, never whether it was still alive, and a wrongful one throws away
the displaced Body's hours on the goal and risks duplicate side effects.

The clock that answers "is it still working this goal" is the body-heartbeat carrier
(``session/body-heartbeat-<SID>.json``). ``heartbeat-tick.sh`` rewrites it at the top
of every worker cycle, and it stops when the Body dies. So a Body HOLDS its goal
while ALL of these hold, and loses it the moment any fails:

  row_names_goal      its per-SID row names THIS goal;
  within_hold_cap     that row's ``claimed_at`` is at most MAX_BODY_HOLD_HOURS old;
  carrier_fresh       its carrier's ``ts`` is at most CARRIER_FRESH_MINUTES old;
  carrier_is_its_own  the carrier's embedded ``sid`` is the holder's (guard-358: a
                      carrier cannot vouch for a Body that did not write it);
  carrier_not_closed  its ``body_state`` is not in CLOSED_BODY_STATES.

Every conjunct is evaluated and every failure reported, never just the first
(guard-3644), exactly as ``reallocation_exempt`` does.

WHY 24 HOURS. A backstop for a Body whose heartbeat keeps ticking while it never
finishes (a wedged loop still laps its cycles). It is the top of the range
``claim_timeout_hours`` already allows and above the longest legitimate hold measured
(17 h). Past it the claim expires exactly as it did before this module.

WHY 100 MINUTES. The carrier's write cadence IS the worker cycle cadence, measured at
15-92 min, and a window below that false-stales live holders. It is the same number,
for the same reason, as ``stranded-claim-sweep.py``'s DEFAULT_CARRIER_FRESH_MINUTES
(g-115-6936, pinned there by test_carrier_window_brackets_the_worker_cycle_cadence).

FAILS TOWARD PERMITTING. A missing row, an unparseable stamp, an absent or unreadable
carrier: each fails its conjunct, and a failed conjunct lets the claim expire as it
did before. This can only keep a claim whose holder is positively alive and on the
goal; it never wedges a goal on missing evidence (the guard-1562 direction).

ONE CARRIER VERDICT FOR EVERY DOOR (g-375-50). Three doors judge the same carrier
file: this module (the selector and the claim endpoint's ``stale`` branch), the claim
endpoint's absent-row path, and ``stranded-claim-sweep.py``. They used to apply
different conjuncts: the absent-row path a 60-minute window and no ``sid`` check, the
sweep no ``body_state`` check. Sampled every 5 min for 6 h on 2026-09-27/28, a live
Body's carrier went 62.5 min between refreshes during one long goal, and at that
sample the absent-row path read it as gone while the other two kept it. All three now
call ``evaluate_carrier``, so one carrier gets one verdict.

Public API:
    evaluate(row, carrier, *, goal_id, sid, now) -> {"holds", "failed", "reason"}
    evaluate_carrier(carrier, *, sid, now, fresh_minutes=CARRIER_FRESH_MINUTES)
        -> {"live", "failed", "reason", "age_minutes"}

Daemon safety: pure over its arguments. No env reads, no file I/O, and no clock:
the caller passes ``now``.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

MAX_BODY_HOLD_HOURS = 24
CARRIER_FRESH_MINUTES = 100

# MIRRORED from body-manifest.py CLOSED_STATES, the SSOT, which a hyphenated file
# name keeps from being imported. Partition site SEVEN, pinned against that SSOT by
# core/scripts/tests/test_graceful_body_close.py.
CLOSED_BODY_STATES = frozenset(
    {"closed-pending-merge", "merged", "closed-stale", "closed-graceful"})


def _stamp(value: Any) -> datetime | None:
    """A naive-UTC ``YYYY-MM-DDTHH:MM:SS`` stamp, or None when absent or unparseable."""
    try:
        return datetime.strptime(str(value).strip()[:19], "%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError):
        return None


def evaluate_carrier(carrier: Any, *, sid: str, now: datetime,
                     fresh_minutes: float = CARRIER_FRESH_MINUTES) -> dict:
    """Is ``carrier`` a live Body's own heartbeat? The three carrier conjuncts, shared
    by every door that reads the carrier (g-375-50). ``carrier`` is the decoded
    document, or None when the caller could not read it. ``fresh_minutes`` exists for
    a caller whose window an operator set explicitly (the sweep's
    ``--carrier-fresh-minutes``); every default is CARRIER_FRESH_MINUTES.
    ``age_minutes`` is None when ``ts`` is absent or unparseable."""
    carrier = carrier if isinstance(carrier, dict) else {}
    failed = []
    beat = _stamp(carrier.get("ts"))
    age = None if beat is None else (now - beat).total_seconds() / 60.0
    if age is None or age > fresh_minutes:
        failed.append("carrier_fresh")
    if not sid or str(carrier.get("sid") or "") != str(sid):
        failed.append("carrier_is_its_own")
    if str(carrier.get("body_state") or "") in CLOSED_BODY_STATES:
        failed.append("carrier_not_closed")
    return {
        "live": not failed,
        "failed": failed,
        "reason": "live" if not failed else "not live: " + ", ".join(failed),
        "age_minutes": age,
    }


def evaluate(row: Any, carrier: Any, *, goal_id: str, sid: str,
             now: datetime) -> dict:
    """Does the Body ``sid`` still hold ``goal_id``? ``row`` is its
    ``in_flight_bodies[sid]`` entry and ``carrier`` its decoded body-heartbeat
    document; either may be None when the caller could not read it."""
    row = row if isinstance(row, dict) else {}
    failed = []
    if not goal_id or str(row.get("goal_id") or "") != str(goal_id):
        failed.append("row_names_goal")
    claimed = _stamp(row.get("claimed_at"))
    if claimed is None or now - claimed > timedelta(hours=MAX_BODY_HOLD_HOURS):
        failed.append("within_hold_cap")
    failed.extend(evaluate_carrier(carrier, sid=sid, now=now)["failed"])
    return {
        "holds": not failed,
        "failed": failed,
        "reason": "holds" if not failed else "released: " + ", ".join(failed),
    }
