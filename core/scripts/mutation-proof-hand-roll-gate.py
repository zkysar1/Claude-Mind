#!/usr/bin/env python3
"""PreToolUse[Bash] ADVISORY: hand-rolled mutation proof ().

Closes the four-artifact honor-system cluster (rb-5219, guard-1516, guard-1595,
guard-1621) at the TOOL layer, which is what g-115-3494's verification criteria
require: "The goal is NOT closed by creating an additional rule, guardrail, or
reasoning-bank entry restating the same lesson."

ADVISORY, NEVER BLOCKING. Every match calls emit_advisory -- permissionDecision
stays "allow" and the command runs. Deliberate, for the same three reasons the
sibling embedded-block gate states:

  - The hand-rolled shape is not forbidden. It is a worse tool for one job.
  - The failure is a REFLEX gap, not a permission gap. Three agents knew the
    rule -- guard-1595 has fired 115 times, guard-1621 72 -- and hand-rolled it
    anyway, because the procedure feels mechanical while you are inside it.
    guard-1475's case is sharper still: it fired correctly, at the right
    moment, and stated the whole recipe INLINE, so following it produced no
    felt gap -- and a felt gap is the only thing that would prompt a registry
    lookup. The artifact that fires can SUPPRESS the lookup by being helpful.
  - A denial would be unfalsifiable friction on shapes with legitimate uses.

The predicate lives in _mutation_proof_predicate.py, shared verbatim with any
future Layer-C detective so gate and audit cannot drift about what the shape IS.

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
        from _mutation_proof_predicate import advisory_text, detect
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
