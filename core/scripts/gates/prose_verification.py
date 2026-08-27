"""Prose-verification-drift gate — daemon-safe shared extraction ().

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
    PROSE_VERIFICATION_MARKERS          -- the original two headers (checks required)
    PROSE_ACCEPTANCE_SYNONYM_MARKERS    -- acceptance/success synonyms (g-306-358;
                                           satisfied by outcomes OR checks)
    ALL_PROSE_MARKERS                   -- the union, for callers that only need
                                           "does this description advertise criteria"

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

import re
from typing import Any, Dict, List, Optional

# gate_id MUST match core/config/gates.yaml id.
GATE_ID = "prose-verification-drift"

# The ORIGINAL two markers. Their satisfaction rule is UNCHANGED (a non-empty
# verification.checks is required) because goal-schemas.md promises backward
# compatibility and these are the headers the /verify-learning convention emits.
PROSE_VERIFICATION_MARKERS = ("Verification outcomes:", "Verification checks:")

# Acceptance/success SYNONYM headers (). A goal that advertises its
# criteria under one of these and leaves structured verification empty slipped
# the gate as a no-op — the predicate-narrower-than-population class
# (guard-1802 / rb-5650).
#
# THE SET IS MEASURED, NOT GUESSED. Counted over the live 2,990-goal corpus
# (2026-08-25, alpha worker cc-08): "Acceptance:" 3 hits, "Done when:" 2,
# "Exit criteria:" 1 — every one a genuine criteria header. The remaining
# entries hit ZERO today and are included as the canonical long forms of the
# same headers, which costs nothing precisely because nothing matches them yet.
PROSE_ACCEPTANCE_SYNONYM_MARKERS = (
    "Acceptance criteria:",
    "Acceptance:",
    "Success criteria:",
    "Done when:",
    "Definition of done:",
    "Completion criteria:",
    "Exit criteria:",
)

# DELIBERATELY NOT A MARKER: bare "Success:". It was a candidate and it was
# MEASURED as a false positive — 's description quotes a load-test log
# line, `Total: 120, Success: 120 (200), Rate limited: 0`, which is not a header
# at all. Bare high-frequency English words do not survive contact with pasted
# tool output. Do not "complete" this list by adding it back; the long form
# "Success criteria:" above already covers the real header.

ALL_PROSE_MARKERS = PROSE_VERIFICATION_MARKERS + PROSE_ACCEPTANCE_SYNONYM_MARKERS

# Code regions are stripped before matching so a description that QUOTES a
# marker (documenting this gate, showing a template, pasting tool output) does
# not self-trip — guard-1668, rb-349/guard-319 prose-filter-pattern.
# An UNCLOSED fence matches nothing and therefore strips nothing, which leaves
# the marker visible and the gate LOUD. That is the correct failure direction:
# a missed strip costs a false block someone sees immediately, while an
# over-eager strip costs a silent miss (guard-3351 — narrowing is the quiet
# direction).
_FENCED_CODE = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`[^`\n]*`")


def _strip_code_regions(text: str) -> str:
    """Blank out fenced blocks and inline code spans, preserving offsets loosely."""
    return _INLINE_CODE.sub(" ", _FENCED_CODE.sub(" ", text))


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

    prose = _strip_code_regions(desc)

    original_seen = [m for m in PROSE_VERIFICATION_MARKERS if m in prose]
    synonym_seen = [m for m in PROSE_ACCEPTANCE_SYNONYM_MARKERS if m in prose]
    if not original_seen and not synonym_seen:
        _log("noop")
        return _verdict("noop", [], None)

    markers_seen = original_seen + synonym_seen
    verification = goal.get("verification") or {}
    if isinstance(verification, dict):
        checks = verification.get("checks")
        outcomes = verification.get("outcomes")
    else:
        checks = outcomes = None
    has_checks = isinstance(checks, list) and len(checks) > 0
    has_outcomes = isinstance(outcomes, list) and len(outcomes) > 0

    # SATISFACTION SEMANTICS DIFFER BY MARKER CLASS, and the split is measured.
    # An ORIGINAL marker still demands checks — that is the pre-existing contract
    # and narrowing it would be a silent behaviour change.
    # A SYNONYM-ONLY goal is satisfied by outcomes OR checks: of the 7 goals the
    # synonym set newly admits, THREE (, , ) carry
    # populated outcomes and no checks. Demanding checks there would block real
    # backlog goals that legitimately have human-verified outcomes and no machine
    # check yet — over-blocking a correct goal to catch a drifted one.
    # Mixed case resolves to the STRICTER rule: any original marker means checks.
    if original_seen:
        satisfied = has_checks
        requirement = "verification.checks"
    else:
        satisfied = has_checks or has_outcomes
        requirement = "verification.checks or verification.outcomes"

    if satisfied:
        _log("pass", trigger_matched=",".join(markers_seen),
             extra={"checks_count": len(checks) if has_checks else 0,
                    "outcomes_count": len(outcomes) if has_outcomes else 0,
                    "synonym_only": not original_seen})
        return _verdict("pass", markers_seen, None)

    _log("block", trigger_matched=",".join(markers_seen),
         payload=desc[:500], extra={"would_block": True,
                                    "synonym_only": not original_seen})
    message = (
        f"Goal {gid}: prose-only verification drift detected. "
        f"Description contains {markers_seen} but {requirement} is absent or empty. "
        f"Fix: either (a) move the prose bullets into a structured verification "
        f"{{outcomes:[...], checks:[...]}} object, (b) remove the acceptance-criteria "
        f"prose header from the description, or (c) if you are QUOTING the header "
        f"rather than declaring criteria -- documenting this gate, showing a "
        f"template -- wrap it in backticks or a fenced block, which this gate "
        f"strips before matching. Prose-only "
        f"verification silently bypasses /verify-learning S49.7 gates."
    )
    return _verdict("block", markers_seen, message)
