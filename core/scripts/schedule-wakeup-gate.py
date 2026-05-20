#!/usr/bin/env python3
"""PreToolUse[ScheduleWakeup] hook -- refuse slash-command prompts that get
rejected by Claude Code's user-invocable gate when the wakeup fires.

Canonical failure (2026-05-18, zeta f1f3066e): LLM called ScheduleWakeup with
`prompt: "/aspirations loop"`. When the wakeup fired, the runtime injected
"/aspirations loop" as USER INPUT. The slash-command resolver matched
`aspirations` (user-invocable: false) and rejected with "This skill can only
be invoked by Claude, not directly by users." Four hits in one session.

Correct patterns (per the ScheduleWakeup tool's own documentation):
  - Autonomous loop: prompt = "<<autonomous-loop-dynamic>>" (sentinel)
  - User /loop continuation: prompt = "/loop <original args>" verbatim
  - Natural-language wakeup: any plain string with no leading slash

The bad-vs-good predicate lives in `_swakeup_predicate.py` — single source of
truth shared with the Layer C audit script. Do NOT inline the check here.

Fail-open contract (CRITICAL — do not change without revisiting the trade):
this gate exists to catch a known LLM mistake, not to be a critical-path
dependency. Any parse/IO/logic error -> approve. A broken gate is recoverable
(the audit script catches what we missed); a fail-closed gate would block
legitimate ScheduleWakeup calls and stall autonomous loops.
"""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from hook_helpers import (  # noqa: E402
    approve_no_mutation,
    emit_deny,
    stdin_json_or_approve,
)
from _swakeup_predicate import is_bad_slash_prefix  # noqa: E402


DENY_REASON = (
    "ScheduleWakeup prompt rejected: slash-command prompts (other than "
    "literal '/loop ...') get re-parsed as user input when the wakeup "
    "fires, and skills with user-invocable=false (aspirations, boot, "
    "respond, reflect, review-hypotheses, etc.) are then refused by the "
    "slash-command resolver.\n\n"
    "Use one of these correct patterns instead:\n"
    "  - Autonomous loop continuation: prompt='<<autonomous-loop-dynamic>>' "
    "(the sentinel - runtime resolves it back to autonomous-loop "
    "instructions at fire time)\n"
    "  - User-initiated /loop continuation: prompt='/loop <original args>' "
    "(only valid when the loop began with a user-typed /loop command)\n"
    "  - Natural-language wakeup: a plain English description of what to "
    "resume on (no leading slash)\n\n"
    "Anti-pattern note: short-interval ScheduleWakeup to POLL for "
    "background Bash completion is also wrong - when harness-tracked "
    "work finishes, the agent is re-invoked automatically. See "
    "ScheduleWakeup tool description and .claude/rules/"
    "schedule-wakeup-correctness.md."
)


def main():
    payload = stdin_json_or_approve()
    if not isinstance(payload, dict):
        approve_no_mutation()

    if payload.get("tool_name") != "ScheduleWakeup":
        approve_no_mutation()

    tool_input = payload.get("tool_input")
    prompt = tool_input.get("prompt") if isinstance(tool_input, dict) else None

    if is_bad_slash_prefix(prompt):
        emit_deny(DENY_REASON)

    approve_no_mutation()


if __name__ == "__main__":
    # except Exception lets SystemExit (raised by approve/emit_deny via
    # sys.exit) propagate cleanly. The catch is only for unexpected bugs in
    # main() - in which case we still fail-open per the docstring contract.
    try:
        main()
    except Exception:
        sys.exit(0)
