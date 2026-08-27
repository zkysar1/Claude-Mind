#!/usr/bin/env bash
# session-digest.sh --  (, FW-9).
#
# Thin entry point for the unified "what changed since last session" orientation
# digest: ONE read covering handoff, own goal mix, recent commits, board,
# team-state, and the agent's own recent journal. READ-ONLY + FAIL-OPEN -- all
# logic (and its tests) live in session-digest.py.
#
# python3 resolves via the shim once _paths.sh is sourced (Windows MS-Store
# stub guard, per .claude/rules/python-invocation conventions).
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || true

#  fix: under Git Bash on Windows, $(cd ... && pwd) returns POSIX
# form /c/... Windows python3 misinterprets that as drive C: with a literal
# subdir c/, yielding FileNotFoundError on C:\c\...\session-digest.py.
# Convert to Windows-native form before exec. Linux/macOS lack cygpath and
# fall through with SCRIPT_DIR unchanged (POSIX paths work natively).
if command -v cygpath >/dev/null 2>&1; then
    SCRIPT_DIR_NATIVE="$(cygpath -w "$SCRIPT_DIR")"
else
    SCRIPT_DIR_NATIVE="$SCRIPT_DIR"
fi

exec python3 "$SCRIPT_DIR_NATIVE/session-digest.py" "$@"
