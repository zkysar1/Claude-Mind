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

THIRD FAILURE THIS GATE COVERS (2026-09-21, sera) -- arming the net with no
loop under it. The net OUTLIVES the loop it was armed for: after a /stop the
agent is IDLE, and a leftover sentinel still fires. The turn that receives it
re-arms FIRST by rule (rb-4345 -- restore the net before any loop-entry work
that could fail), and only THEN reaches the Phase -1.5 state gate, which
refuses because the agent is not RUNNING. So the turn re-arms the very net
whose firing it could not use, and 600s later does it again: a loop with no
exit, made of two steps that are each correct on a live loop. The re-arm
ordering is right and stays; what was missing is that an IDLE agent has
nothing to resurrect. State is the discriminator here exactly as
`stop-requested` is above.

That refusal's TEXT must not assume IDLE means stopped (g-373-138). A graceful
stop sets IDLE at D1 and still owes D4-D7; told "this turn ends normally" there,
a vessel mind ended its turn mid-consolidation with no handoff. When
`stop_in_progress` attributes an unfinished stop to THIS session, the same deny
names the continuation instead.

FOURTH FAILURE THIS GATE COVERS (2026-09-24, alpha; g-115-10755) -- an arm
without `noop`. Claude Code 2.1.280 refuses every ScheduleWakeup that omits
`noop` unless `stop` is true. That refusal is an ordinary error result, and in
the deadman pair the batched Skill(aspirations) runs regardless, so loops
looked healthy fleet-wide with NO net armed. This deny says so in the loop's
own terms. It is reachable: a noop-less call with a slash prompt returned THIS
gate's slash deny, not the harness's noop error (zeta, cc-02, 2026-09-25), so
hooks see the call before the harness validates the field. The check mirrors
the harness's rule and is coupled to it: if a later harness stops requiring
`noop`, remove this check together with the `noop` in the documented shapes.
It is Claude Code's rule, and it is applied only there: a Zak-Code Body's
ScheduleWakeup arms without `noop` (its slot takes noop=None), so this deny under
that harness refuses an arm the harness would have honoured and manufactures the
very wedge it describes -- measured 2026-09-25 on a worker Body: nine identical
denials in one turn (77 min), and on the build whose schema declares `noop` the
model still omitted it on its first arm. The harness is read from the marker each
one exports to every hook (`CLAUDECODE` / `ZAKCODE_SESSION`), the same detector
`_confidence_ledger.py` and `_runtime.sh` use; an unknown harness keeps the
stricter rule.

The same failure has a second field (2026-09-25, g-115-10936): the same harness
refuses an arm without `reason` -- "`delaySeconds` and `reason` are required
when `stop` is not true." (zeta, cc-02) -- with the same silent-disarm result.
Its deny mirrors the `noop` one: same harness scope, runs right after it, and an
arm missing both gets the `noop` deny, whose example carries both fields.
Zak-Code declares `reason` optional in its ScheduleWakeup schema (Zak-Code
origin/main 6be6925, src/zakcode/tools/builtins/schedule_wakeup.py), so the
carve-out holds for this field too.

