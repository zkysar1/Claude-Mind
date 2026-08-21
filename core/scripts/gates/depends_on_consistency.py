"""depends_on/blocked_by consistency gate — daemon-safe shared extraction ().

`depends_on` and `blocked_by` are NOT synonyms, and the difference is the whole
point of this gate. `blocked_by` is the SEQUENCING field: goal-selector.py reads
it (and only it — zero occurrences of `depends_on` in that file) to suppress a
goal whose prerequisite is unmet. `depends_on` is the OUTPUT-PASSING annotation
described in goal-schemas.md "Output-Passing Dependencies": a list of
`{goal_id, expects}` dicts that `dependent-unblock.py` uses to prepend a
predecessor's factual output into the dependent goal's description.

goal-schemas.md:636 states the invariant that keeps the two coherent:

    Each `depends_on.goal_id` MUST also appear in `blocked_by`

A goal that violates it carries an output-passing annotation with no sequencing
behind it — it LOOKS sequenced and is not, and nothing warns. Measured
2026-08-20 over 2771 live goal records: 6 carried a non-empty `depends_on`, and
exactly ONE conformed. Four used bare id strings instead of `{goal_id, expects}`
dicts (so they could not serve the output-passing purpose either), and five had
no matching `blocked_by` entry at all.

WHY THE INVARIANT WAS UNENFORCED. aspirations.py::validate_goal has carried this
exact check since the field was introduced, but under no-python-cli-fallback
(2026-05-14) the live write path is the daemon, and the daemon's `_validate_goal`
is a deliberate SUBSET — id format, status enum, recurring, interval_hours. The
CLI check was therefore orphaned from every real write. This is the same
orphaning that produced the prose-verification false-negatives (g-115-1440); its
remedy is guard-547 — extract to a shared gates/ module imported by both paths
rather than duplicating. That is what this module is.

WHY NOT MAKE THE SELECTOR HONOUR `depends_on` INSTEAD. Measured and refuted: the
selector's predicate is `[bid for bid in _ensure_list(goal.get("blocked_by"))
if bid not in done_ids]` and `done_ids` is a SET (goal-selector.py:2127). A
`{goal_id, expects}` dict is unhashable, so a union of the two fields raises
`TypeError: unhashable type: 'dict'` on the first dict-shaped carrier it scores
— crashing the fleet's mandatory selection entry point rather than suppressing
anything. The conceptual objection is the same shape: the selector reads only
`blocked_by` BY DESIGN, because that is the sequencing field.

Public API:
    evaluate(goal, *, meta_dir=None, agent_name=None) -> dict

Return shape:
    {
      "would_block": bool,          # True on a genuine consistency violation
      "decision": str,              # "noop" | "pass" | "block"
      "violations": list[str],      # machine-readable reason tokens
      "message": str | None,        # populated only when would_block is True
    }

The CALLER decides the side effect: the CLI raises ValueError(message); a daemon
endpoint returns Response.error(400, ...). evaluate() NEVER raises for control
flow. Telemetry is emitted via _gate_log.log and is fail-open — a logging error
never changes the verdict.

WIRE AT THE ADD SITES ONLY, never inside `_validate_goal`. Five live records
already violate this invariant; `update_goal` validates its in-lock candidate
through `_validate_goal`, so a check placed there would retroactively wedge every
status change — claim, in-progress, complete — on goals that are running fine
today. That reasoning is not new here: `_assert_no_invalid_checks` carries it
verbatim for the same reason. A filing-time gate that freezes existing work is
worse than the drift it prevents.

Daemon safety: reads no env directly; meta_dir / agent_name are explicit so the
firing record lands in the CALLING agent's gate-firings.jsonl (the module-level
_gate_log.META_DIR is frozen at daemon startup).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# gate_id MUST match core/config/gates.yaml id.
GATE_ID = "depends-on-consistency"


def evaluate(goal: Dict[str, Any], *, meta_dir=None,
             agent_name: Optional[str] = None) -> Dict[str, Any]:
    """Detect depends_on/blocked_by inconsistency. See module docstring."""
    gid = goal.get("id", "<unassigned>")

    def _log(decision: str, *, trigger_matched=None, payload=None, extra=None):
        # Fail-open: telemetry must never alter the verdict or raise.
        try:
            import _gate_log
            _gate_log.log(
                GATE_ID, decision,
                caller=f"gates.depends_on_consistency.evaluate goal={gid}",
                trigger_matched=trigger_matched, payload=payload, extra=extra,
                meta_dir=meta_dir, agent_name=agent_name,
            )
        except Exception:
            pass

    def _verdict(decision: str, violations: List[str], message: Optional[str]):
        return {
            "would_block": decision == "block",
            "decision": decision,
            "violations": violations,
            "message": message,
        }

    if "depends_on" not in goal:
        _log("noop")
        return _verdict("noop", [], None)

    deps = goal.get("depends_on")
    # An explicitly-empty list is the documented way to carry no output-passing
    # dependency. It is not a violation and must not be treated as one.
    if deps is None or (isinstance(deps, list) and not deps):
        _log("noop", extra={"reason": "depends_on empty"})
        return _verdict("noop", [], None)

    if not isinstance(deps, list):
        violations = ["not_a_list"]
        _log("block", trigger_matched="not_a_list",
             payload=repr(deps)[:200], extra={"would_block": True})
        return _verdict("block", violations, _message(gid, violations, [], []))

    # blocked_by tolerates the legacy bare-string shape the same way
    # aspirations_write.py's cleanup path does — a string is wrapped, not
    # iterated character-by-character (which would make every membership test
    # succeed on a single-character id and silently pass the gate).
    raw_blocked = goal.get("blocked_by") or []
    blocked_by = set(raw_blocked if isinstance(raw_blocked, list) else [raw_blocked])

    violations: List[str] = []
    wrong_shape: List[str] = []
    missing: List[str] = []
    for dep in deps:
        if not isinstance(dep, dict) or "goal_id" not in dep:
            wrong_shape.append(repr(dep)[:60])
            continue
        if dep["goal_id"] not in blocked_by:
            missing.append(dep["goal_id"])

    if wrong_shape:
        violations.append("wrong_shape")
    if missing:
        violations.append("not_in_blocked_by")

    if not violations:
        _log("pass", extra={"deps_count": len(deps)})
        return _verdict("pass", [], None)

    _log("block", trigger_matched=",".join(violations),
         payload=repr(deps)[:500],
         extra={"would_block": True, "wrong_shape": len(wrong_shape),
                "missing": len(missing)})
    return _verdict("block", violations, _message(gid, violations, wrong_shape, missing))


def _message(gid: str, violations: List[str], wrong_shape: List[str],
             missing: List[str]) -> str:
    """Build the refusal text.

    The message is the gate's real product. A guardrail describing this trap
    already exists (guard-4554) but is retrieval-dependent — it reaches a filer
    only if they happen to query for it. This text arrives at the moment of the
    mistake, so it must state the distinction, not just the violation.
    """
    parts = [f"Goal {gid}: depends_on/blocked_by inconsistency ({', '.join(violations)})."]
    if "not_a_list" in violations:
        parts.append("depends_on must be a list.")
    if wrong_shape:
        parts.append(
            f"These entries are not {{goal_id, expects}} objects: {wrong_shape}. "
            f"A bare goal-id string is the usual sign that depends_on was reached "
            f"for as a sequencing field."
        )
    if missing:
        parts.append(
            f"These depends_on goal_ids are absent from blocked_by: {missing}."
        )
    parts.append(
        "The two fields do different jobs. blocked_by is what SEQUENCES a goal — "
        "goal-selector.py reads it and only it, so a prerequisite named nowhere "
        "else does not suppress anything. depends_on is the output-passing "
        "annotation dependent-unblock.py uses to prepend a predecessor's factual "
        "output into this goal's description. goal-schemas.md 'Output-Passing "
        "Dependencies' requires every depends_on.goal_id to appear in blocked_by "
        "as well. Fix: set blocked_by to the prerequisite ids, and write "
        "depends_on as [{goal_id: <id>, expects: <what output you need>}] — or "
        "drop depends_on entirely if you only need sequencing."
    )
    return " ".join(parts)
