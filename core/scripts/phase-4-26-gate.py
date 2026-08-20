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

WHERE IT FIRES, measured rather than assumed (g-115-3148, 2026-08-01).
This gate runs inside iteration-close.sh `do_state_update`, which is a
LATER phase invocation than `do_verify` — and `do_verify` is what writes
`status=completed` (iteration-close.sh:552-562, via aspirations-update-goal.sh
or aspirations-complete-by.sh for recurring). So the gate does NOT prevent
the completion write, and the docstring said it did until this was checked
against the call site. What it actually does is halt do_state_update (exit 1
at iteration-close.sh:912-916), stopping every downstream obligation of the
close — meta bookkeeping, the WM session-completion append, tree-encoding
bookkeeping, the Phase-6 spark sentinel, iteration-commit. That is real
pressure and the iteration cannot proceed past it, but it is post-completion
pressure. Read the title above as "refuse to CLOSE OUT", not "refuse to mark".

It reads the persisted retrieval-session.json (the writer's own audit
trail) and refuses to proceed when the signal is structurally weak. The
agent has one of three escape paths:

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

from _paths import WORLD_DIR, AGENT_DIR, retrieval_session_path  # noqa: E402
from _gate_log import log as _gate_log  # noqa: E402

GATE_ID = "phase-4-26-gate"  # MUST match the id in core/config/gates.yaml

OVERRIDE_LEDGER = WORLD_DIR / "phase-4-26-overrides.jsonl"

# Which _gate_log decision each branch reports. Split by whether the gate had
# anything to grade: a session with no retrieval population is `noop` (invoked,
# nothing triggered), not `pass` — the retirement evaluator scores
# count(decision != "noop"), so counting vacuous passes as firings would make
# an inert gate look busy. That is the exact failure this gate has already had
# twice ( inert predicate,  saturated predicate), so the
# telemetry must not be able to hide a third.
_DECISION_FOR_PATH = {
    "no-session":            "noop",
    "stale-session":         "noop",
    "retrieval-performed-false": "noop",
    "empty-population":      "noop",
    "no-method-legacy":      "pass",
    "infer-helpful":         "pass",
    "explicit-classification": "pass",
    "unknown-method":        "pass",
    "pending-true":          "block",
    "all-noise":             "block",
    "all-unknown":           "block",
    "infer-zero-helpful":    "block",
    "read-error":            "fail_open",
}


def _load_session(goal_id):
    """Read retrieval-session.json for the agent. Returns (session_dict, err).
    Returns ({}, "no-session") when the file is missing — common for goals
    that did not invoke retrieval, treated as fail-open by the caller.
    """
    if AGENT_DIR is None:
        return {}, "no-agent-dir"
    # Body-aware (). Composing the agent-wide path by hand made this
    # gate return verdict=pass / "empty retrieval population" on EVERY
    # worker-executed goal, while the real manifest sat unread in
    # sessions/<sid>/. A fail-open gate is the dangerous half of that defect:
    # a lost counter shows up as a zero, a passing gate shows up as nothing.
    path = retrieval_session_path(AGENT_DIR)
    if not path.exists():
        return {}, "no-session"
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d, None
    except (OSError, json.JSONDecodeError) as e:
        return {}, "read-error: " + str(e)


