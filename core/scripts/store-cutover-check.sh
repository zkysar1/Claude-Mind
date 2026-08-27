#!/usr/bin/env bash
# Parametrized store-cutover ordering gate with derived attestation ( item 3).
#
#   bash core/scripts/store-cutover-check.sh --store utilization            # is the flip safe?
#   bash core/scripts/store-cutover-check.sh --store utilization --attest   # fallback hand-stamp
#
# Exit 0 = SAFE (every live agent proven or stamped) · 2 = UNSAFE · 3 = error.
# Fail-CLOSED: anything unreadable reports UNSAFE, never SAFE.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"

# : under Git Bash on Windows, `$(cd ... && pwd)` yields POSIX /c/...
# which Windows python3 reads as drive C: plus a literal `c/` subdir, so the
# script path resolves to C:\c\...\<name>.py and dies FileNotFoundError.
# Convert to Windows-native form before exec. Linux/macOS have no cygpath and
# fall through unchanged (POSIX paths work natively there).
if command -v cygpath >/dev/null 2>&1; then
    SCRIPT_DIR_NATIVE="$(cygpath -w "$SCRIPT_DIR")"
else
    SCRIPT_DIR_NATIVE="$SCRIPT_DIR"
fi

exec python3 "$SCRIPT_DIR_NATIVE/store-cutover-check.py" "$@"
