#!/usr/bin/env python3
"""PostToolUse hook — plan-completion verdict reminder (plan-mode approval).

Plants the completion obligation in the model's recent context mechanically,
so delivering the verdict does not depend on the model remembering a rule
under context pressure (2026-09-05 observation: agents end plan execution on
"plan finished", leave the plan in place, and never answer the question the
user originally asked). The always-loaded rule is
.claude/rules/plan-completion-verdict.md; this hook is its mechanical booster
at the one structural moment a harness exposes to a hook:

* ExitPlanMode — the APPROVAL gate. Nothing on the hook surface marks
  execution's END, so the reminder is planted at its START.

WHY THERE IS NO PLAN-TOOL BRANCH (retired 2026-09-21). From 2026-09-05 this
hook also matched a task-network plan tool (settings.json matcher
`update_plan`, wire name `TodoWrite`) and fired when the tool's result showed
every step done. It found that moment by parsing the harness's PROSE: a header
line shaped "Current plan (F/T steps done):". Five days later the harness
stopped echoing the plan and returned a one-line receipt instead (Zak-Code
ADR-0124, 2026-09-10), and the branch never fired again. Nothing went red,
because this hook's own tests fed it the old render, which no harness was
sending any more (guard-920: a fixture must carry the production shape).
Measured 2026-09-21: the harness's real wire payload for a finished plan left
the hook silent while the old render still fired it, and 0 of 284 plan-tool
results across four served runs carried the reminder.

Retired rather than repaired, because the HARNESS owns that moment. A harness
that keeps a plan knows when it completes without parsing anything, and
Zak-Code already speaks at that moment itself: its ADR-0108 closing line
carries the original request, never enters the persisted history, and is
withheld while a turn-end hook governs the turn. That last part is measured:
a line asking for the closing answer inside such a turn made a small model
stop a second time in 18 of 116 rollouts, against 0 of 116 without it
(Zak-Code bench/results/veto-door-preregistration.log, ADR-0205). A repaired
branch would have written that same ask into the PERSISTED tool result on
every finished plan of an autonomous loop. Do not re-add a branch that reads a
harness's rendered text: a reminder a harness needs at its own plan's end
belongs in the harness, keyed on its own state. A plan tool's payload now
falls through the different-tool guard below and is silent.

Output contract: hookSpecificOutput.additionalContext per Claude Code's
PostToolUse format. Empty stdout + exit 0 = nothing injected.

SAFETY: fail open on ANY error. Never exits non-zero. Never emits malformed
JSON. The settings.json matcher is the real gate; the tool_name check
below is a defensive no-op guard.
"""
import json
import sys

REMINDER = (
    "<system-reminder>\n"
    "[plan-completion-verdict] Plan APPROVED — execution begins. This plan is a\n"
    "MEANS, not the deliverable. When EVERY step has executed:\n"
    "  1. CLEAR the plan file (delete it, or mark it complete) so no stale plan\n"
    "     lingers as active work.\n"
    "  2. RE-READ the plan file's `## Original request` section. If the plan lacks\n"
    "     one, add it NOW, quoting the user's ask verbatim — it is the durable\n"
    "     anchor the verdict is derived from and it survives context compression.\n"
    "  3. ANSWER that original request with a conclusion/verdict, leading with the\n"
    "     answer. The plan's steps are supporting detail, not the headline.\n"
    "NEVER end on \"plan finished\" / \"done\" / a step recap with no conclusion —\n"
    "that hands the user a finished plan and an unanswered question.\n"
    "Rule: .claude/rules/plan-completion-verdict.md\n"
    "</system-reminder>"
)


def _emit(text: str) -> None:
    try:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": text,
            }
        }))
    except Exception:
        pass


def main() -> int:
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    # Defensive: the settings.json matcher is the real gate. Only no-op when the
    # payload positively names a DIFFERENT tool (a plan tool's included).
    payload: dict = {}
    try:
        parsed = json.loads(raw) if raw.strip() else None
        payload = parsed if isinstance(parsed, dict) else {}
    except Exception:
        payload = {}
    tool_name = payload.get("tool_name")
    if tool_name and tool_name != "ExitPlanMode":
        return 0
    _emit(REMINDER)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
