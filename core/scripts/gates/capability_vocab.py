"""requires_capability vocabulary gate — daemon-safe shared extraction ().

`requires_capability` is an opt-in per-RUNNER fence: a goal declares the tokens a
box must provide, and `goal_is_locally_executable` is a plain subset test against
what the box provides. KNOWN_CAPABILITIES calls itself "the cross-file contract
between goal `requires_capability` values and the runner_capabilities config" —
and until this gate, NOTHING ENFORCED IT. Fleet-wide grep returned exactly three
references: the definition, one test asserting the six tokens are a subset of
itself, and a prose comment in aspirations.yaml.

WHY AN UNKNOWN TOKEN IS WORSE THAN A TYPO IN ANY OTHER FIELD. No runner ever adds
an off-contract token to its capability set — the only surface that can is a
hand-written `runner_capabilities.provides` in `local-paths.conf`, which is
gitignored AND in `owncloud_sync._EXCLUDE_NAMES`, so it reaches neither git nor
S3. A goal carrying an unknown token is therefore filtered out of the ranked pool
on EVERY box in the fleet, permanently, and nothing reports the strand. This is
the asymmetry `_runner_capabilities`' own docstring is built around (rb-1028: a
wrongly-hidden goal is invisible, un-actioned and un-learned-from) — realised in
full, because the field whose PURPOSE is routing work away from boxes that cannot
do it instead routes work away from everyone, including the one box that can.

THREE MEASURED LIVE INSTANCES, none of which anything refused at write time:
  * `win32` (g-369-61, 2026-09-03) — sat block_reason=not_my_lane for 5 DAYS
    while an idle Windows box with the target repo sat next to it.
  * free text `"live runner claim for agent dir foxtrot"` (g-115-9215,
    2026-09-06, bravo/cc-05) — the cleanest signal was the candidate COUNT:
    1615 with the field set, 1616 with it null. Not ranked low; ABSENT.
  * `vinheim-operator-api-key` (g-369-404, 2026-09-20, bravo/cc-05) — HIGH, and
    the goal measuring that 8 of 29 active customers (27.6%) have runs but no
    LEDGER row and therefore cannot be billed.

WHY WRITE TIME AND NOT A READ-TIME WARNING. Nothing READ the field until the
selector silently dropped the row, so a read-time warning fires on a box that has
already excluded the goal — and the author is long gone. The moment of write is
the only point where the person who chose the token is present to fix it (the
same rationale the `_goal_fields` allowlist rests on). Every audit that asks "is
this goal correctly routed?" answers YES for these rows: status pending,
intended_agent set, handoff_to set, every field well-formed.

SCOPE: UNKNOWN TOKENS ONLY. A token in `NEVER_AUTO_PROVIDED` (currently
`studio-session`) is equally invisible when no box declares it, but it is a
LEGITIMATE token that routes correctly the moment a Studio host declares it, so
refusing it here would be wrong. That case is surfaced at read time instead, by
`_runner_capabilities.capability_block_detail`. Validating against
KNOWN_CAPABILITIES is NECESSARY AND NOT SUFFICIENT (zeta, 2026-09-06) — this gate
is the necessary half and does not pretend to be the whole of it.

FAIL-OPEN (rb-1028): an absent field, a null, a non-list/str shape, or an
unimportable `_runner_capabilities` all SKIP. A gate that cannot read its own
vocabulary must never refuse every capability-tagged filing fleet-wide.

NO OVERRIDE FLAG, DELIBERATELY. The sanctioned fix is to correct the token or to
register a real one in KNOWN_CAPABILITIES (an agent-editable framework file), so
there is no invariant here the author lacks authority to resolve — and naming a
bypass in the deny text of a gate whose whole purpose is a closed vocabulary
would re-open the hole (guard-5502).

Public API:
    evaluate(goal, *, meta_dir=None, agent_name=None) -> dict

Return shape (mirrors gates.intended_agent_vocab):
    {
      "would_block": bool,
      "decision": str,          # "noop" | "pass" | "block"
      "violations": list[str],
      "message": str | None,    # populated only when would_block is True
    }

The CALLER decides the side effect: the CLI raises ValueError(message); the
daemon returns Response.error(400, ...). evaluate() NEVER raises for control
flow. Telemetry via _gate_log.log is fail-open.

WIRE AT THE ADD SITES ONLY, never inside `_validate_goal` — same blast-radius
reasoning as the four sibling gates: update_goal validates its in-lock candidate
through `_validate_goal`, so a check there would wedge status changes on any
legacy carrier that arrives via merge from another box. The three measured
instances above are exactly such carriers and must stay editable.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# gate_id MUST match core/config/gates.yaml id.
GATE_ID = "capability-vocab"


def evaluate(goal: Dict[str, Any], *, meta_dir=None,
             agent_name: Optional[str] = None) -> Dict[str, Any]:
    """Check requires_capability against KNOWN_CAPABILITIES. See module docstring."""
    gid = goal.get("id", "<unassigned>") if isinstance(goal, dict) else "<unassigned>"

    def _log(decision: str, *, trigger_matched=None, payload=None, extra=None):
        # Fail-open: telemetry must never alter the verdict or raise.
        try:
            import _gate_log
            _gate_log.log(
                GATE_ID, decision,
                caller=f"gates.capability_vocab.evaluate goal={gid}",
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

    if not isinstance(goal, dict) or "requires_capability" not in goal:
        _log("noop")
        return _verdict("noop", [], None)

    raw = goal.get("requires_capability")
    if raw is None:
        _log("noop", extra={"reason": "requires_capability null"})
        return _verdict("noop", [], None)

    # Shape tolerance mirrors goal_required_capabilities exactly (a str or a
    # list/tuple/set); anything else is read as "no requirement" there, so
    # refusing it here would disagree with the reader this gate protects.
    if not isinstance(raw, (str, list, tuple, set)):
        _log("pass", extra={"reason": f"unreadable shape {type(raw).__name__} — fail-open"})
        return _verdict("pass", [], None)

    try:
        from _runner_capabilities import (
            KNOWN_CAPABILITIES, NEVER_AUTO_PROVIDED, unknown_capability_tokens)
    except Exception:
        # Cannot resolve the vocabulary — never refuse on absent evidence.
        _log("pass", extra={"reason": "vocabulary unresolvable — fail-open (rb-1028)"})
        return _verdict("pass", [], None)

    unknown = sorted(unknown_capability_tokens(raw))
    if not unknown:
        _log("pass")
        return _verdict("pass", [], None)

    valid = sorted(KNOWN_CAPABILITIES)
    hand_only = sorted(NEVER_AUTO_PROVIDED)
    _log("block", trigger_matched="unknown_capability_token",
         payload=",".join(unknown)[:200],
         extra={"would_block": True, "vocabulary": valid})
    return _verdict("block", ["unknown_capability_token"], (
        f"Goal {gid}: requires_capability {unknown!r} is not in "
        f"KNOWN_CAPABILITIES {valid}. No runner can ever provide an "
        f"off-contract token — a box's capability set is built from probes plus "
        f"a hand-written runner_capabilities.provides in local-paths.conf, which "
        f"is gitignored and sync-excluded — so this goal would be filtered out of "
        f"the ranked pool on EVERY box in the fleet, permanently, with no error "
        f"anywhere (measured: g-369-61 hidden 5 days; g-115-9215 absent from a "
        f"1615-candidate pool; g-369-404, a revenue goal, hidden while 27.6% of "
        f"active customers could not be billed). Fix the token, or register a "
        f"real capability in core/scripts/_runner_capabilities.py "
        f"KNOWN_CAPABILITIES together with a probe that asserts it. Note that "
        f"membership alone is not enough: {hand_only} are in the vocabulary but "
        f"are never auto-probed, so a goal requiring one is hidden fleet-wide "
        f"until some box declares it."
    ))
