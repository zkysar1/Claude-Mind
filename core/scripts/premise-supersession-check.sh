#!/usr/bin/env bash
# premise-supersession-check.sh — is the REASON for this goal still true? (gap-142)
# Thin exec passthrough; sources _paths.sh so the child gets the resolved world/meta env.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"

#  fix: under Git Bash on Windows, $(cd ... && pwd) returns POSIX
# form /c/... Windows python3 misinterprets that as drive C: with a literal
# subdir c/, yielding FileNotFoundError on C:\c\...\premise_supersession_check.py.
# Convert to Windows-native form before exec. Linux/macOS lack cygpath and
# fall through with SCRIPT_DIR unchanged (POSIX paths work natively).
if command -v cygpath >/dev/null 2>&1; then
    SCRIPT_DIR_NATIVE="$(cygpath -w "$SCRIPT_DIR")"
else
    SCRIPT_DIR_NATIVE="$SCRIPT_DIR"
fi

exec python3 "$SCRIPT_DIR_NATIVE/premise_supersession_check.py" --root "$PROJECT_ROOT" "$@"
