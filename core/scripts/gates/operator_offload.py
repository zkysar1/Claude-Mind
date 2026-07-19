"""Operator-offload gate — the Layer-B backstop for gh-005.

Fires ONLY when a goal being filed is RECURRING (``recurring: true`` or a
present ``interval_hours``). A recurring goal is a standing claim on LLM
iterations at its stated frequency, so the pipeline requires evidence that
the operator-offload test (meta/aspiration-generation-strategy.yaml gh-005)
was run: deterministic + clocked + checkable work belongs on the
Ayoai-Operator as a scheduled job with a low-frequency Mind PULL goal, not
on the LLM loop.

The test is satisfied by an ``offload_decision`` field on the goal body —
a short free-text justification of why the goal stays on the LLM loop (or
names the operator job it pulls from). Examples:

    "stays-mind: judgement/retrieval-heavy (tree curation)"
    "stays-mind: mind-box-local state (reads own transcripts)"
    "stays-mind: web research"
    "operator-pull: reads InboxWatch audit rows, escalates on NEW"

Non-recurring goals never fire this gate. gh-005 covers the generation
flow; this gate covers the direct-file path (aspirations-add-goal.sh and
aspiration-creation with embedded goals) so the ramp is enforced at every
goal-creation chokepoint, not just the generated one.

Public API:
    evaluate(goal: dict, *, override_offload=None,
             meta_dir=None, agent_name=None) -> dict

Return shape (mirrors sibling blocking gates):
    {"would_block": bool, "fired": bool, "reason": str,
     "offload_decision": str|None, "override": str|None}

Daemon safety: pure decision function; the only I/O is best-effort
telemetry via _gate_log (never raises). No env reads, no file reads.
"""
from __future__ import annotations

from typing import Optional

from _gate_log import log as _gate_log  # type: ignore

GATE_ID = "operator-offload-gate"

_BLOCK_MESSAGE = (
    "Recurring goal filed without an operator-offload decision (gh-005). "
    "A recurring goal burns a full LLM iteration every cycle; if the work is "
    "(1) deterministic, (2) clocked, and (3) checkable, it belongs on the "
    "Ayoai-Operator as a scheduled job (/build-operator-job: TaskVerticleBase "
    "subclass + TaskRegistry entry + pure-static test) with a low-frequency "
    "PULL goal reading the audit trail — token cost then scales with events, "
    "not frequency (proven: InboxWatch cutover, ~80-120K tokens/day; see "
    "world/conventions/operator-ramp.md + rb-3281). To file this goal, add an "
    "'offload_decision' field stating why it stays on the LLM loop, e.g. "
    "\"stays-mind: judgement/retrieval-heavy (<what>)\", \"stays-mind: "
    "mind-box-local state (<what>)\", \"stays-mind: web research\", or "
    "\"operator-pull: reads <JobName> audit rows\". To bypass with "
    "justification: --override-offload \"<reason>\"."
)


def _is_recurring(goal: dict) -> bool:
    """A goal is recurring if it says so or carries an interval."""
    if not isinstance(goal, dict):
        return False
    if goal.get("recurring") is True:
        return True
    return goal.get("interval_hours") is not None


def evaluate(goal: dict, *, override_offload: Optional[str] = None,
             meta_dir=None, agent_name=None) -> dict:
    """Run the gate. See module docstring for the contract."""
    if not _is_recurring(goal):
        # Not recurring — the gate does not apply. No telemetry: this is
        # the no-op hot path for every ordinary goal add.
        return {"would_block": False, "fired": False,
                "reason": "not-recurring", "offload_decision": None,
                "override": None}

    decision_field = goal.get("offload_decision") if isinstance(goal, dict) else None
    has_decision = isinstance(decision_field, str) and decision_field.strip() != ""

    if override_offload:
        _gate_log(GATE_ID, "override",
                  caller="aspirations-add-goal",
                  payload={"goal_title": (goal.get("title") or "")[:120]},
                  override_reason=override_offload,
                  meta_dir=meta_dir, agent_name=agent_name)
        return {"would_block": False, "fired": True,
                "reason": "override", "offload_decision": decision_field,
                "override": override_offload}

    if has_decision:
        _gate_log(GATE_ID, "pass",
                  caller="aspirations-add-goal",
                  payload={"goal_title": (goal.get("title") or "")[:120],
                           "offload_decision": decision_field[:160]},
                  meta_dir=meta_dir, agent_name=agent_name)
        return {"would_block": False, "fired": True,
                "reason": "offload-decision-recorded",
                "offload_decision": decision_field, "override": None}

    _gate_log(GATE_ID, "block",
              caller="aspirations-add-goal",
              payload={"goal_title": (goal.get("title") or "")[:120],
                       "interval_hours": goal.get("interval_hours")},
              meta_dir=meta_dir, agent_name=agent_name)
    return {"would_block": True, "fired": True,
            "reason": _BLOCK_MESSAGE, "offload_decision": None,
            "override": None}
