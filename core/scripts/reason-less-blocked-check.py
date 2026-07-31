#!/usr/bin/env python3
"""Reason-Less-Blocked Check — flag status=blocked goals that carry NO
blocker_ref, NO blocked_by, AND NO defer_reason (a Blocker Reference Schema
violation that escapes selection, blocker-recheck, AND quiescence).

THE GAP THIS FILLS (g-115-2595, surfaced by g-115-2591):
A goal set to status=blocked via a direct status update — with blocker_ref None,
blocked_by [], defer_reason None — is invisible to every existing guard:
  * gates/blocker_ref.py validates a blocker_ref's STRUCTURE only when one is
    paired with a defer_reason (no defer_reason here -> the gate never fires).
  * blocker-create-gate.py fires at CREATE time (these goals were created
    pending and later flipped to blocked, so the create gate never saw them).
  * blocker-recheck.py re-probes goals that HAVE a blocker_ref/blocked_by to
    re-examine (empty here -> nothing to recheck, never auto-unblocks).
Result: the goal is invisible to selection (blocked goals are not selected), to
blocker-recheck's auto-unblock, AND to quiescence reasoning. Worse (the
g-115-2198-b class): a GENUINE peer-dependency existed (blocked_by=g-115-2068
per alpha's own experience doc) but the structured field never persisted, so
when g-115-2068 COMPLETED 2026-07-16 nothing auto-unblocked the dependent goal —
it stranded ~2 days until a felt-sense RAW queue read caught it.

DETECTIVE + SELF-CONTAINED FILING. Detection runs mechanically every iteration
(bash-enforced via the precheck wrapper, NOT LLM-discretionary — an LLM WARN is
the exact drift this goal exists to fix; guard-616/rb-616). With --apply it
files ONE deduplicated Investigate (origin_signal
"investigate:reason-less-blocked-audit") listing the reason-less-blocked goals
for reconciliation: reconstruct the real blocker into blocked_by/blocker_ref, OR
unblock to pending if the premise is gone (the g-115-2591 reconcile protocol).

DEDUP IS BY THE SAME READ (fail-closed by construction, guard-487). The open
audit Investigate is detected IN THE SAME active read that finds the blocked
goals — no separate query that could error into a blind double-file. Because
that read is guard-383 fatal on error, a read failure aborts BEFORE any filing
(cannot file blindly), and at most one open audit exists at a time (a missed
filing re-detects next iteration; a cross-box duplicate does not self-heal, so
skip-on-uncertainty is correct — guard-487).

WHY NO AGE FILTER (unlike the defer sweeps): a reason-less-blocked goal is a
STRUCTURAL violation, not a time-based one — a blocked goal must always carry a
reason. Goal writes are whole-record atomic (no transient blocked-then-set-field
window), so there is no just-flipped state to suppress. A properly-blocked goal
(any one of the three fields present) is never flagged, at any age.

JSON output:
  {
    "scanned": N,
    "reason_less_count": N,
    "reason_less": [
      {"goal_id", "source", "aspiration_id", "intended_agent",
       "blocked_since", "title"}
    ],
    "open_audit_exists": bool,          # an investigate:reason-less-blocked-audit
    "open_audit_goal_id": str | None,   #   already pending/in-progress
    "investigate_filed": str | None,    # goal_id filed this run (--apply only)
    "actions_taken": "dry-run" | "apply",
    "now": iso,
  }

Sibling pattern (rb-428 self-contained-apply family): blocker-recheck.py,
handoff-aging-check.py, inbox-alert-age-check.py. Detective template:
defer-drift-check.py. Guards honored: guard-383 (fatal per-source read error;
the single fail-open boundary is the shell wrapper), guard-420 (tolerant
datetime — n/a, no arithmetic), guard-487 (dedup fail-closed), guard-510
(measured severity — a WARN, not "CRITICAL"), guard-614 (structured JSON
output), guard-645 (every field read with a default). Reference: g-115-2595.
"""

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)

# : never hardcode the escalation aspiration —  is the UPSTREAM
# deployment's queue and does not exist elsewhere, so a literal files nothing.
try:
    from _paths import AGENT_DIR, WORLD_DIR  # noqa: E402
    from _escalation_target import resolve as _resolve_asp, source_flag as _asp_source
    ESCALATION_ASP, _ESCALATION_ASP_VIA = _resolve_asp(CORE_ROOT, WORLD_DIR, AGENT_DIR)
    ESCALATION_SOURCE = _asp_source(ESCALATION_ASP, WORLD_DIR, AGENT_DIR)
except Exception:
    ESCALATION_ASP, _ESCALATION_ASP_VIA, ESCALATION_SOURCE = (
        "asp-115", "fallback:import-failed", "world")