Fail-open contract (CRITICAL — do not change without revisiting the trade):
this gate exists to catch a known LLM mistake, not to be a critical-path
dependency. Any parse/IO/logic error -> approve. A broken gate is recoverable
(the audit script catches what we missed); a fail-closed gate would block
legitimate ScheduleWakeup calls and stall autonomous loops.
"""

import os
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


ARM_DENY_REASON = (
    "ScheduleWakeup re-arm rejected: this is the autonomous-loop deadman "
    "sentinel, and agent-state is not RUNNING -- there is no loop under the "
    "net to resurrect.\n\n"
    "A net outlives the loop it was armed for. After a /stop the agent is "
    "IDLE, but a sentinel armed before it still fires; the turn that receives "
    "it re-arms first by rule and only then reads the state gate, which "
    "refuses. Arming again from there re-enters the same turn forever "
    "(measured 2026-09-21).\n\n"
    "What to do instead:\n"
    "  - Nothing. You are IDLE: the loop is stopped, the net protects nothing, "
    "and this turn ends normally. In assistant or reader mode, answer the user "
    "and stop -- there is no loop to keep alive.\n"
    "  - Waiting on something EXTERNAL (a CI run, a deploy)? That is a "
    "legitimate wakeup, but give it its own natural-language prompt; the "
    "sentinel means loop-continuation and nothing else.\n"
    "  - Meant to resume autonomous work? That is the USER's /start, which "
    "sets agent-state to RUNNING; this gate approves the sentinel from there.\n\n"
    "See .claude/rules/schedule-wakeup-correctness.md (Re-arm FIRST on "
    "resurrection)."
)


NOOP_DENY_REASON = (
    "ScheduleWakeup rejected: `noop` is missing, so this call would schedule "
    "NOTHING. Claude Code (2.1.280+) refuses every ScheduleWakeup without "
    "`noop` unless `stop` is true. When this is the deadman net, the batched "
    "Skill(aspirations) re-entry still runs, so the loop looks healthy with no "
    "resurrection net armed (measured fleet-wide 2026-09-24, g-115-10755).\n\n"
    "Re-emit the SAME call with noop=false, the framework's canonical value, "
    "and a short `reason` (the harness requires that field too), "
    "e.g. ScheduleWakeup(prompt='<<autonomous-loop-dynamic>>', "
    "delaySeconds=600, noop=false, reason='deadman resurrection net'). See "
    ".claude/rules/schedule-wakeup-correctness.md."
)


REASON_DENY_REASON = (
    "ScheduleWakeup rejected: `reason` is missing, so this call would schedule "
    "NOTHING. Claude Code (2.1.280+) refuses every ScheduleWakeup without "
    "`reason` unless `stop` is true (\"`delaySeconds` and `reason` are required "
    "when `stop` is not true.\"). When this is the deadman net, the batched "
    "Skill re-entry still runs, so the loop looks healthy with no resurrection "
    "net armed -- the same silent disarm as a missing `noop` (g-115-10936).\n\n"
    "Re-emit the SAME call with a short one-line reason, e.g. "
    "ScheduleWakeup(prompt='<<autonomous-loop-dynamic>>', delaySeconds=600, "
    "noop=false, reason='deadman resurrection net'). See "
    ".claude/rules/schedule-wakeup-correctness.md."
)


LOOP_SENTINEL = "<<autonomous-loop-dynamic>>"


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


def _arm_would_resurrect_nothing(tool_input, session_id):
    """True when the deadman sentinel is being armed on an agent with no loop.

    The mirror image of `_cancel_would_strand_loop`: that one refuses a CANCEL
    while the loop is live, this one refuses an ARM while it is not. They read
    the same one file, and between them the net exists exactly when a loop does.

    Every outcome of this test is enumerated (guard-3328):
      1. `stop` truthy -> False. This call is a CANCEL, not an arm: the tool's
         own contract is that when `stop` is true "all other fields are
         ignored", so a sentinel sitting in `prompt` there is vestigial --
         usually the model re-sending its previous args. Reading it as an arm
         DENIES a legitimate cancel, and cancelling a leftover net from IDLE is
         exactly the manual remedy for the bug this guard exists to prevent.
         `_cancel_would_strand_loop` owns every stop-bearing call.
      2. prompt is not the sentinel -> False (a /loop continuation or an
         external-wait wakeup is the user's or the world's business, never the
         loop's net; only the sentinel claims to resurrect the loop)
      3. agent unresolvable / agent-state unreadable -> False (fail-open).
         Absence of the file is UNINITIALIZED and lands here, so the last
         outcome really is IDLE: `session.py` VALID_STATES is {RUNNING, IDLE}.
      4. agent-state == RUNNING -> False (a live loop -- this is the rb-4345
         re-arm the deadman design REQUIRES, and blocking it would wedge the
         very remedy the other deny message hands out)
      5. agent-state is anything else -> True (DENY -- IDLE has no loop, so the
         net would fire into a Phase -1.5 refusal that re-arms it again)
    """
    if not isinstance(tool_input, dict) or tool_input.get("stop"):
        return False                                    # outcome 1
    if tool_input.get("prompt") != LOOP_SENTINEL:
        return False                                    # outcome 2
    try:
        session = _session_dir(session_id)
        if session is None:
            return False                                # outcome 3
        state_file = session / "agent-state"
        if not state_file.is_file():
            return False                                # outcome 3
        return state_file.read_text(encoding="utf-8").strip() != "RUNNING"
    except Exception:
        return False                                    # outcome 3 (fail-open)


def _arm_deny_reason(session_id):
    """ARM_DENY_REASON -- unless THIS session is mid-way through a graceful stop.

    D1 sets IDLE long before D7 finishes the stop, so "you are IDLE, this turn
    ends normally" is false in that window, and a model that believed it ended
    the turn mid-D4 with no handoff (measured 2026-09-24, g-373-138). The deny
    stands either way -- arming is still wrong -- only the text changes, and
    only when stop_in_progress positively attributes an unfinished stop to this
    session. Any fault falls back to the unchanged text.
    """
    try:
        session = _session_dir(session_id)
        if session is not None:
            from stop_in_progress import arm_refusal_for
            text = arm_refusal_for(session, session_id)
            if text:
                return text
    except Exception:
        pass
    return ARM_DENY_REASON


def _arm_lacks_noop(tool_input):
    """True when an ARM omits `noop`, which the harness requires on every arm.

    Every outcome is enumerated (guard-3328):
      1. payload not a dict -> False (fail-open)
      2. `stop` truthy -> False. A cancel: the harness requires `noop` only
         "when `stop` is not true", so demanding it here would block the
         legitimate /stop cancel that `_cancel_would_strand_loop` approves.
      3. `noop` present, true OR false -> False
      4. `noop` absent or null -> True (DENY -- the harness would refuse the
         call and set no wakeup at all)
    """
    if not isinstance(tool_input, dict) or tool_input.get("stop"):
        return False                                    # outcomes 1, 2
    return tool_input.get("noop") is None               # outcomes 3, 4


def _arm_lacks_reason(tool_input):
    """True when an ARM omits `reason`, which the harness also requires on every
    arm -- the exact mirror of `_arm_lacks_noop` (g-115-10936).

    Every outcome is enumerated (guard-3328):
      1. payload not a dict -> False (fail-open)
      2. `stop` truthy -> False. A cancel: the harness requires `reason` only
         "when `stop` is not true".
      3. `reason` present -> False. The gate demands the FIELD, not a wording.
      4. `reason` absent or null -> True (DENY -- the harness would refuse the
         call and set no wakeup at all)
    """
    if not isinstance(tool_input, dict) or tool_input.get("stop"):
        return False                                    # outcomes 1, 2
    return tool_input.get("reason") is None             # outcomes 3, 4


def _harness_requires_arm_fields():
    """True unless the harness running this hook is one whose ScheduleWakeup arms
    without `noop` and `reason`.

    Every outcome is enumerated (guard-3328):
      1. `CLAUDECODE` set -> True. Claude Code (2.1.280+) refuses the arm itself,
         so the denies above name the fix the harness would otherwise hide.
      2. else `ZAKCODE_SESSION` or `ZAKCODE_MODEL` set -> False. A Zak-Code agent
         exports the session id into every hook's environment for exactly this
         branch, and its slot arms with noop=None and needs no reason (its schema
         declares `reason` optional): refusing here would only turn a harmless
         omission into a wedge.
      3. neither -> True. An unknown harness keeps the stricter rule; the cost of
         being wrong is one deny the model can act on, not a net that never arms.
    Same detector as `_confidence_ledger.py` / `_runtime.sh` (Claude Code first).
    """
    if (os.environ.get("CLAUDECODE") or "").strip():
        return True                                     # outcome 1
    if any((os.environ.get(k) or "").strip() for k in ("ZAKCODE_SESSION", "ZAKCODE_MODEL")):
        return False                                    # outcome 2
    return True                                         # outcome 3


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

    if _arm_would_resurrect_nothing(tool_input, payload.get("session_id", "")):
        emit_deny(_arm_deny_reason(payload.get("session_id", "")))

    if is_bad_slash_prefix(prompt):
        emit_deny(DENY_REASON)

    # Last on purpose: a call that also fails a check above gets THAT check's
    # reason, which names the more fundamental fix.
    if _arm_lacks_noop(tool_input) and _harness_requires_arm_fields():
        emit_deny(NOOP_DENY_REASON)

    # After noop: an arm missing both fields gets the noop deny, whose example
    # already carries `reason`, so one retry fixes both.
    if _arm_lacks_reason(tool_input) and _harness_requires_arm_fields():
        emit_deny(REASON_DENY_REASON)

    approve_no_mutation()


if __name__ == "__main__":
    # except Exception lets SystemExit (raised by approve/emit_deny via
    # sys.exit) propagate cleanly. The catch is only for unexpected bugs in
    # main() - in which case we still fail-open per the docstring contract.
    try:
        main()
    except Exception:
        sys.exit(0)
