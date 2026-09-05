"""Reallocation-exemption predicate — SSOT shared by the selector and the claim gate.

TWO COMPONENTS MUST AGREE ON ONE POLICY, and until this module existed they did
not (g-115-3492). The selector DELIBERATELY surfaces a cross-lane goal when the
reallocation condition holds ("fall through so a running capable agent can pick
up otherwise-stranded work"), and the daemon claim endpoint then REFUSED that
very goal with `cross_lane_refused` — a rescue path that surfaces work it cannot
deliver, terminating in a gate only an audited bypass can pass. The components
did not disagree about POLICY; the daemon simply could not EXPRESS the
selector's exception. This module is that expression, imported by both.

Structural sibling of ``_drain_title.py``: two independent code paths must
recognise the same condition, so the condition lives in one place and a change
to it cannot silently desync them (rb-3452 "assert the mechanism, not the case").

WHY THE PREDICATE IS NOT SHORT-CIRCUITED. ``evaluate`` computes EVERY conjunct
before returning, and reports all failures. An AND-predicate that stops at its
first false reports the CHEAPEST failing condition, which is almost never the
one that governs — so a probe can print the same true-but-shallow reason forever
while a decisive condition sits unread (guard-3644). The audit trail this feeds
(build constraint 3) is only worth writing if it says which conjunct actually
decided.

WHY SCOPE IS AN EXPLICIT REQUIRED PARAMETER. ``idle_agents`` and
``cadence_threshold`` are passed in, never defaulted and never derived from an
ambient bound agent. A predicate's NAME states the condition it tests, never the
SCOPE of the data it reads; a helper that silently resolves scope from its
caller's identity returns a plausible answer for the wrong subject, compiles
fine, and keeps every existing test green (guard-2601). The selector passes the
set it already computed once per run; the daemon passes the set it computed for
this one claim.

THE THRESHOLD FAILS TOWARD REFUSING. A missing or unparseable
``cadence_threshold`` closes the cadence door rather than opening it: this
number feeds a protective cutoff, so its fallback must keep the protection on
(guard-3024). An unreadable config must not become a fleet-wide exemption.

Public API:
    is_owner_scoped_goal(goal) -> bool
    recurring_cadence_stranded(goal, *, cadence_threshold) -> bool
    evaluate(goal, *, intended_agent, idle_agents, cadence_threshold) -> dict

``evaluate`` return shape:
    {
      "exempt": bool,          # True iff every conjunct holds
      "door": "idle" | "cadence" | None,   # which disjunct opened conjunct 1
      "failed": [str, ...],    # EVERY conjunct that failed, never just the first
      "reason": str,           # one-line human-readable summary
    }

Daemon safety: pure over its arguments. No env reads, no file I/O, no clock
beyond ``datetime.now()`` for cadence arithmetic, no telemetry.
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

# Resolve the sibling _drain_title.py without a package-relative import — this
# module is imported by the daemon (as `gates.reallocation_exempt`) and by
# goal-selector.py (via sys.path), so `from .._drain_title import ...` fails in
# the second path. Mirrors the resolution capability_route.py already uses.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _drain_title import is_drain_action_title  # noqa: E402


def _hours_since(timestamp_str):
    """Hours since a past timestamp, or None if absent/unparseable/in the future.

    Faithful to goal-selector.py::hours_since, whose contract this predicate was
    extracted from: handles both `YYYY-MM-DD` and `YYYY-MM-DDTHH:MM:SS`, and
    returns None for a NEGATIVE delta so an off-machine ahead-clock reads as
    "unknown" rather than as a large elapsed time.
    """
    if not timestamp_str:
        return None
    s = str(timestamp_str)
    try:
        if "T" in s:
            past = datetime.fromisoformat(s)
        else:
            past = datetime.combine(date.fromisoformat(s), datetime.min.time())
        hours = (datetime.now() - past).total_seconds() / 3600.0
        if hours < 0:
            return None
        return hours
    except (ValueError, TypeError):
        return None


def _interval_hours(goal):
    """Recurring interval in hours. Faithful to goal-selector.py::get_interval_hours:
    `interval_hours` first, else `remind_days * 24`, else 24. Returned RAW — a
    store record can carry it as a string, so callers must coerce.
    """
    if "interval_hours" in goal:
        return goal["interval_hours"]
    if "remind_days" in goal:
        return goal["remind_days"] * 24
    return 24


def is_owner_scoped_goal(goal):
    """True when a goal operates ONLY on its owner's own dir tree and so CANNOT
    be executed by a cross-agent reallocatee (rb-4792, g-115-2945).

    /drain-temp is the canonical case: its SKILL.md Phase 1 sets
    TEMP_DIR=$AGENT_DIR/temp and operates on the bound agent ONLY. Surfacing such
    a goal to another agent puts it in the reallocatee's candidate list where it
    is UNEXECUTABLE — running it drains the WRONG agent's temp, and running it as
    the owner collides with the owner's live session.

    Detected three independent ways so a rename of any one signal still catches
    it: the skill id, the origin_signal, or the title. The `maintain:temp-drain`
    Maintain goal (skill=None) is caught by origin_signal/title; the
    orchestrator-filed HIGH `/drain-temp` action goal is caught by skill.

    SCOPE NOTE: this recognises OWNER-scoped work (bound to an agent's own dir),
    NOT HOST-pinned work (bound to a physical machine — Studio, a plugin bridge,
    a box-local toolchain). Those are the same CLASS to a reallocatee — both are
    unexecutable wherever it is standing — but only the first is detected here.
    The host-pinned half is measured and unowned by this predicate; see
    g-115-3492's SECOND AXIS (26 of 48 released goals host-gated by their own
    prose) and g-115-5978. Do not read a False here as "any agent can run it".
    """
    if (goal.get("skill") or "") == "/drain-temp":
        return True
    origin = (goal.get("origin_signal") or "").lower()
    if "temp" in origin and "drain" in origin:
        return True
    # Positive drain-action signature () — the SAME SSOT matcher
    # precheck-eval.py dedup uses, so a title-template edit cannot desync them.
    if is_drain_action_title(goal.get("title")):
        return True
    return False


def recurring_cadence_stranded(goal, *, cadence_threshold):
    """True when a RECURRING goal is overdue past `cadence_threshold`.

    WHY THIS DOOR EXISTS (g-115-8700, measured 2026-09-02, landed 85ba6ab4c
    2026-09-03): a recurring goal routed to a LIVE but busy owner starves exactly
    like one routed to a dormant owner, and the idle door cannot free it because
    the owner is not idle. Two bravo-routed goals stopped firing for 144h and 9h
    while sitting at rank 1097/1180 on their owner's queue with recurring_urgency
    already at the urgency_max clamp — so no amount of further waiting could
    raise them, and every other Body was excluded by routed_to_agent. For CADENCE
    purposes a busy owner that never reaches rank 1 is indistinguishable from a
    dormant one.

    THE UNIT IS overdue_ratio, NOT AN ELAPSED MULTIPLE, and they differ by
    exactly one: overdue_ratio = elapsed/interval - 1. A threshold of 2.0
    therefore means elapsed >= 3x interval.

    A goal whose `lastAchievedAt` FIELD is absent has never fired, so its clock
    starts at created_at (g-115-1763). Keying on the FIELD's absence rather than
    on a None elapsed matters — an off-machine ahead-clock stamps a FUTURE
    lastAchievedAt, which _hours_since also reports as None, and treating that as
    never-fired would free a goal that just ran.
    """
    if not goal.get("recurring"):
        return False
    # An unreadable threshold closes this door (guard-3024): the number feeds a
    # protective cutoff, so its failure mode must keep the protection on.
    try:
        threshold = float(cadence_threshold)
    except (TypeError, ValueError):
        return False
    try:
        # float() is not defensive decoration: _interval_hours returns the RAW
        # field and a store record can carry it as a string. Un-coerced, "48"
        # compares fine against 0 but explodes on the subtraction below — and
        # this runs inside candidate collection, so one malformed goal would
        # take down selection for every goal.
        interval = float(_interval_hours(goal))
    except (TypeError, ValueError):
        return False
    if interval <= 0:
        return False
    la_raw = goal.get("lastAchievedAt")
    la = _hours_since(la_raw)
    if la_raw is None:  # FIELD absence == never fired ()
        la = _hours_since(goal.get("created_at") or goal.get("created"))
    if la is None:
        return False
    return ((la - interval) / interval) >= threshold


def confirms_dormant(name, last_active_iso, *, threshold_hours, world_dir):
    """True only when liveness_check UPHOLDS the dormant conclusion for `name`
    (g-115-2315). A stale `last_active` alone is NOT evidence of death — it is
    ambiguous between "idle" and "alive with a broken heartbeat writer", and two
    live agents once read 59h and 66h stale (check-team-state-before-silent.md
    rule 5). So the age test only nominates; this confirms.

    ONLY the `dormant` verdict counts. `unknown` means the signal was unreadable
    or contradicted, and a wrong "dormant" here hands a LIVE peer's goal to
    someone else. `retired` means decommissioned — also unreachable, but it is
    deliberately NOT treated as idle by this predicate today, because the
    selector it was extracted from does not, and behavioural equivalence
    outranks tidiness at extraction time (guard-2184). Routing work away from a
    retired agent belongs to the retirement path, not to reallocation.

    DELIBERATELY NOT MEMOIZED HERE. Caching belongs to the caller, because the
    two callers have opposite lifetimes: the selector is a short-lived process
    that must probe each stale agent once per RUN (its own wrapper holds that
    memo), while the daemon lives for days and a process-lifetime cache would
    pin "dormant" long after the agent came back — freezing a verdict that
    decides whether a live peer's work gets handed away. A single claim probes
    once anyway, so there is nothing for the daemon to amortise.

    Fail-safe: ANY import/probe error returns False (NOT idle) — the same
    direction as decide_liveness's `unknown`, degrading toward
    goals-stay-routed (slow, but never wrongly leaked).
    """
    try:
        import os
        from datetime import datetime as _dt
        import liveness_check as _lc
        backend = os.environ.get("STORAGE_BACKEND", "local")
        fresh_iso = _lc.fetch_fresh_signal(name, str(world_dir), backend)
        # The shard OBJECT's write time is BODY activity; the last_active VALUE
        # inside the authoritative shard is MIND liveness (-e). Supply
        # both, with provenance (), so this routing decision cannot
        # disagree with the CLI verdict — object-time alone made a worker Body's
        # write look like a live reducer, and a mirror value promoted to
        # verdict=alive would strand a dead agent's goals indefinitely.
        auth_iso, auth_prov = _lc.fetch_authoritative_last_active_with_provenance(
            name, str(world_dir))
        # A row stamped by ANOTHER agent cannot certify its subject alive
        # (, guard-3604): clearing a dormant peer's stranded in_flight
        # bumps the CLEARED row's last_active, so a peer the fleet just policed
        # reads fresh for a full window.
        row_stamp = _lc.fetch_row_stamp(name, str(world_dir))
        verdict = _lc.decide_liveness(
            last_active_iso, fresh_iso, threshold_hours=threshold_hours,
            now=_dt.now(),
            authoritative_last_active_iso=auth_iso,
            authoritative_provenance=auth_prov,
            row_updated_by=row_stamp, row_agent=name)["verdict"]
        dormant = (verdict == "dormant")
    except Exception:  # noqa: BLE001 — fail-safe toward NOT idle
        dormant = False
    return dormant


def idle_agents(agent_status, *, reallocation_hours, confirm):
    """Set of agent names whose routing may be overridden because they are
    verifiably unreachable (g-115-1766 gap #4).

    `agent_status` is the COMPOSED team-state rows dict, passed in rather than
    read here: each caller already owns a tested reader for it (the selector a
    cached-per-run one with shard composition and partial-YAML retries, the
    daemon its own), and moving that plumbing would risk the very divergence
    this module exists to prevent. What lives here is the AGE policy — who is
    even a candidate — so both callers cannot disagree about the threshold.

    `confirm(name, last_active_iso) -> bool` is the caller's dormancy
    confirmation, REQUIRED and never defaulted. Both real callers hand it a
    closure over `confirms_dormant` below, so the probe itself stays a single
    implementation; what differs is only WHERE the per-run memo lives. Making it
    a parameter rather than a hard-wired call is what lets the selector keep its
    own patchable cache (its test-suite controls liveness through exactly that
    seam) without this module growing a second notion of who is alive. An
    optional-with-default hook would silently re-open that hole for the next
    caller while still compiling (guard-2601).

    Conservative + fail-open: an empty set when reallocation is disabled
    (`reallocation_hours is None`), and an agent with a missing/unparseable
    `last_active` is NOT idle (keep routing). Age NOMINATES; only `confirm`
    decides — a stale timestamp alone is ambiguous between "idle" and "alive
    with a broken heartbeat writer".
    """
    if reallocation_hours is None:
        return set()
    idle = set()
    for name, row in (agent_status or {}).items():
        if not isinstance(row, dict):
            continue
        age = _hours_since(row.get("last_active"))
        if age is not None and age > reallocation_hours:
            if confirm(name, row.get("last_active")):
                idle.add(name)
    return idle


def evaluate(goal, *, intended_agent, idle_agents, cadence_threshold):
    """Decide whether `goal`, routed away from the claimer, is nonetheless
    exempt from the cross-lane refusal because the selector's reallocation
    condition holds.

    The caller is responsible for having already established that the goal
    ROUTES AWAY from the claimer (`routes_away_from`); this function does not
    re-derive that, because the two callers reach it by different routes.

    Every conjunct is evaluated — see the module docstring on guard-3644.
    """
    idle_set = set(idle_agents or ())
    door_idle = intended_agent in idle_set
    door_cadence = recurring_cadence_stranded(
        goal, cadence_threshold=cadence_threshold)

    unclaimed = not goal.get("claimed_by")
    not_owner_scoped = not is_owner_scoped_goal(goal)

    failed = []
    if not (door_idle or door_cadence):
        failed.append("target-neither-idle-nor-cadence-stranded")
    if not unclaimed:
        failed.append("already-claimed")
    if not not_owner_scoped:
        failed.append("owner-scoped")

    exempt = not failed
    # Report the IDLE door first when both are open: it is the identity-computed
    # one (team-state positively shows the target idle), so it is the stronger
    # evidence to record in the audit trail.
    door = ("idle" if door_idle else "cadence") if (door_idle or door_cadence) else None

    if exempt:
        reason = (f"reallocation-exempt via {door} door: intended_agent "
                  f"'{intended_agent}' unreachable, goal unclaimed and not "
                  f"owner-scoped")
    else:
        reason = "not reallocation-exempt: " + ", ".join(failed)

    return {"exempt": exempt, "door": door, "failed": failed, "reason": reason}
