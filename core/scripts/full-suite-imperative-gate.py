#!/usr/bin/env python3
"""PreToolUse[Bash] hook -- deliver the run-full-suite imperative JIT, at the
moment a suite is about to run.

WHY THIS EXISTS (g-115-6469, outcome #3). The behavioural heads of
`.claude/rules/run-full-suite-after-deep-code.md` are the difference between a
trustworthy green and a green that means nothing -- and that rule was 37 KB of
the fixed per-turn preamble, paid by every agent on every turn whether or not
it was ever going to run a suite. Path-scoping it moves the cost to the moment
of use, but scoping ALONE would be a regression: a path-scoped rule loads when
a matching file is touched and is NOT re-injected after a compaction, so an
agent that edits a script, autocompacts, then closes the goal would claim "all
tests pass" with the rule absent -- exactly the failure the rule exists to
prevent.

This hook is the half that makes the scoping safe. It fires off the COMMAND,
not off a file read, so it reaches the closure regardless of what the preamble
happens to hold. Same shape as the gradle rule's defence and for the same
reason: when a rule's knowledge has a chokepoint, the chokepoint is a better
carrier than the preamble. See core/config/conventions/rules-loading.md.

ADVISORY, NEVER A DENY. The decision is always `allow` and the command always
runs. This gate has no opinion on whether the suite SHOULD run -- only on how
to read what it prints.

Fail-open contract: any parse/IO/logic error approves silently. A broken
advisory is recoverable; a fail-closed one would stall every test invocation in
the fleet.

The predicate and the imperative text live in `_full_suite_imperative.py` --
single source of truth shared with the test suite. Do NOT inline either here.
"""

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from hook_helpers import (  # noqa: E402
    approve_no_mutation,
    stdin_json_or_approve,
)
from _full_suite_imperative import (  # noqa: E402
    OVERRIDE_TOKEN,
    build_message,
    matched_families,
)


def emit_advisory(message):
    """Non-blocking advisory on the channel that actually reaches the model.

    Field selection is NOT a guess -- it is the shape measured to deliver in
    g-115-3511 (see core/scripts/trailing-echo-exit-gate.py, which carries the
    five-probe table). `allow` + `permissionDecisionReason` alone did NOT
    deliver; the combination below did. Reused rather than re-derived on
    purpose: re-probing needs a FRESH session per probe because hook-injected
    context appears deduped per session, so a negative from inside one session
    cannot distinguish "wrong field" from "already delivered once".

    stderr carries only a ONE-LINE pointer, unlike the sibling gates which echo
    the whole message there. Those fire on a mistake; this one fires on every
    ordinary test invocation, and a 25-line banner in the terminal each time
    would train a human to stop reading hook output altogether.
    """
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": message,
            "additionalContext": message,
        },
        "systemMessage": message,
    }
    sys.stderr.write(
        "[full-suite-imperative] read the VERDICT line first; never pipe the "
        "runner. (.claude/rules/run-full-suite-after-deep-code.md)\n"
    )
    print(json.dumps(payload))


def main():
    payload = stdin_json_or_approve()
    if not isinstance(payload, dict):
        approve_no_mutation()

    if payload.get("tool_name") != "Bash":
        approve_no_mutation()

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        approve_no_mutation()

    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        approve_no_mutation()

    if OVERRIDE_TOKEN in command:
        approve_no_mutation()

    families = matched_families(command)
    if not families:
        approve_no_mutation()

    emit_advisory(build_message(families))
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        approve_no_mutation()
