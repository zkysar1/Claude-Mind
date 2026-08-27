#!/usr/bin/env bash
# Conclusion-Record — thin wrapper for conclusion-record.py.
# Appends one structured entry to the `conclusions` WM slot. Invoked from
# CREATE_BLOCKER Phase 2.56 after blocker-create-gate passes. Fails quiet:
# a blocker-creation flow must not be blocked by conclusion-record errors.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/conclusion-record.py" "$@"