AUDIT_ORIGIN_SIGNAL = "investigate:reason-less-blocked-audit"
NON_TERMINAL_OPEN = ("pending", "in-progress")


def _is_reason_less_blocked(goal) -> bool:
    """Pure eligibility test for ONE goal (no I/O — unit-testable).

    True IFF the goal is status=blocked AND ALL THREE structured blocker fields
    are empty:
      - blocker_ref  falsy (None / "" / absent)
      - blocked_by   empty (None / [] / absent)
      - defer_reason falsy (None / "" / absent)
    Any one field present means the block carries a reason and is NOT flagged.
    """
    if not isinstance(goal, dict):
        return False
    if goal.get("status") != "blocked":
        return False
    if goal.get("blocker_ref"):
        return False
    if goal.get("blocked_by"):
        return False
    if goal.get("defer_reason"):
        return False
    return True


def _read_goals(source):
    """Read all active goals from world or agent queue via the daemon.

    guard-383 fatal symmetry (rb-987): a per-source read error in an N>=2
    source aggregator MUST be fatal — a silent `return []` writes a
    complete-looking lie into the merged aggregate (a schema violation hidden
    behind "0 reason-less"). The single fail-open boundary is the shell
    wrapper's `|| echo WARN`, never inside this aggregator. Mirrors
    defer-drift-check.py / precondition-defer-recheck.py.
    """
    try:
        out = _rt.aspirations_read(source=source, active=True)
    except _rt.RtError as e:
        print(f"[reason-less-blocked-check] {source} read failed: {e.body or e}",
              file=sys.stderr)
        sys.exit(1)  # guard-383: source error fatal
    data = _rt.tolerant_decode_aggregate(
        f"reason-less-blocked-check: {source}", out)
    if data is None:
        return []
    goals = []
    for asp in (data.get("aspirations") if isinstance(data, dict) else data) or []:
        for g in asp.get("goals", []) or []:
            g["_source"] = source
            g["_aspiration_id"] = asp.get("id")
            goals.append(g)
    return goals


def _find_open_audit(all_goals):
    """Return the goal_id of an already-open audit Investigate, else None.

    Scans the SAME goal list produced by _read_goals — no separate query. An
    audit is 'open' when its origin_signal == AUDIT_ORIGIN_SIGNAL and its
    status is pending/in-progress. Resolved/skipped/completed audits do NOT
    block re-filing (the violation may have recurred with new goals).
    """
    for g in all_goals:
        if (g.get("origin_signal") == AUDIT_ORIGIN_SIGNAL
                and g.get("status") in NON_TERMINAL_OPEN):
            return g.get("id")
    return None


def _build_investigate(entries):
    """Build the audit Investigate record listing the reason-less-blocked goals."""
    lines = []
    for e in entries:
        lines.append(
            f"  - {e['goal_id']} [{e['aspiration_id']}] intended="
            f"{e.get('intended_agent')}: {e['title']}")
    listing = "\n".join(lines)
    description = (
        f"{len(entries)} goal(s) are status=blocked with an EMPTY Blocker "
        f"Reference Schema (blocker_ref None AND blocked_by [] AND defer_reason "
        f"None). This violation escapes selection (blocked goals are not "
        f"selected), blocker-recheck (nothing to re-probe), AND quiescence "
        f"reasoning — the goals silently strand (g-115-2595, surfaced by "
        f"g-115-2591; canonical g-115-2198-b stranded ~2 days).\n\n"
        f"Reason-less-blocked goals:\n{listing}\n\n"
        f"RECONCILE each (the g-115-2591 protocol): determine whether a GENUINE "
        f"blocker exists. If yes, reconstruct it into the structured field "
        f"(blocked_by=<dep-goal-id> or blocker_ref=<pq/blocker-id>) so it "
        f"auto-unblocks when the dependency resolves. If the premise is gone or "
        f"never existed, unblock to pending (status=pending). Route lane-owned "
        f"goals to their owning agent's vertical rather than "
        f"appropriating them. Detected by reason-less-blocked-check.py "
        f"(aspirations-precheck Phase 0.5b.11)."
    )
    return {
        "title": (f"Investigate: reconcile {len(entries)} reason-less-blocked "
                  f"goal(s) (empty blocker_ref + blocked_by + defer_reason)"),
        "description": description,
        "priority": "MEDIUM",
        "participants": ["agent"],
        "category": "framework-maintenance",
        "tags": ["reason-less-blocked", "blocker-schema", "learning-health"],
        "origin_signal": AUDIT_ORIGIN_SIGNAL,
    }


