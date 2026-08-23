#!/usr/bin/env bash
# hot-path-size-gate.sh — the always-loaded prose surface may not grow ().
#
# Thin wrapper over core/scripts/hot-path-size-gate.py. The GATE itself is run by
# core/githooks/commit-msg (which calls the .py directly with the message file);
# this wrapper is the human/verify-learning entry point:
#
#   bash core/scripts/hot-path-size-gate.sh --check              # HEAD report + audit-baselines ratchet
#   bash core/scripts/hot-path-size-gate.sh --check --no-ratchet # read only
#   bash core/scripts/hot-path-size-gate.sh --explain <path>     # which set / cap a path gets now
#
# Set definition + caps: core/config/hot-path-budget.yaml.
# Convention: core/config/conventions/hot-path-size-budget.md.
# Exit: 0 (report), 1 only with --hard-gate on a FAIL line.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"

# : Git Bash on Windows returns POSIX /c/... from $(pwd); Windows
# python3 reads that as C:\c\... — convert. Linux/macOS lack cygpath: no-op.
if command -v cygpath >/dev/null 2>&1; then
    SCRIPT_DIR_NATIVE="$(cygpath -w "$SCRIPT_DIR")"
else
    SCRIPT_DIR_NATIVE="$SCRIPT_DIR"
fi

exec python3 "$SCRIPT_DIR_NATIVE/hot-path-size-gate.py" "$@"
