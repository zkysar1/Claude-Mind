#!/usr/bin/env bash
# Flag status=blocked goals with an EMPTY Blocker Reference Schema (no
# blocker_ref, no blocked_by, no defer_reason) — a violation that escapes
# selection, blocker-recheck, AND quiescence. With --apply, files ONE
# deduplicated reconcile Investigate. See reason-less-blocked-check.py docstring
# for full semantics and the  /  lineage.
# Sibling pattern: defer-drift-check.sh (detective), blocker-recheck.sh (apply).
#
# Usage: reason-less-blocked-check.sh [--apply] [--output json|human]
#                                     [--investigate-aspiration asp-NNN]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/reason-less-blocked-check.py" "$@"
