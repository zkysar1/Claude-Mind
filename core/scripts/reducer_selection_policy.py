#!/usr/bin/env python3
"""reducer_selection_policy — should the reducer stop competing for ordinary goals?

OWNER DIRECTIVE 2026-09-03 (g-306-419), after a measured read of the five-worker
run: the reducer runs the SAME goal-selector as every worker and competes with
five of them for ordinary goals it could leave to them, while the work only IT
can do starves. Measured that day: g-001-01 (Reflect and journal, 29.25h cadence,
reducer-only by nature) last ran 2026-08-26; g-306-284 (carrier dispositions)
holds its streak only because it is `intended_agent: alpha`; fleet guardrail
creation fell ~100/day (08-26) to 72/day (09-02) while completions held. rb-8344
already records that MEDIUM recurring sensors starve under a HIGH stream.

PURE BY CONSTRUCTION, and that is the point (guard-2783). This module reads no
files, no env, and no clock — every input is passed in. The defect this whole
class of change produces lives at the CALL SITE, in the population the decision
is applied to, not in the decision function, so the function is kept trivially
testable against BOTH roles and the wiring is what carries the risk.

THE ROLE GUARD RUNS BEFORE THE DECISION, NEVER AFTER. `role_of()` is evaluated
first and a WORKER returns immediately: a worker must select byte-identically to
today. `worker_execute.LIFECYCLE_DISPOSITIONS["select"]` says "A worker selects
exactly like the reducer -- same scorer, same candidate set. There is no
worker-specific selection logic and there must not be one", and nothing here adds
any: the worker branch is a no-op, and the asymmetry is entirely on the reducer
side. That is the direction the directive asks for.

THE PREDICATE IS THE SYSTEM'S OWN, NOT A NEW ONE (guard-2783: "two predicates for
one role is a second bug"). Two established signals, both required:
  * BODY_ROLE == "worker" -- exported by bash-agent-inject.py ONLY when a
    body-WM-file exists, i.e. only for a non-reducer Body. The reducer gets it
    UNSET. Same signal agent-watchdog.is_worker_body() reads.
  * sid == running_sid -- "the reducer is the Body holding running-session-id",
    the definition body-merge.py, health-ledger-append.py and bash-agent-inject.py
    all already use.
BOTH are required because BODY_ROLE-unset alone is not reducer-hood: an OBSERVER
session (reader/assistant) and an ad-hoc CLI run also have it unset, and neither
should get the reducer branch. Requiring the positive identity too makes the
answer UNKNOWN rather than "reducer" for them, and UNKNOWN does nothing.

WHY A FLOOR AND NOT A WEIGHT. The goal says "threshold and weight"; the
implementation is a threshold plus a one-slot floor, and the substitution is
deliberate. `apply_strategic_focus_floor` already measured this exact question on
this exact queue: a +1.5 scalar could not close a 4.41 deficit against an
exploration_noise width of 1.22, and guard-1895 rule (2) names an intervention
sized below the contested band as one that "changes almost nothing WHILE LOOKING
LIKE A FIX", prescribing removal from the competition instead. A weight would
have been the shape the directive's evidence already refutes.
"""
from __future__ import annotations

import collections
import datetime
from typing import Optional

#: Roles this module distinguishes. UNKNOWN is a first-class answer, not an
#: error: an observer session is genuinely neither, and saying so is what keeps
#: it out of the reducer branch.
ROLE_WORKER = "worker"
ROLE_REDUCER = "reducer"
ROLE_UNKNOWN = "unknown"

#: Branch names, recorded verbatim in the execution diary so the policy is
#: auditable from the record alone (outcome 1).
BRANCH_NOT_REDUCER = "not-reducer"
BRANCH_DISABLED = "disabled"
BRANCH_BELOW_THRESHOLD = "below-threshold"
BRANCH_PREFER_REDUCER_ONLY = "prefer-reducer-only"

DEFAULTS = {
    "enabled": True,
    "worker_threshold": 3,
    "claim_fresh_hours": 6.0,
}

Decision = collections.namedtuple(
    "Decision", "role live_workers branch prefer_reducer_only reason")


def role_of(body_role: Optional[str], sid: Optional[str],
            running_sid: Optional[str]) -> str:
    """WORKER / REDUCER / UNKNOWN from the two established signals.

    Order matters: the worker test comes FIRST and is decisive. A Body carrying
    BODY_ROLE=worker is never the reducer whatever else is true, and resolving
    that first means no later branch can reach a worker.
    """
    if (body_role or "").strip().lower() == ROLE_WORKER:
        return ROLE_WORKER
    s, r = (sid or "").strip(), (running_sid or "").strip()
    if s and r and s == r:
        return ROLE_REDUCER
    return ROLE_UNKNOWN


