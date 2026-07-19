"""Pure claim-liveness verdict for claim-liveness-check.sh (3).

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

    LIVE: <reason>            claim intact (status=in-progress, claimed_by=agent)
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
