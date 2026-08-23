#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-tool-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Entry sentinel for hook-fire-audit () — FIRST executable line,
# bash-builtin only, fail-open. mtime of core/logs/hook-fires/schedule-wakeup-gate
# = last fire of this hook.
{ _HF_DIR="${BASH_SOURCE[0]%/*}/../.." ; mkdir -p "$_HF_DIR/core/logs/hook-fires" 2>/dev/null && : > "$_HF_DIR/core/logs/hook-fires/schedule-wakeup-gate" 2>/dev/null ; unset _HF_DIR ; } 2>/dev/null || true

# PreToolUse[ScheduleWakeup] hook — refuse slash-command prompts that get
# rejected by Claude Code's user-invocable gate when the wakeup fires.
#
# Canonical incident: zeta (sid f1f3066e), 2026-05-18. LLM called
# ScheduleWakeup four times with prompt="/aspirations loop". All four
# fired and were rejected with "This skill can only be invoked by Claude".
#
# Thin bash wrapper. The Python body lives in schedule-wakeup-gate.py.
# Sources _paths.sh + _platform.sh per project convention so python3
# resolves to the Windows shim and PROJECT_ROOT is in cygpath form.
#
# SAFETY: fail open on ANY error. Never exits non-zero. Empty stdout + exit 0
# = "approve with no mutation" per Claude Code's PreToolUse hook contract.

source "$(cd "$(dirname "$0")" && pwd)/_paths.sh" 2>/dev/null || exit 0
source "$(cd "$(dirname "$0")" && pwd)/_platform.sh" 2>/dev/null || exit 0
export PROJECT_ROOT

SCRIPT_PATH="$PROJECT_ROOT/core/scripts/schedule-wakeup-gate.py"
[ -f "$SCRIPT_PATH" ] || exit 0

python3 "$SCRIPT_PATH" 2>/dev/null
exit 0
