#!/usr/bin/env python3
"""PostToolUse[ExitPlanMode] hook — plan-completion verdict reminder.

Fires the moment a plan is APPROVED and execution begins. It plants the
completion obligation in the model's recent context mechanically, so
delivering the verdict does not depend on the model remembering a rule under
context pressure — the same failure class as the idea-capture gap
(2026-09-05 observation: agents end plan execution on "plan finished", leave
the plan file in place, and never answer the question the user originally
asked). The always-loaded rule is .claude/rules/plan-completion-verdict.md;
this hook is its mechanical booster at the one structural moment the harness
exposes (ExitPlanMode is the approval gate — nothing marks execution's END, so
the reminder is planted at its START).

Output contract: hookSpecificOutput.additionalContext per Claude Code's
PostToolUse format. Empty stdout + exit 0 = nothing injected.

SAFETY: fail open on ANY error. Never exits non-zero. Never emits malformed
JSON. The matcher in settings.json already scopes this to ExitPlanMode; the
tool_name check below is a defensive no-op guard, not the gate.
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


def main() -> int:
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    # Defensive: the settings.json matcher is the real gate. Only no-op when the
    # payload positively names a DIFFERENT tool.
    try:
        tool_name = json.loads(raw).get("tool_name") if raw.strip() else None
    except Exception:
        tool_name = None
    if tool_name and tool_name != "ExitPlanMode":
        return 0
    try:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": REMINDER,
            }
        }))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
