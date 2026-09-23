"""Pure claim-liveness verdict for claim-liveness-check.sh ().

Layer B automation for guard-1151: mid-execution, before an irreversible
action (daemon restart is the wired chokepoint — mind-api-start.sh
FORCE_RESTART-on-healthy branch), verify the invoking agent's claim on the
goal it is executing is still live. Claims can be superseded and released
while the agent executes (canonical: g-115-2407 ran 47 minutes past its own
supersession and performed a redundant daemon restart inside the blind
window, 2026-07-16; rb-3735).

Pure stdin->stdout: reads the FULL goal record JSON on stdin (the caller
resolves it via aspirations-read.sh; aspirations-query.sh projects only 6
fields and drops claimed_by), takes --agent and --goal-id args, prints ONE
verdict line:

    LIVE: <reason>            claim intact (claimed_by=agent AND status is
                              in-progress, OR 'pending' — the shape
                              aspirations-claim.sh leaves behind; g-115-10473)
    STALE: <reason>           claim superseded / released / taken over
    INDETERMINATE: <reason>   record unreadable or goal not found (fail-open)

Exit code is always 0 — the WRAPPER maps STALE to its own exit 1. This
helper never spawns subprocesses (rb-225/rb-247: helpers piped from bash
must not re-enter bash).

Fail-open doctrine: any parse/lookup failure is INDETERMINATE, never STALE.
A wrong refusal would block legitimate daemon-recovery restarts; the harm
asymmetry favors letting an occasional stale-claim restart through over
freezing recovery (the unhealthy-daemon restart paths are deliberately
unwired — recovery must never consult a daemon that is down).
"""
from __future__ import annotations

import argparse
import json
import sys


def verdict(payload_text: str, agent: str, goal_id: str) -> tuple[str, str]:
    """Classify claim liveness from a goal-record JSON payload.

    Accepts either a bare goal record, a list of records, or an aspiration
    record carrying a "goals" list (the aspirations-read.sh shape).
    """
    try:
        d = json.loads(payload_text)
    except Exception:
        return ("INDETERMINATE", "goal record unreadable (fail-open)")

    candidates: list = []
    if isinstance(d, dict):
        if d.get("id") == goal_id or d.get("goal_id") == goal_id:
            candidates = [d]
        elif isinstance(d.get("goals"), list):
            candidates = d["goals"]
        elif isinstance(d.get("aspiration"), dict):
            candidates = d["aspiration"].get("goals", [])
    elif isinstance(d, list):
        candidates = d

    rec = None
    for g in candidates:
        if not isinstance(g, dict):
            continue
        if g.get("id") == goal_id or g.get("goal_id") == goal_id:
            rec = g
            break

    if rec is None:
        return ("INDETERMINATE", f"goal {goal_id} not found in payload (fail-open)")

    status = rec.get("status")
    claimed_by = rec.get("claimed_by")

    # : `pending` + OUR OWN claim is a LIVE claim that has not been
    # advanced yet — it is the shape aspirations-claim.sh PRODUCES (it writes
    # claimed_by/claimed_at/started/executed_by and deliberately leaves status
    # at 'pending'). Without this branch the broader branch below hands that
    # normal claim output a DEFINITE "STALE", citing three causes — completed,
    # superseded, released — none of which happened (guard-3616: a classifier's
    # fall-through must not assign a definite verdict to a case it has no
    # information about; this file already states the house rule by carrying
    # INDETERMINATE).
    #
    # THE SLOT IS LOAD-BEARING (guard-6943): BELOW the two read-quality
    # INDETERMINATE guards above — an unreadable or absent record must never
    # reach a definite verdict — and ABOVE the weaker `status != in-progress`
    # branch, which fires on a broader predicate and would otherwise swallow
    # the case this teaches the chain to recognise.
    #
    # `claimed_by == agent` is what makes admitting 'pending' safe: release()
    # pops claimed_by/claimed_at/claimed_by_sid together (aspirations_write.py
    # release), so a RELEASED claim cannot reach here —
    # test_stale_when_released_to_pending pins exactly that pairing
    # (pending + claimed_by=None -> STALE) and is unaffected. Every terminal
    # status (completed/skipped/expired/superseded/decomposed) still falls
    # through to STALE below. This adds no looseness the in-progress arm did
    # not already have: both are agent-NAME scoped, and the caller resolves the
    # goal id from its OWN Body-scoped in_flight row.
    #
    # MEASURED cc-03 2026-09-22 (Linux 6.8.0-139-generic): a live claim made
    # seconds earlier by aspirations-claim.sh read STALE, and this box's own
    # mind_api/state/spawn.log carries two REFUSED --restart lines
    # ( 07:27,  06:22, both 2026-09-20) — a second-box
    # replication of the cc-04 incident that filed this goal (guard-7213).
    if status == "pending" and claimed_by == agent:
        return ("LIVE",
                f"status='pending' claimed_by={agent} — claimed but not yet "
                f"advanced (aspirations-claim.sh leaves status at 'pending'; "
                f"g-115-10473)")

    if status != "in-progress":
        return ("STALE",
                f"status={status!r} (expected in-progress) — claim was "
                f"completed, superseded, or released while executing")
    if claimed_by != agent:
        return ("STALE",
                f"claimed_by={claimed_by!r} != {agent!r} — claim was "
                f"released or taken over while executing")
    return ("LIVE", f"status=in-progress claimed_by={agent}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True)
    ap.add_argument("--goal-id", required=True)
    args = ap.parse_args()
    kind, reason = verdict(sys.stdin.read(), args.agent, args.goal_id)
    print(f"{kind}: {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
