#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Entry sentinel for hook-fire-audit () — FIRST executable line,
# bash-builtin only, fail-open. mtime of core/logs/hook-fires/settings-structural-validator
# = last fire of this hook.
{ _HF_DIR="${BASH_SOURCE[0]%/*}/../.." ; mkdir -p "$_HF_DIR/core/logs/hook-fires" 2>/dev/null && : > "$_HF_DIR/core/logs/hook-fires/settings-structural-validator" 2>/dev/null ; unset _HF_DIR ; } 2>/dev/null || true

# PreToolUse[Edit|Write|MultiEdit] — settings.json structural validator.
#
# Thin bash wrapper. Python body in settings-structural-validator.py
# (heredoc on `python -` would consume stdin before json.load runs).
#
# UNLIKE sibling PreToolUse hooks: the python body fails CLOSED on validator
# exceptions because it gates the file that gates the entire safety system
# (bootstrap-paradox defense, rb-931). The bash layer here is conventionally
# fail-open: missing _paths.sh or missing script → approve. That gap is
# bounded — once the python runs, fail-closed kicks in. A missing python
# script would be visible as a missing file in `core/scripts/`, which is
# git-tracked and surfaces in routine review.
#
# Two-source pattern (_paths.sh + _platform.sh): required on Windows so
# python3 resolves to core/scripts/.python-shim/python3 and PROJECT_ROOT is
# in cygpath form. Same as path-resolution-hook.sh — do not simplify.
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh" 2>/dev/null || exit 0
source "$(cd "$(dirname "$0")" && pwd)/_platform.sh" 2>/dev/null || exit 0
export PROJECT_ROOT

SCRIPT_PATH="$PROJECT_ROOT/core/scripts/settings-structural-validator.py"
[ -f "$SCRIPT_PATH" ] || exit 0

python3 "$SCRIPT_PATH" 2>/dev/null
exit 0
