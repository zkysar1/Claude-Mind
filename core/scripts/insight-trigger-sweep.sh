#!/usr/bin/env bash
# core/scripts/insight-trigger-sweep.sh
#
# Bash wrapper for insight-trigger-sweep.py. See the Python script's
# docstring for the full design rationale.
#
# Usage:
#   insight-trigger-sweep.sh                  # sweep + file goals
#   insight-trigger-sweep.sh --dry-run        # probe-only
#   insight-trigger-sweep.sh --dry-run --json # /prime Step 5.5b consumer
#
# Exit code: inherited from the Python sweeper (0 on success including
# no-op no-pending-triggers; non-zero on Python exception).
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_paths.sh"

#  fix: under Git Bash on Windows, $(cd ... && pwd) returns POSIX
# form /c/... Windows python3 misinterprets that as drive C: with a literal
# subdir c/, yielding FileNotFoundError on C:\c\...\insight-trigger-sweep.py.
# Convert to Windows-native form before exec. Linux/macOS lack cygpath and
# fall through with SCRIPT_DIR unchanged (POSIX paths work natively).
if command -v cygpath >/dev/null 2>&1; then
    SCRIPT_DIR_NATIVE="$(cygpath -w "$SCRIPT_DIR")"
else
    SCRIPT_DIR_NATIVE="$SCRIPT_DIR"
fi

# Single python3 invocation per python-invocation.md rule 3: inside a .sh
# wrapper that has sourced _paths.sh, the shim is on PATH and python3 is
# canonical. exec replaces the bash process — output and exit pass through.
exec python3 "$SCRIPT_DIR_NATIVE/insight-trigger-sweep.py" "$@"
