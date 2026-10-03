#!/usr/bin/env bash
# Flag goals whose deferred_until is PAST while a structured-defer marker
# persists (deferred_readiness selector pollution). Detective by default —
# never mutates existing goal state; --apply files ONE per-member-deduplicated
# re-gate Investigate (). See defer-drift-check.py docstring for
# full semantics and the canonical  incident.
# Sibling pattern: precondition-defer-recheck.sh (detective),
# reason-less-blocked-check.sh (apply family).
#
# Usage: defer-drift-check.sh [--apply] [--output json|human] [--min-hours-past N]
#                             [--investigate-aspiration asp-NNN]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/defer-drift-check.py" "$@"
