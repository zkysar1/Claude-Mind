#!/usr/bin/env python3
"""_abandoned_claim.py — detect claims that are held by nobody ().

THE CLASS. A goal can sit `status=in-progress`, `claimed_by=<agent>`, while NO
live Body holds it. Measured 2026-09-04: one critical-path goal sat that way from
16:26 to 20:17. On release it scored 21.45 — rank 1 of 1871, next candidate
14.93 — and was claimed within 2 minutes. Two dependent goals waited behind it
the whole time, one of them for 7.3 days.

WHY THE THREE EXISTING TOOLS CANNOT SEE IT. Each name suggests coverage its
predicate does not have (guard-6002, tree node enumerator-all-clear-boundary):

  aspirations-clear-stale-claims.sh  predicate is "status is TERMINAL" — claim
                                     residue self-heal. An in-progress goal is
                                     not terminal, so it reports 0.
  claim-liveness-check.sh            asks "is this claim still MINE"
                                     (guard-1151 supersession). Its verdict()
                                     returns LIVE on
                                     `status==in-progress and claimed_by==agent`
                                     and never reads in_flight_bodies, so the
                                     abandoned claim reads healthy.
  claim-integrity-check.sh           reconcile damage / partial field survival.
                                     The fields are all intact here; verdict
                                     is clean.

None of the three is wrong. The population simply falls between them, which is
why this is a separate lane rather than a widened predicate on any of them.

THE PREDICATE IS ONE LINE:
    claimed_by is set AND status == "in-progress" AND the goal id appears in no
    agent_status[*].in_flight_bodies[*].goal_id (nor any legacy in_flight.goal_id)

BOTH IN-FLIGHT SHAPES ARE REQUIRED, and reading only `in_flight` opens this
detector completely (g-306-276). `in_flight` is REDUCER-owned —
team-state-in-flight.sh stamps it only when the box's running-session-id equals
MIND_SID, and SKIPs for every other Body, writing `in_flight_bodies.<sid>`
instead. A partner running as a WORKER Body is therefore invisible in
`in_flight`. Reading one shape would report every worker-held goal as abandoned
— the exact inversion this module exists to prevent.

KEEP-SAFE DIRECTION (guard-4000). The default is REPORT. A release fires only
when ALL FOUR hold:

  1. no in-flight row of either shape names the goal,
  2. claimed_at is older than `threshold_minutes` (default 180, matching
     DEFAULT_REAP_STALE_MINUTES),
  3. the goal is non-terminal, and
  4. the team-state read was AUTHORITATIVE.

Condition 4 is not ceremony. `team-state-read.sh --json` is the authoritative
reader; the local tree is a read-through cache under own-cloud (guard-980), and
a mirror read that came back thin would show zero in-flight rows — making every
claim in the fleet look abandoned. So an unauthoritative read must release
NOTHING while still being allowed to report; `find_abandoned` enforces that by
zeroing `releasable` rather than by suppressing the row.

Condition 2 exists for a specific race: a Body between its claim write and its
first in_flight row write holds a real claim with no row yet. That window must
not be reaped. The threshold is the bound on it, so it is a parameter, not a
constant — measure the window before lowering it.

Pure and dependency-free by design: `find_abandoned` takes plain data so the
predicate can be tested without a daemon, a world, or a live fleet.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

DEFAULT_THRESHOLD_MINUTES = 180  # mirrors DEFAULT_REAP_STALE_MINUTES

TERMINAL_STATUSES = frozenset(
    {"completed", "skipped", "expired", "superseded", "decomposed"}
)


def _parse_ts(value: Any) -> datetime | None:
    """Parse a naive ISO stamp. Returns None on anything unparseable.

    Naive by fleet convention (CLAUDE.md: no zone suffix, UTC wall time on every
    box). A `Z` or offset suffix is tolerated rather than rejected, because a
    stamp we cannot read must degrade to "age unknown" — which blocks release —
    never to a crash that takes the whole sweep down.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=None)


