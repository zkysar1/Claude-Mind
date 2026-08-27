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

SECOND FAILURE THIS GATE COVERS (2026-08-25, zeta) -- `stop: true`.
The gate above reads only `prompt`, so a ScheduleWakeup carrying `stop: true`
and no prompt sailed through untouched. Under the deadman design
(.claude/rules/return-protocol.md) that single replace-slot wakeup is the ONLY
resurrection net behind the Skill(aspirations) chain: cancelling it while the
agent is RUNNING converts a recoverable pause into a hard stop. Measured: the
loop had already text-died, the agent cancelled the net "to stop spinning",
and the session could not resume on its own -- the user had to ask why it had
stalled.

The legitimate stop is NOT gated away: a real /stop writes `stop-requested`
FIRST (see .claude/rules/stop-hook-compliance.md), so that signal is the
discriminator -- present means a genuine stop is in flight and the cancel is
approved.

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


STOP_DENY_REASON = (
    "ScheduleWakeup stop rejected: agent-state is RUNNING and no "
    "`stop-requested` signal is set, so this cancel would remove the "
    "deadman resurrection net while the autonomous loop is still live.\n\n"
    "That net is a SINGLE replace-slot wakeup and it is the only thing that "
    "restarts the loop after a text-death breaks the Skill(aspirations) "
    "chain. Cancelling it turns a recoverable pause into a hard stop that "
    "needs a human to notice (measured 2026-08-25).\n\n"
    "What to do instead:\n"
    "  - Pausing because context is low or work is blocked? Do NOT cancel "
    "the net. Re-arm it (prompt='<<autonomous-loop-dynamic>>') and end the "
    "turn on your normal terminal call -- the wakeup will resume the loop.\n"
    "  - Genuinely stopping the agent? That is the USER's /stop, which "
    "writes `stop-requested` first; this gate approves the cancel once that "
    "signal exists.\n\n"
    "See .claude/rules/return-protocol.md (deadman terminal-pair) and "
    ".claude/rules/schedule-wakeup-correctness.md."
)


def _session_dir(session_id):
    """The bound agent's session/ dir, or None.

    Resolution goes through the hook PAYLOAD's session_id, exactly as the
    sibling PreToolUse hook `post-recovery-edit-gate.py` does -- NOT through
    `MIND_AGENT`. That env var is injected by the PreToolUse[Bash] hook for
    Bash calls only; this hook fires on ScheduleWakeup, where it is absent, so
    an env-based resolve would make the guard silently INERT in production
    while hand-testing green (the class that left `pre-edit-context-gate`
    dead for 59 days).

    `MIND_AGENT_DIR` is honored first as the sanctioned unit-test override
    (`_paths.py` Tier 4: "exists for unit tests; production code never sets it").
    """
    import os
    override = os.environ.get("MIND_AGENT_DIR", "").strip()
    if override:
        return Path(override) / "session"
    if not session_id:
        return None
    from _paths import PROJECT_ROOT, agent_dir
    from _resolve_agent_from_sid import resolve as resolve_agent
    agent = resolve_agent(session_id, PROJECT_ROOT)
    if not agent:
        return None
    return agent_dir(agent) / "session"


def _cancel_would_strand_loop(tool_input, session_id):
    """True when `stop: true` would cancel the deadman on a LIVE loop.

    Every outcome of this new test is enumerated (guard-3328):
      1. `stop` falsy/absent      -> False (not this branch; prompt check runs)
      2. agent unresolvable / agent-state unreadable -> False (fail-open)
      3. agent-state != RUNNING   -> False (loop already idle; cancel is fine)
      4. RUNNING + stop-requested -> False (a real /stop is in flight)
      5. RUNNING, no stop-requested -> True (DENY -- strands the loop)
    """
    if not isinstance(tool_input, dict) or not tool_input.get("stop"):
        return False                                    # outcome 1
    try:
        session = _session_dir(session_id)
        if session is None:
            return False                                # outcome 2
        state_file = session / "agent-state"
        if not state_file.is_file():
            return False                                # outcome 2
        if state_file.read_text(encoding="utf-8").strip() != "RUNNING":
            return False                                # outcome 3
        if (session / "stop-requested").exists():
            return False                                # outcome 4
        return True                                     # outcome 5
    except Exception:
        return False                                    # outcome 2 (fail-open)


def main():
    payload = stdin_json_or_approve()
    if not isinstance(payload, dict):
        approve_no_mutation()

    if payload.get("tool_name") != "ScheduleWakeup":
        approve_no_mutation()

    tool_input = payload.get("tool_input")
    prompt = tool_input.get("prompt") if isinstance(tool_input, dict) else None

    if _cancel_would_strand_loop(tool_input, payload.get("session_id", "")):
        emit_deny(STOP_DENY_REASON)

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
