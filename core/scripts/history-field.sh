#!/usr/bin/env bash
# history-field.sh — read ONE record's ONE field from a historical snapshot.
# Pure exec passthrough: sources _paths.sh so world/ and meta/ virtual prefixes
# resolve, then execs history-field.py with "$@" unchanged. Read-only — see the
# .py docstring for why this is NOT the restore CLI (guard-4165 / guard-5651).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"
#  fix: under Git Bash on Windows, $(cd ... && pwd) returns POSIX
# form /c/... that Windows python3 misreads as C:\c\... Convert to the
# native form before exec; Linux/macOS lack cygpath and fall through.
if command -v cygpath >/dev/null 2>&1; then
    SCRIPT_DIR_NATIVE="$(cygpath -w "$SCRIPT_DIR")"
else
    SCRIPT_DIR_NATIVE="$SCRIPT_DIR"
fi
exec python3 "$SCRIPT_DIR_NATIVE/history-field.py" "$@"