def _file_investigate(aspiration_id, entries):
    """File the audit Investigate via the daemon. Returns goal_id or an
    <add-goal-failed:...> marker (never raises — the caller surfaces it).

    On a goal_duplication_blocked refusal, retries ONCE with a justified,
    audited X-Mind-Override-Duplication. _file_investigate is reached ONLY
    when the caller's _find_open_audit found NO open reconcile audit, so the
    gate's block is against COMPLETED prior recurring audits — a structural
    false positive for this inherently-recurring audit (each fresh straggler
    needs its own audit once the prior one closed). Without the retry the
    reason-less safety mechanism can never escalate a straggler once >=1
    reconcile audit has completed. Mirrors ohs-husk-cluster-check.py
    file_goal() (g-335-96) + the automated-filer-gate-interaction tree node.
    (g-115-3067)"""
    record = _build_investigate(entries)
    try:
        result = _rt.aspirations_add_goal(aspiration_id, record, source=ESCALATION_SOURCE)
    except _rt.RtError as e:
        body = (e.body or str(e)).strip()
        if "goal_duplication_blocked" not in body:
            return f"<add-goal-failed:{body or 'no detail'}>"
        # Retry ONCE with a justified override — the sweep's own open-audit
        # dedup already guarantees no OPEN duplicate (audited to
        # world/goal-duplication-overrides.jsonl).
        try:
            result = _rt.aspirations_add_goal(
                aspiration_id, record, source=ESCALATION_SOURCE,
                overrides={"Duplication": (
                    "reason-less sweep: _find_open_audit confirmed no OPEN "
                    "reconcile audit; dup-gate match is COMPLETED prior "
                    "recurring audits (structural FP). g-115-3067")})
        except _rt.RtError as e2:
            return f"<add-goal-failed:{(e2.body or str(e2)).strip() or 'no detail'}>"
    if isinstance(result, dict):
        return result.get("goal_id") or result.get("id") or "<unknown-id>"
    return "<unknown-id>"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=("Flag status=blocked goals with an empty Blocker Reference "
                     "Schema (no blocker_ref, no blocked_by, no defer_reason). "
                     "Detective by default; --apply files ONE deduplicated "
                     "reconcile Investigate. Reference: g-115-2595."))
    ap.add_argument("--apply", action="store_true",
                    help=("File a deduplicated reconcile Investigate when "
                          "reason-less-blocked goals are found and no open audit "
                          "already exists. Default: dry-run (report only)."))
    ap.add_argument("--investigate-aspiration", default=ESCALATION_ASP,
                    help=("Aspiration ID the audit Investigate is filed under. "
                          f"Default {ESCALATION_ASP} (framework hygiene), resolved "
                          "per deployment. Must exist in the resolved queue."))
    ap.add_argument("--output", choices=["json", "human"], default="json")
    args = ap.parse_args(argv)

    now = dt.datetime.now()
    all_goals = _read_goals("world") + _read_goals("agent")

    reason_less = []
    for g in all_goals:
        if not _is_reason_less_blocked(g):
            continue
        reason_less.append({
            "goal_id": g.get("id"),
            "source": g.get("_source"),
            "aspiration_id": g.get("_aspiration_id"),
            "intended_agent": g.get("intended_agent"),
            "blocked_since": g.get("blocked_since"),
            "title": (g.get("title") or "")[:80],
        })

    # Deterministic order (goal_id) so the report + Investigate listing are stable.
    reason_less.sort(key=lambda e: (e.get("goal_id") or ""))

    open_audit_goal_id = _find_open_audit(all_goals)

    result = {
        "scanned": len(all_goals),
        "reason_less_count": len(reason_less),
        "reason_less": reason_less,
        "open_audit_exists": open_audit_goal_id is not None,
        "open_audit_goal_id": open_audit_goal_id,
        "investigate_filed": None,
        "actions_taken": "apply" if args.apply else "dry-run",
        "now": now.isoformat(timespec="seconds"),
    }

    if args.apply and reason_less and open_audit_goal_id is None:
        filed = _file_investigate(args.investigate_aspiration, reason_less)
        if str(filed).startswith("<add-goal-failed"):
            # Surface the failure loudly; the sweep re-detects next iteration
            # (guard-487 fail-closed: a missed filing re-detects, so it is safe
            # to leave unfiled — never file a possible duplicate on uncertainty).
            print(f"[reason-less-blocked-check] Investigate filing failed: "
                  f"{filed}", file=sys.stderr)
            result["investigate_filed"] = None
            result["investigate_error"] = filed
        else:
            result["investigate_filed"] = filed

    if args.output == "human":
        print(f"scanned={result['scanned']} "
              f"reason_less_count={result['reason_less_count']} "
              f"open_audit={result['open_audit_goal_id']} "
              f"investigate_filed={result['investigate_filed']}")
        for e in reason_less:
            print(f"  [reason-less] {e['goal_id']} ({e['source']}) "
                  f"[{e['aspiration_id']}] intended={e.get('intended_agent')}: "
                  f"{e['title']}")
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
