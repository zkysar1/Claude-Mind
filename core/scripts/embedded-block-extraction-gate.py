#!/usr/bin/env python3
"""PreToolUse[Bash] ADVISORY: hand-rolled embedded-block extraction ().

Closes guard-2222 at the TOOL layer, which is what that guardrail's own text
pre-commits to after a third recurrence: "Treat a further recurrence as evidence
for closing this at the TOOL layer (make the hand-rolled path harder or the
helper the obvious default), not for restating the rule again."

ADVISORY, NEVER BLOCKING. Every match calls emit_advisory -- permissionDecision
stays "allow" and the command runs. That is deliberate, not timidity:

  - The hand-rolled shape is not forbidden. It is a worse tool for one job, and
    there are legitimate uses of every construct in the predicate.
  - The failure this addresses is a REFLEX gap, not a permission gap. Three
    agents knew the rule (times_active 1796) and still did not reach for the
    helper, because extracting a block feels mechanical. What is missing is a
    reminder at the moment of use -- exactly what an advisory delivers.
  - A denial would be unfalsifiable friction on a common pipeline shape.
    pre-edit-context-gate.sh is the sibling precedent: advisory, never denies.

The predicate lives in _embedded_block_predicate.py and is shared verbatim with
the Layer-C detective (embedded-block-hand-roll-audit.py), so the gate and the
audit can never drift into disagreeing about what the shape IS. Mirrors the
_swakeup_predicate.py gate+detective arrangement.

SAFETY: fail open on ANY error (guard-591 body contract) -- import failure,
malformed payload, unexpected shape all reach approve_no_mutation(). A hook that
breaks must never break the Bash tool.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from hook_helpers import (approve_no_mutation, emit_advisory,
                              stdin_json_or_approve)
except Exception:
    sys.exit(0)


def main():
    try:
        from _embedded_block_predicate import advisory_text, detect
    except Exception:
        approve_no_mutation()

    payload = stdin_json_or_approve()
    if not isinstance(payload, dict):
        approve_no_mutation()

    if payload.get("tool_name") not in (None, "Bash"):
        approve_no_mutation()

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        approve_no_mutation()

    command = tool_input.get("command")
    if not isinstance(command, str) or not command:
        approve_no_mutation()

    try:
        finding = detect(command)
    except Exception:
        approve_no_mutation()

    if not finding:
        approve_no_mutation()

    emit_advisory(advisory_text(finding))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)
