#!/usr/bin/env bash
# completion-digest.sh -- build the USER-FACING digest the completion report
# emails (thin wrapper over completion_digest.py). The on-disk
# COMPLETION-REPORT.md stays the agent-facing archive; this is what the user reads.
#   completion-digest.sh --agent <name> [--since ISO] [--notes-file F] [--out F] [--max-items N] [--json]
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"
#  fix: under Git Bash on Windows, $(cd ... && pwd) returns POSIX
# form /c/... Windows python3 misinterprets that as drive C: with a literal
# subdir c/, yielding FileNotFoundError on C:\c\...\completion_digest.py.
# Convert to Windows-native form before exec. Linux/macOS lack cygpath and
# fall through with SCRIPT_DIR unchanged (POSIX paths work natively).
if command -v cygpath >/dev/null 2>&1; then SCRIPT_DIR_NATIVE="$(cygpath -w "$SCRIPT_DIR")"; else SCRIPT_DIR_NATIVE="$SCRIPT_DIR"; fi
exec python3 "$SCRIPT_DIR_NATIVE/completion_digest.py" "$@"
