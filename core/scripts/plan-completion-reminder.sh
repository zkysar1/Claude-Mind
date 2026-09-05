#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-tool-call latency budget / hook critical path. Keep local: never add MCP or remote-service indirection here.
# PostToolUse[ExitPlanMode | update_plan] hook — plan-completion verdict reminder.
#
# Thin bash wrapper. The Python body lives in plan-completion-reminder.py
# because a heredoc on `python -` would consume stdin before
# json.load(sys.stdin) runs — same pattern as iteration-close-reminder.sh.
#
# Fires AFTER a plan is approved (ExitPlanMode), or the moment a task-network
# plan tool renders every step terminal (matcher update_plan; wire name
# TodoWrite), injecting a <system-reminder> that commands: CLEAR the plan and
# ANSWER the user's original request with a verdict — never end on "plan
# finished". Payload-shape detail: the .py docstring.
# Rule: .claude/rules/plan-completion-verdict.md
#
# SAFETY: fail open on ANY error. Never exits non-zero. Never emits malformed
# JSON. Empty stdout + exit 0 = "no additional context injected" per Claude
# Code's PostToolUse hook contract.
# Source _paths.sh + _platform.sh: required on Windows so `python3` resolves
# to core/scripts/.python-shim/python3 (dispatches to `py`) AND so PROJECT_ROOT
# is in cygpath (C:/...) form. Do not "simplify" by removing either source.
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh" 2>/dev/null || exit 0
source "$(cd "$(dirname "$0")" && pwd)/_platform.sh" 2>/dev/null || exit 0
export PROJECT_ROOT
SCRIPT_PATH="$PROJECT_ROOT/core/scripts/plan-completion-reminder.py"
[ -f "$SCRIPT_PATH" ] || exit 0
python3 "$SCRIPT_PATH" 2>/dev/null
exit 0
