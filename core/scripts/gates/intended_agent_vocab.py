"""intended_agent vocabulary gate — daemon-safe shared extraction (selection-stack review 2026-08-21).

`intended_agent` is the routing hint: an active agent name, the "either"
sentinel ("no strong signal — defer to selector"), or null. Everything else
names nobody who can honor the routing.

WHY THE INVARIANT WAS UNENFORCED. aspirations.py::validate_goal has carried a
vocabulary check since the field was introduced (g-282-02), but under
no-python-cli-fallback (2026-05-14) the live write path is the daemon, and the
daemon's `_validate_goal` is a deliberate subset — id format, status enum,
recurring, interval_hours. Fourth instance of the guard-547 orphaning already
documented by gates.prose_verification / gates.check_schema /
gates.depends_on_consistency. Measured 2026-08-21: 5 live goals carried
"agent" / "reducer" / "any" — all filed by insight-trigger-sweep.py, which
copied the board tag `requires_action_by:<x>` verbatim into the field.

WHY REFUSE AT WRITE TIME WHEN THE READ SIDE TOLERATES OFF-VOCAB. g-115-3482
made the selector AND the claim path treat an off-vocabulary value as "either"
(fall-through to visible — the safe direction for legacy rows already in the
store). Write-time refusal is still correct, for four reasons:

  1. The stored value LIES to every reader: `intended_agent: "reducer"` reads
     as deliberate routing and silently means "anyone". Censuses, dashboards,
     and the to:<agent> coordination surfaces mis-bucket it.
  2. The fall-through is roster-dependent. routes_away_from's conservative
     branch (unresolvable roster => historical name-mismatch behavior) makes
     an off-vocab goal visible on a healthy box and INVISIBLE on a box whose
     roster read fails — nondeterministic routing per box.
  3. A typo of a real agent name ("bravp" for "bravo") silently converts a
     deliberate single-agent routing into a broadcast — the opposite of the
     author's intent. Refusal at the moment of write is the only point where
     the author is present to fix it (the _goal_fields allowlist rationale).
  4. CLI parity (guard-547): the CLI check refuses; one behavior, both paths.

Writers that convert free-text addressing into goals must NORMALIZE before
filing (off-roster -> "either", provenance kept in tags) — see
insight-trigger-sweep.py::_build_goal_payload. This gate is the backstop for
hand filings, not a substitute for writer-side normalization.

Public API:
    evaluate(goal, *, roster=None, meta_dir=None, agent_name=None) -> dict

Return shape (mirrors gates.depends_on_consistency):
    {
      "would_block": bool,
      "decision": str,          # "noop" | "pass" | "block"
      "violations": list[str],  # machine-readable reason tokens
      "message": str | None,    # populated only when would_block is True
    }

`roster` overrides live-roster resolution (tests / callers that already hold
one). Default resolution is lazy via `_resolve_roster()` — a monkeypatchable
seam over `_agents.get_active_agents()` (mtime-cached; the same resolver the
daemon's own `_valid_intended_agents()` reaches through gates.capability_route).

FAIL-OPEN ON AN UNRESOLVABLE OR EMPTY ROSTER (rb-1028, never-block-on-absent-
evidence): when the vocabulary cannot be resolved, or resolves to {"either"}
alone (fresh install before the first /start), the check is SKIPPED — mirroring
routes_away_from's `len(valid) > 1` guard. An unreadable team-state must never
refuse every routed filing fleet-wide.

The CALLER decides the side effect: the CLI raises ValueError(message); the
daemon returns Response.error(400, ...). evaluate() NEVER raises for control
flow. Telemetry via _gate_log.log is fail-open.

WIRE AT THE ADD SITES ONLY, never inside `_validate_goal` — same blast-radius
reasoning as the three sibling gates: update_goal validates its in-lock
candidate through `_validate_goal`, so a check there would wedge status changes
on any legacy off-vocab carrier that arrives via merge from another box.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# gate_id MUST match core/config/gates.yaml id.
GATE_ID = "intended-agent-vocab"


def _resolve_roster() -> tuple:
    """Live agent names. Lazy import so a roster failure lands in evaluate's
    fail-open branch rather than at module import; monkeypatch seam for tests
    (the real resolver is env-independent — _agents._project_root() is derived
    from __file__, so a tmp-world fixture cannot redirect it)."""
    from _agents import get_active_agents
    return get_active_agents()


def evaluate(goal: Dict[str, Any], *, roster=None, meta_dir=None,
             agent_name: Optional[str] = None) -> Dict[str, Any]:
    """Check intended_agent against the live vocabulary. See module docstring."""
    gid = goal.get("id", "<unassigned>")

    def _log(decision: str, *, trigger_matched=None, payload=None, extra=None):
        # Fail-open: telemetry must never alter the verdict or raise.
        try:
            import _gate_log
            _gate_log.log(
                GATE_ID, decision,
                caller=f"gates.intended_agent_vocab.evaluate goal={gid}",
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

    if "intended_agent" not in goal:
        _log("noop")
        return _verdict("noop", [], None)

    val = goal.get("intended_agent")
    if val is None:
        _log("noop", extra={"reason": "intended_agent null"})
        return _verdict("noop", [], None)

    if not isinstance(val, str):
        violations = ["not_a_string"]
        _log("block", trigger_matched="not_a_string",
             payload=repr(val)[:200], extra={"would_block": True})
        return _verdict("block", violations, (
            f"Goal {gid}: intended_agent must be a string or null, got "
            f"{val!r}. Valid values are an active agent name or 'either'."
        ))

    if roster is None:
        try:
            roster = _resolve_roster()
        except Exception:
            _log("pass", extra={"reason": "roster unresolvable — fail-open"})
            return _verdict("pass", [], None)

    valid = set(roster) | {"either"}
    if len(valid) <= 1:
        # Empty roster (fresh install, all-retired, or unreadable team-state
        # collapsed to nothing) — cannot distinguish a typo from a real name.
        _log("pass", extra={"reason": "roster empty — fail-open (rb-1028)"})
        return _verdict("pass", [], None)

    # Membership on the RAW value — byte-parity with the CLI check this gate
    # extracts (`val not in valid`). routes_away_from strips for READ-side
    # tolerance; the write side stays strict so a padded or cased variant is
    # fixed at the moment of write instead of stored.
    if val in valid:
        _log("pass")
        return _verdict("pass", [], None)

    violations = ["off_vocabulary"]
    _log("block", trigger_matched="off_vocabulary", payload=val[:100],
         extra={"would_block": True, "vocabulary": sorted(valid)})
    return _verdict("block", violations, (
        f"Goal {gid}: intended_agent {val!r} is not in the live vocabulary "
        f"{sorted(valid)}. If you mean 'anyone', write 'either' — the read "
        f"side already treats off-vocabulary values that way (g-115-3482), "
        f"so storing {val!r} only misleads readers, and a typo of a real "
        f"agent name would silently broadcast a deliberate single-agent "
        f"routing. Writers converting free-text addressing (board tags, "
        f"directives) must normalize off-roster targets to 'either' before "
        f"filing, keeping the original in a tag for provenance."
    ))
