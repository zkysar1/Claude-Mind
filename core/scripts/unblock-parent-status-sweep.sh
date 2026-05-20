#!/usr/bin/env bash
# Detect Layer D auto-Unblock goals whose parent goal has landed in a
# terminal non-execution state (skipped/completed/superseded/archived).
# See unblock-parent-status-sweep.py for the full docstring. Report-only by
# default; --apply marks candidates as skipped with outcome_note
# "parent resolved without action needed".
#
# Usage: unblock-parent-status-sweep.sh [--max-age-hours N] [--apply] \
#                                       [--output json|human]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/unblock-parent-status-sweep.py" "$@"
