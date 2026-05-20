#!/usr/bin/env python3
"""phase-4-26-gate — refuse to mark goal completed if Phase 4.26 produced no positive signal.

Writer-layer enforcement for guard-415 / rb-472. Phase 4.26 of
aspirations-state-update is the explicit-feedback step that increments
times_helpful / times_inferred_helpful / times_noise on retrieved items
(tree nodes, reasoning-bank entries, guardrails). Under context pressure
the LLM abbreviates or skips this step. utilization-gate.sh (PreToolUse
hook) backstops by auto-running utilization-feedback.sh --all-noise when
Phase 4.26 is skipped — but that path produces ONLY times_noise writes,
so times_helpful and times_inferred_helpful stay at zero across the
population. The result is a starved positive-signal stream:
times_helpful counted on only 20 of 411 active reasoning-bank entries
nearly triggered Plan A retire-the-dead-entries (rb-472).

This gate fires inside iteration-close.sh do_state_update BEFORE the
goal record is updated to status=completed. It reads the persisted
retrieval-session.json (the writer's own audit trail) and refuses to
proceed when the signal is structurally weak. The agent has one of
three escape paths:

  1. Run Phase 4.26 manually with explicit --helpful items, OR
  2. Run --infer and have it classify at least one item helpful, OR
  3. Pass --no-retrieval-applicable "<reason>" to iteration-close.sh,
     which logs the override to world/phase-4-26-overrides.jsonl
     and lets the close proceed.

Verdict matrix (utilization_method field on retrieval-session.json):
  retrieval_performed=false           → pass (no signal to gate)
  empty retrieval population           → pass (nothing was retrieved)
  utilization_method=manual            → pass (LLM classified explicitly)
  utilization_method=all_helpful       → pass (LLM marked all helpful)
  utilization_method=infer, helpful>0  → pass (automated positive signal)
  utilization_method=infer, helpful=0  → block (zero positive signal)
  utilization_method=all_noise         → block (legacy backstop fired alone)
  utilization_method=all_unknown       → block (preferred backstop fired alone —
                                            no times_noise poisoning, but still
                                            no positive signal so the LLM should
                                            either run --infer/Phase 4.26 manually
                                            or pass --no-retrieval-applicable)
  utilization_method missing,
    utilization_pending=true           → block (Phase 4.26 didn't run)
  utilization_method missing,
    utilization_pending=false          → pass (legacy session, fail-open)

When --no-retrieval-applicable is set, the gate passes regardless of
verdict and writes one entry to the override ledger:
  {"timestamp", "goal_id", "agent", "method", "reason"}

Usage:
  bash phase-4-26-gate.sh --goal <goal-id>
  bash phase-4-26-gate.sh --goal <goal-id> --no-retrieval-applicable "<reason>"

Exit codes:
  0 pass (gate green or override logged)
  1 block (verdict says signal too weak; reason on stderr + JSON stdout)
  2 usage error (missing --goal, malformed retrieval-session.json)

Stdout: JSON {goal_id, verdict, reason, method, helpful_count, override}.
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _paths import WORLD_DIR, AGENT_DIR  # noqa: E402

OVERRIDE_LEDGER = WORLD_DIR / "phase-4-26-overrides.jsonl"


def _load_session(goal_id):
    """Read retrieval-session.json for the agent. Returns (session_dict, err).
    Returns ({}, "no-session") when the file is missing — common for goals
    that did not invoke retrieval, treated as fail-open by the caller.
    """
    if AGENT_DIR is None:
        return {}, "no-agent-dir"
    path = AGENT_DIR / "session" / "retrieval-session.json"
    if not path.exists():
        return {}, "no-session"
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d, None
    except (OSError, json.JSONDecodeError) as e:
        return {}, "read-error: " + str(e)


def _evaluate(session, goal_id):
    """Return (verdict, reason, method, helpful_count).
    verdict in {"pass", "block"}.
    """
    sess_goal = session.get("goal_id") or ""
    if sess_goal and sess_goal != goal_id:
        return ("pass", "stale session (different goal_id) — fail-open",
                None, 0)
    if not session.get("retrieval_performed"):
        return ("pass", "retrieval_performed=false (no signal to gate)",
                None, 0)
    pop = len(session.get("tree_nodes_loaded") or []) + \
          len(session.get("supplementary_items") or [])
    if pop == 0:
        return ("pass", "empty retrieval population", None, 0)
    method = session.get("utilization_method")
    if method is None:
        if session.get("utilization_pending"):
            return ("block",
                    "utilization_pending=true — Phase 4.26 did not run",
                    None, 0)
        return ("pass",
                "no utilization_method but pending=false (legacy session)",
                None, 0)
    if method == "all_noise":
        return ("block",
                "method=all_noise — backstop fired alone, no positive signal",
                method, 0)
    if method == "all_unknown":
        return ("block",
                "method=all_unknown — backstop fired alone, no positive signal",
                method, 0)
    if method == "infer":
        helpful = ((session.get("inference_stats") or {}).get("helpful", 0))
        if helpful == 0:
            return ("block",
                    "method=infer with helpful=0 — no positive signal",
                    method, 0)
        return ("pass", "method=infer with helpful=" + str(helpful),
                method, helpful)
    if method in ("manual", "all_helpful"):
        return ("pass", "method=" + method + " (explicit LLM classification)",
                method, -1)
    return ("pass", "unknown method=" + str(method) + " — fail-open",
            method, 0)


def _log_override(goal_id, method, reason):
    """Append one JSONL row to the override ledger. Best-effort; silent
    on IO error so gate-pass-with-override does not regress on a
    write failure."""
    payload = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "goal_id": goal_id,
        "agent": os.environ.get("MIND_AGENT", "unknown"),
        "method": method,
        "reason": reason,
    }
    try:
        OVERRIDE_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with open(OVERRIDE_LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError as e:
        print("phase-4-26-gate: override ledger write failed: " + str(e),
              file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--goal", required=True,
                    help="goal_id of the just-completing goal")
    ap.add_argument("--no-retrieval-applicable", default=None,
                    help='Override: documented reason that no retrieval '
                         'feedback applies for this goal '
                         '(e.g., "purely-mechanical infrastructure goal").')
    args = ap.parse_args(argv)

    goal_id = args.goal
    override_reason = args.no_retrieval_applicable

    session, load_err = _load_session(goal_id)
    if load_err == "no-session":
        verdict, reason, method, helpful = (
            "pass", "no retrieval-session.json — fail-open", None, 0)
    elif load_err and load_err.startswith("read-error"):
        print("phase-4-26-gate: " + load_err, file=sys.stderr)
        return 2
    else:
        verdict, reason, method, helpful = _evaluate(session, goal_id)

    payload = {
        "goal_id": goal_id,
        "verdict": verdict,
        "reason": reason,
        "method": method,
        "helpful_count": helpful,
        "override": False,
    }

    if verdict == "block" and override_reason is not None:
        _log_override(goal_id, method or "none", override_reason)
        payload["verdict"] = "pass"
        payload["override"] = True
        payload["override_reason"] = override_reason
        payload["original_block_reason"] = reason
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 1 if payload["verdict"] == "block" else 0


if __name__ == "__main__":
    sys.exit(main())
