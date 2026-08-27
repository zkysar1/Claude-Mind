#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# PostToolUse[Bash] hook — iteration-close loop-continuity reminder.
#
# Thin bash wrapper. The Python body lives in iteration-close-reminder.py
# because a heredoc on `python -` would consume stdin before
# json.load(sys.stdin) runs — same pattern as bash-agent-inject.sh.
#
# Fires AFTER iteration-close.sh --phase productivity-check OR
# recurring-close.sh completes, injecting a <system-reminder> into the
# model's context that commands Skill(aspirations) args='loop' as the next
# tool call. See iteration-close-reminder.py for full rationale.
#
# SAFETY: fail open on ANY error. Never exits non-zero. Never emits malformed
# JSON. Empty stdout + exit 0 = "no additional context injected" per Claude
# Code's PostToolUse hook contract.

# Source _paths.sh + _platform.sh: required on Windows so `python3` resolves
# to core/scripts/.python-shim/python3 (dispatches to `py`) AND so PROJECT_ROOT
# is in cygpath (C:/...) form. All other hooks in this repo follow the same
# two-source pattern. Do not "simplify" by removing either source.
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh" 2>/dev/null || exit 0
source "$(cd "$(dirname "$0")" && pwd)/_platform.sh" 2>/dev/null || exit 0
export PROJECT_ROOT

SCRIPT_PATH="$PROJECT_ROOT/core/scripts/iteration-close-reminder.py"
[ -f "$SCRIPT_PATH" ] || exit 0

python3 "$SCRIPT_PATH" 2>/dev/null
exit 0
