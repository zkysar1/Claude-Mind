#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Entry sentinel for hook-fire-audit (g-115-636) — FIRST executable line,
# bash-builtin only, fail-open. mtime of core/logs/hook-fires/trailing-echo-exit-gate
# = last fire of this hook.
{ _HF_DIR="${BASH_SOURCE[0]%/*}/../.." ; mkdir -p "$_HF_DIR/core/logs/hook-fires" 2>/dev/null && : > "$_HF_DIR/core/logs/hook-fires/trailing-echo-exit-gate" 2>/dev/null ; unset _HF_DIR ; } 2>/dev/null || true

# PreToolUse[Bash] ADVISORY hook — Layer B of the guard-1150 defense
# (goal g-115-3511). Warns when a BACKGROUNDED command's last statement is an
# `echo`/`printf`, which makes the shell — and therefore the task-completion
# notification — report that statement's status instead of the command's.
#
# Thin bash wrapper. The Python body lives in trailing-echo-exit-gate.py
# because a heredoc on `python -` would consume stdin before json.load runs
# (same reason as bare-bash-authoring-gate.sh / bash-path-resolution-hook.sh).
#
# POSTURE: advisory, not blocking. The value is the visible stderr banner
# delivered at the moment of use — the one thing a stored guardrail cannot do.
#
# SAFETY: fail open on ANY error. Never exits non-zero (guard-591: the wrapper
# must never propagate the Python exit code). Never writes to stdout — Claude
# Code interprets hook stdout as a deny payload, and this gate never denies.
# Empty stdout + exit 0 = "approve with no mutation".

source "$(cd "$(dirname "$0")" && pwd)/_paths.sh" 2>/dev/null || exit 0
source "$(cd "$(dirname "$0")" && pwd)/_platform.sh" 2>/dev/null || exit 0
export PROJECT_ROOT

SCRIPT_PATH="$PROJECT_ROOT/core/scripts/trailing-echo-exit-gate.py"
[ -f "$SCRIPT_PATH" ] || exit 0

# DO NOT add `2>/dev/null` here. The sibling gates suppress the Python body's
# stderr because their output channel is stdout (a deny payload). This gate is
# ADVISORY: its entire value IS the stderr banner, so suppressing stderr would
# leave a hook that fires, costs latency, and communicates nothing — failing
# silently in the one way nobody would notice. The advisory is deliberately on
# stderr rather than stdout so it can never be mistaken for a deny payload
# under ANY invocation path, including a direct call to the .py.
python3 "$SCRIPT_PATH"
exit 0
