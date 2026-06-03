#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Entry sentinel for hook-fire-audit () — FIRST executable line,
# bash-builtin only, fail-open. mtime of core/logs/hook-fires/bash-path-resolution-hook
# = last fire of this hook.
{ _HF_DIR="${BASH_SOURCE[0]%/*}/../.." ; mkdir -p "$_HF_DIR/core/logs/hook-fires" 2>/dev/null && : > "$_HF_DIR/core/logs/hook-fires/bash-path-resolution-hook" 2>/dev/null ; unset _HF_DIR ; } 2>/dev/null || true

# PreToolUse[Bash] hook — L1 path-resolution enforcement (Bash sibling).
# Catches Bash commands that would create new top-level entries under
# governed roots (WORLD_PATH, META_PATH, bound agent dir) — the failure mode
# that path-resolution-hook.sh misses because it only fires on Write/Edit/
# MultiEdit. Canonical incident: rotate-lambda-common-pat.sh landing at
# agents/alpha/scripts/ via Bash heredoc (2026-05-21).
#
# Thin bash wrapper. The Python body lives in bash-path-resolution-hook.py
# because a heredoc on `python -` would consume stdin before json.load runs.
#
# SAFETY: fail open on ANY error. Never exits non-zero. Never emits malformed
# JSON. Empty stdout + exit 0 = "approve with no mutation" per Claude Code's
# PreToolUse hook contract.

source "$(cd "$(dirname "$0")" && pwd)/_paths.sh" 2>/dev/null || exit 0
source "$(cd "$(dirname "$0")" && pwd)/_platform.sh" 2>/dev/null || exit 0
export PROJECT_ROOT

SCRIPT_PATH="$PROJECT_ROOT/core/scripts/bash-path-resolution-hook.py"
[ -f "$SCRIPT_PATH" ] || exit 0

python3 "$SCRIPT_PATH" 2>/dev/null
exit 0