def _evaluate(session, goal_id):
    """Return (verdict, reason, method, helpful_count, decision_path).

    verdict in {"pass", "block"}. `decision_path` names WHICH branch produced
    the verdict — one unique label per return, per guard-502, so a firing in
    meta/gate-firings.jsonl distinguishes "this branch ran" from "the gate was
    never invoked". The label is also what makes a silent-path regression
    visible: an inert predicate shows up as 100% one label rather than as an
    absence nobody can see.
    """
    sess_goal = session.get("goal_id") or ""
    if sess_goal and sess_goal != goal_id:
        return ("pass", "stale session (different goal_id) — fail-open",
                None, 0, "stale-session")
    # `retrieval_performed` is written ONLY by iteration-close.sh's no-retrieval
    # STUB, always as an explicit False; the real retrieve.sh path records
    # goal_id + counts and leaves the key ABSENT. So `not session.get(...)` —
    # the obvious check — reads every REAL retrieval as "no signal" and returns a
    # vacuous pass, which is why this gate was 100% inert since it shipped
    # (). Only the explicit-False stub means "nothing was retrieved".
    # Contract established empirically in  (4 experiments over live
    # session files); `pre-apply-consult-gate.py:203` is the reference
    # implementation of the same predicate.
    if session.get("retrieval_performed") is False:
        return ("pass", "retrieval_performed=false (no-retrieval stub — nothing to gate)",
                None, 0, "retrieval-performed-false")
    pop = len(session.get("tree_nodes_loaded") or []) + \
          len(session.get("supplementary_items") or [])
    if pop == 0:
        return ("pass", "empty retrieval population", None, 0,
                "empty-population")
    method = session.get("utilization_method")
    if method is None:
        if session.get("utilization_pending"):
            return ("block",
                    "utilization_pending=true — Phase 4.26 did not run",
                    None, 0, "pending-true")
        return ("pass",
                "no utilization_method but pending=false (legacy session)",
                None, 0, "no-method-legacy")
    if method == "all_noise":
        return ("block",
                "method=all_noise — backstop fired alone, no positive signal",
                method, 0, "all-noise")
    if method == "all_unknown":
        return ("block",
                "method=all_unknown — backstop fired alone, no positive signal",
                method, 0, "all-unknown")
    if method == "infer":
        helpful = ((session.get("inference_stats") or {}).get("helpful", 0))
        if helpful == 0:
            return ("block",
                    "method=infer with helpful=0 — no positive signal",
                    method, 0, "infer-zero-helpful")
        return ("pass", "method=infer with helpful=" + str(helpful),
                method, helpful, "infer-helpful")
    if method in ("manual", "all_helpful"):
        return ("pass", "method=" + method + " (explicit LLM classification)",
                method, -1, "explicit-classification")
    return ("pass", "unknown method=" + str(method) + " — fail-open",
            method, 0, "unknown-method")


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


def _emit_firing(path, goal_id, method, helpful, reason, override):
    """One firing record per invocation, labelled with the branch that decided.

    Deliberately ONE call site rather than a `_gate_log` beside each `return`:
    guard-502's requirement is that every branch be DISTINGUISHABLE in the
    ledger, and carrying `decision_path` out of `_evaluate` achieves that while
    keeping `_evaluate` a pure function the tests can exercise without touching
    telemetry. `log()` never raises (see _gate_log docstring), so this cannot
    break the gate.

    An override is reported as decision=`override`, NOT as the underlying
    block — that is what makes the FP ratio count(override)/(block+override)
    computable. Until now the ONLY trace of this gate's behaviour was
    world/phase-4-26-overrides.jsonl, which records overridden blocks and
    nothing else: blocks that STOOD and every pass were invisible, so the
    gate's own pass/block split was unmeasurable (g-115-3148).
    """
    decision = _DECISION_FOR_PATH.get(path, "fail_open")
    _gate_log(
        GATE_ID,
        "override" if override is not None else decision,
        caller="iteration-close.sh:do_state_update",
        trigger_matched=method,
        payload=goal_id,
        override_reason=override,
        extra={
            "decision_path": path,
            "would_block": decision == "block",
            "helpful_count": helpful,
            "reason": reason,
        },
    )


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
        verdict, reason, method, helpful, path = (
            "pass", "no retrieval-session.json — fail-open", None, 0,
            "no-session")
    elif load_err and load_err.startswith("read-error"):
        print("phase-4-26-gate: " + load_err, file=sys.stderr)
        _emit_firing("read-error", goal_id, None, 0, load_err, override=None)
        return 2
    else:
        verdict, reason, method, helpful, path = _evaluate(session, goal_id)

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
        _emit_firing(path, goal_id, method, helpful, reason,
                     override=override_reason)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    _emit_firing(path, goal_id, method, helpful, reason, override=None)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 1 if payload["verdict"] == "block" else 0


if __name__ == "__main__":
    sys.exit(main())