def _parse_ts(value) -> Optional[datetime.datetime]:
    """Naive ISO-8601, the fleet's one timestamp format. Unparseable -> None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.datetime.fromisoformat(value.strip().replace("Z", ""))
    except ValueError:
        return None


def live_worker_count(team_state: dict, now: datetime.datetime,
                      fresh_hours: float, exclude_sid: Optional[str] = None) -> dict:
    """Count in_flight_bodies rows across ALL agents with a FRESH claim.

    ALL agents, not just this one: every Body in the fleet selects from the same
    `world/aspirations.jsonl`, so every live Body is competition for an ordinary
    goal regardless of which agent it belongs to.

    FRESHNESS IS LOAD-BEARING AND THE GOAL SAYS SO -- "minus stale rows for
    terminal goals". Measured 2026-09-03 on the live board: bravo carried an
    in_flight_bodies row claimed 2026-08-07, 27 DAYS stale, for a goal long since
    terminal. Counting rows without an age test would report phantom workers and
    stand the reducer down against a fleet that is not there. g-306-412 owns
    CLEARING those rows; this function must be correct while they are still
    present, so it filters rather than assuming a clean store.

    A row with an unparseable or missing `claimed_at` is NOT counted. That is the
    fail-safe direction: an uncountable row makes the count SMALLER, which makes
    the reducer LESS likely to step back, which preserves today's behaviour. The
    opposite default would stand the reducer down on unreadable data.

    Returns a detail dict rather than a bare int so the diary record can explain
    itself (guard-3743: a decision record reproduces its verdict from its inputs).
    """
    counted, stale, undated, excluded = 0, 0, 0, 0
    try:
        agents = (team_state or {}).get("agent_status") or {}
        if not isinstance(agents, dict):
            agents = {}
    except Exception:  # pragma: no cover - fail-open guard
        agents = {}
    cutoff = None
    if isinstance(fresh_hours, (int, float)) and fresh_hours > 0:
        cutoff = now - datetime.timedelta(hours=float(fresh_hours))
    for _agent, row in sorted(agents.items()):
        bodies = (row or {}).get("in_flight_bodies")
        if not isinstance(bodies, dict):
            continue
        for sid, entry in sorted(bodies.items()):
            if exclude_sid and sid == exclude_sid:
                excluded += 1
                continue
            ts = _parse_ts((entry or {}).get("claimed_at")
                           if isinstance(entry, dict) else None)
            if ts is None:
                undated += 1
                continue
            if cutoff is not None and ts < cutoff:
                stale += 1
                continue
            counted += 1
    return {"live": counted, "stale": stale, "undated": undated,
            "excluded_self": excluded, "fresh_hours": fresh_hours}


def decide(*, role: str, live_workers: int, config: Optional[dict] = None) -> Decision:
    """The whole policy, in one pure function.

    Every branch returns prefer_reducer_only=False except the last, so a caller
    that ignores the branch name still cannot change behaviour by accident.
    """
    cfg = dict(DEFAULTS)
    if isinstance(config, dict):
        cfg.update({k: v for k, v in config.items() if k in DEFAULTS})
    try:
        threshold = int(cfg["worker_threshold"])
    except (TypeError, ValueError):
        threshold = DEFAULTS["worker_threshold"]

    if role != ROLE_REDUCER:
        return Decision(role, live_workers, BRANCH_NOT_REDUCER, False,
                        f"role={role}: the policy applies to the reducer only; "
                        f"selection is byte-identical to pre-policy behaviour")
    if not cfg.get("enabled"):
        return Decision(role, live_workers, BRANCH_DISABLED, False,
                        "reducer_selection_policy.enabled is false")
    if live_workers < threshold:
        return Decision(role, live_workers, BRANCH_BELOW_THRESHOLD, False,
                        f"{live_workers} live worker Bodies < threshold {threshold}: "
                        f"the reducer keeps competing for ordinary goals")
    return Decision(role, live_workers, BRANCH_PREFER_REDUCER_ONLY, True,
                    f"{live_workers} live worker Bodies >= threshold {threshold}: "
                    f"prefer reducer-only work and leave ordinary goals claimable")


def is_reducer_only_row(row: dict, skill_is_reducer_only: bool = False) -> bool:
    """Is this scored candidate work ONLY the reducer can do?

    ONE routing implementation, not two (the goal's own words, and why it was
    filed blocked_by g-115-7372). Two sources, checked in this order:

      1. `executable_by_role == "reducer"` -- the goal-level declaration
         g-115-7372 added. Checked FIRST because it is a deliberate assertion by
         the goal's author, where the skill bridge is an inference.
      2. `skill_is_reducer_only` -- the shared skill bridge's verdict for this
         row's skill, resolved by the CALLER via
         `worker_execute.skill_eligibility(skill).eligible is False`. Passed in
         as a plain bool so this module stays pure and there is no second copy
         of the bridge here; the caller owns the import and the caching.

    `executable_by_role` IS NOT ON main AS OF 2026-09-03 -- commit e62c24033
    (g-115-7372, COMPLETED) sits unconsumed on refs/workers/alpha/2fda1f3e..., so
    a tree grep finds the name in zero files and no goal can carry it yet (writes
    are gated on _goal_fields registration). Clause 1 is therefore INERT today and
    correct tomorrow: it reads a plain dict key, so it needs no import and cannot
    break while the field is absent, and it starts working the moment the reducer
    merges that ref. This is guard-4638 in its exact shape -- a CLOSED goal does
    not mean its code is on main -- and the note is here rather than in a commit
    message because the next reader of this function will otherwise conclude the
    clause is dead code and delete it.
    """
    if not isinstance(row, dict):
        return False
    if str(row.get("executable_by_role") or "").strip().lower() == ROLE_REDUCER:
        return True
    return bool(skill_is_reducer_only)
