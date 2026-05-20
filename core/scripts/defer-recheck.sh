#!/usr/bin/env bash
# Re-examine aged goal defers whose reason names a dependency goal that has
# since completed. See defer-recheck.py for the full docstring. Dry-run by
# default; --apply actually clears defer_reason.
#
# Usage: defer-recheck.sh [--max-age-hours N] [--apply] [--output json|human]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/defer-recheck.py" "$@"
