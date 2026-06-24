"""Prose-verification-drift gate — daemon-safe shared extraction (4).

Goal descriptions that advertise "Verification outcomes:" / "Verification
checks:" prose headers WITHOUT a corresponding structured verification.checks
entry silently slip past /verify-learning S49.7 post-write. This gate catches
them pre-write so the drift can't enter the file in the first place.

Extracted from aspirations.py::_check_prose_verification_drift. The g-115-1440
investigation found the CLI had this check but the daemon _validate_goal subset
omitted it entirely — every daemon-path goal add/edit bypassed the gate, a
realized false-negative (g-315-119 completed, g-316-08 pending, both prose-only
goals that slipped through). Per guard-547, validation logic that must hold on
BOTH the CLI and daemon write paths lives in a shared gates/ module imported by
both, not duplicated — duplication is exactly the drift that produced this gap.

Public API:
    evaluate(goal, *, meta_dir=None, agent_name=None) -> dict

Return shape:
    {
      "would_block": bool,        # True only on genuine prose-only drift
      "decision": str,            # "noop" | "pass" | "block"
      "markers_seen": list[str],  # which markers triggered the check
      "message": str | None,      # populated only when would_block is True
    }

The CALLER decides the side effect: the CLI raises ValueError(message); a daemon
endpoint returns Response.error(400, ...). evaluate() NEVER raises for control
flow. Telemetry is emitted via _gate_log.log and is fail-open — a logging error
never changes the verdict.

Daemon safety: reads no env directly; meta_dir / agent_name are explicit so the
firing record lands in the CALLING agent's gate-firings.jsonl (the module-level
_gate_log.META_DIR is frozen at daemon startup — see aspirations_write.py
_gate_log_layer_d for the same pattern).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# gate_id MUST match core/config/gates.yaml id.
GATE_ID = "prose-verification-drift"
PROSE_VERIFICATION_MARKERS = ("Verification outcomes:", "Verification checks:")


def evaluate(goal: Dict[str, Any], *, meta_dir=None,
             agent_name: Optional[str] = None) -> Dict[str, Any]:
    """Detect prose-only verification drift. See module docstring."""
    gid = goal.get("id", "<unassigned>")

    def _log(decision: str, *, trigger_matched=None, payload=None, extra=None):
        # Fail-open: telemetry must never alter the verdict or raise.
        try:
            import _gate_log
            _gate_log.log(
                GATE_ID, decision,
                caller=f"gates.prose_verification.evaluate goal={gid}",
                trigger_matched=trigger_matched, payload=payload, extra=extra,
                meta_dir=meta_dir, agent_name=agent_name,
            )
        except Exception:
            pass

    def _verdict(decision: str, markers: List[str], message: Optional[str]):
        return {
            "would_block": decision == "block",
            "decision": decision,
            "markers_seen": markers,
            "message": message,
        }

    desc = goal.get("description") or ""
    if not isinstance(desc, str):
        _log("noop", extra={"reason": "description not a string"})
        return _verdict("noop", [], None)

    if not any(marker in desc for marker in PROSE_VERIFICATION_MARKERS):
        _log("noop")
        return _verdict("noop", [], None)

    markers_seen = [m for m in PROSE_VERIFICATION_MARKERS if m in desc]
    verification = goal.get("verification") or {}
    checks = verification.get("checks") if isinstance(verification, dict) else None
    if isinstance(checks, list) and len(checks) > 0:
        _log("pass", trigger_matched=",".join(markers_seen),
             extra={"checks_count": len(checks)})
        return _verdict("pass", markers_seen, None)

    _log("block", trigger_matched=",".join(markers_seen),
         payload=desc[:500], extra={"would_block": True})
    message = (
        f"Goal {gid}: prose-only verification drift detected. "
        f"Description contains {markers_seen} but verification.checks is absent or empty. "
        f"Fix: either (a) move the prose bullets into a structured verification "
        f"{{outcomes:[...], checks:[...]}} object, or (b) remove the 'Verification "
        f"outcomes:/checks:' prose headers from the description. Prose-only "
        f"verification silently bypasses /verify-learning S49.7 gates."
    )
    return _verdict("block", markers_seen, message)