def _age_minutes(stamp: Any, now: datetime) -> float | None:
    parsed = _parse_ts(stamp)
    if parsed is None:
        return None
    return (now - parsed).total_seconds() / 60.0


def held_goal_ids(team_state: dict | None) -> dict[str, list[str]]:
    """Map goal_id -> the in-flight rows naming it, across BOTH shapes.

    Returns a mapping rather than a set so a caller can say WHO holds a goal —
    a report that says "held" without naming the holder cannot be acted on.
    """
    held: dict[str, list[str]] = {}
    if not isinstance(team_state, dict):
        return held
    for agent, status in (team_state.get("agent_status") or {}).items():
        if not isinstance(status, dict):
            continue
        # Shape 1: reducer-owned single row.
        flight = status.get("in_flight")
        if isinstance(flight, dict) and flight.get("goal_id"):
            held.setdefault(str(flight["goal_id"]), []).append(f"{agent}:in_flight")
        # Shape 2: per-Body rows, keyed by sid. The half a single-shape read misses.
        bodies = status.get("in_flight_bodies")
        if isinstance(bodies, dict):
            for sid, row in bodies.items():
                if isinstance(row, dict) and row.get("goal_id"):
                    held.setdefault(str(row["goal_id"]), []).append(
                        f"{agent}:body:{str(sid)[:8]}"
                    )
    return held


def find_abandoned(
    goals: Iterable[dict],
    team_state: dict | None,
    now: datetime,
    threshold_minutes: float = DEFAULT_THRESHOLD_MINUTES,
    authoritative: bool = True,
) -> dict:
    """Report claims no live in-flight row accounts for.

    The return carries the POPULATION alongside the finding count, so a small
    number is never mistaken for a small queue (guard-3830) and a zero can be
    told apart from a scan that saw nothing.
    """
    held = held_goal_ids(team_state)
    scanned = 0
    claimed_in_progress = 0
    rows: list[dict] = []

    for goal in goals:
        if not isinstance(goal, dict):
            continue
        scanned += 1
        if goal.get("status") != "in-progress":
            continue
        claimed_by = goal.get("claimed_by")
        if not claimed_by:
            continue
        claimed_in_progress += 1
        goal_id = str(goal.get("id") or "")
        if goal_id in held:
            continue  # a live row accounts for it — not abandoned

        age = _age_minutes(goal.get("claimed_at") or goal.get("started"), now)
        # Every release condition, evaluated explicitly so the row shows WHY it
        # is or is not releasable. An unknown age blocks release: we cannot
        # prove the claim is older than the claim-to-row race window.
        old_enough = age is not None and age >= threshold_minutes
        non_terminal = goal.get("status") not in TERMINAL_STATUSES
        releasable = bool(old_enough and non_terminal and authoritative)

        reasons = []
        if not old_enough:
            reasons.append(
                "claim younger than threshold"
                if age is not None
                else "claimed_at unparseable — age unknown"
            )
        if not non_terminal:
            reasons.append("goal is terminal")
        if not authoritative:
            reasons.append("team-state read was NOT authoritative")

        rows.append(
            {
                "goal_id": goal_id,
                "claimed_by": claimed_by,
                "claimed_by_sid": goal.get("claimed_by_sid"),
                "claimed_at": goal.get("claimed_at") or goal.get("started"),
                "age_minutes": None if age is None else round(age, 1),
                "title": str(goal.get("title") or "")[:100],
                "aspiration_id": goal.get("aspiration_id"),
                "releasable": releasable,
                "hold_reasons": reasons,
            }
        )

    rows.sort(key=lambda r: (r["age_minutes"] is None, -(r["age_minutes"] or 0)))
    return {
        "scanned_goals": scanned,
        "claimed_in_progress": claimed_in_progress,
        "in_flight_rows": sum(len(v) for v in held.values()),
        "held_goal_ids": sorted(held),
        "abandoned_count": len(rows),
        "releasable_count": sum(1 for r in rows if r["releasable"]),
        "threshold_minutes": threshold_minutes,
        "authoritative": authoritative,
        "abandoned": rows,
    }
