#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- PreToolUse hook critical path; keep local, no remote/MCP indirection.
# Entry sentinel for hook-fire-audit ().
{ _HF_DIR="${BASH_SOURCE[0]%/*}/../.." ; mkdir -p "$_HF_DIR/core/logs/hook-fires" 2>/dev/null && : > "$_HF_DIR/core/logs/hook-fires/tree-yaml-validate-gate" 2>/dev/null ; unset _HF_DIR ; } 2>/dev/null || true

# PreToolUse[Write|Edit|MultiEdit] hook -- knowledge-tree YAML parse-validation ().
# Thin bash wrapper; Python body in tree-yaml-validate-gate.py (a heredoc on
# `python -` would consume stdin before json.load can read the hook payload).
# SAFETY: fail open on ANY error. Never exits non-zero. Empty stdout + exit 0 =
# approve. Structured JSON on stdout = deny (per the PreToolUse contract).
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh" 2>/dev/null || exit 0
source "$(cd "$(dirname "$0")" && pwd)/_platform.sh" 2>/dev/null || exit 0
export PROJECT_ROOT

SCRIPT_PATH="$PROJECT_ROOT/core/scripts/tree-yaml-validate-gate.py"
[ -f "$SCRIPT_PATH" ] || exit 0

python3 "$SCRIPT_PATH" 2>/dev/null
exit 0
