#!/usr/bin/env python3
"""PostToolUse hook — plan-completion verdict reminder (two harness shapes).

Plants the completion obligation in the model's recent context mechanically,
so delivering the verdict does not depend on the model remembering a rule
under context pressure (2026-09-05 observation: agents end plan execution on
"plan finished", leave the plan in place, and never answer the question the
user originally asked). The always-loaded rule is
.claude/rules/plan-completion-verdict.md; this hook is its mechanical booster
at the structural moments each harness exposes:

* ExitPlanMode (Claude Code plan mode) — the APPROVAL gate. Nothing marks
  execution's END, so the reminder is planted at its START.
* A task-network plan tool (settings.json matcher `update_plan`; on the wire
  the tool is named by its Claude Code counterpart, `TodoWrite`, and the
  payload carries the tool's rendered `output`). Its rendered header is
  `Current plan (F/T steps done):`, and F == T is exactly the network's
  completion predicate (terminal leaves over all leaves), so the reminder
  fires at the moment the plan COMPLETES — once. "Plan cleared." renders no
  header, so the clear the reminder asks for cannot re-trigger it. A payload
  without `output` (a Claude Code TodoWrite, should anyone route it here) is
  silent by construction.

Output contract: hookSpecificOutput.additionalContext per Claude Code's
PostToolUse format. Empty stdout + exit 0 = nothing injected.

SAFETY: fail open on ANY error. Never exits non-zero. Never emits malformed
JSON. The settings.json matchers are the real gate; the tool_name checks
below are defensive no-op guards.
"""
import json
import re
import sys

PLAN_TOOL_NAMES = ("TodoWrite", "update_plan")
_PLAN_HEADER_RE = re.compile(r"^Current plan \((\d+)/(\d+) steps done\):", re.M)

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

COMPLETE_REMINDER = (
    "<system-reminder>\n"
    "[plan-completion-verdict] Plan COMPLETE — every step is terminal. The plan\n"
    "was a MEANS, not the deliverable. Before you end this turn:\n"
    "  1. CLEAR the plan: call update_plan with tasks: [] so a finished checklist\n"
    "     does not linger as active work.\n"
    "  2. RE-READ the user's ORIGINAL request — the message that started this\n"
    "     task, not the last step's title.\n"
    "  3. ANSWER that original request with a conclusion/verdict, leading with\n"
    "     the answer. The steps are supporting detail, not the headline.\n"
    "NEVER end on \"plan finished\" / \"all steps complete\" / \"no further action\n"
    "needed\" — that hands the user a finished checklist and an unanswered question.\n"
    "Rule: .claude/rules/plan-completion-verdict.md\n"
    "</system-reminder>"
)


def _plan_just_completed(payload: dict) -> bool:
    """True iff a task-network plan tool's rendered output shows F/T with F == T > 0."""
    output = payload.get("output")
    if not isinstance(output, str):
        return False
    m = _PLAN_HEADER_RE.search(output)
    if not m:
        return False
    finished, total = int(m.group(1)), int(m.group(2))
    return total > 0 and finished == total


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
    # Defensive: the settings.json matchers are the real gate. Only no-op when the
    # payload positively names a DIFFERENT tool.
    payload: dict = {}
    try:
        parsed = json.loads(raw) if raw.strip() else None
        payload = parsed if isinstance(parsed, dict) else {}
    except Exception:
        payload = {}
    tool_name = payload.get("tool_name")
    if tool_name in PLAN_TOOL_NAMES:
        if _plan_just_completed(payload):
            _emit(COMPLETE_REMINDER)
        return 0
    if tool_name and tool_name != "ExitPlanMode":
        return 0
    _emit(REMINDER)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
