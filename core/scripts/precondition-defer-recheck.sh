#!/usr/bin/env bash
# Re-evaluate structured-precondition defers and clear when ALL pass.
# See precondition-defer-recheck.py docstring for full semantics.
# Sibling pattern: defer-recheck.sh, blocker-recheck.sh, monitor-stale-check.sh.
#
# Usage: precondition-defer-recheck.sh [--max-age-hours N] [--apply]
#                                      [--output json|human] [--metrics-log PATH|""]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/precondition-defer-recheck.py" "$@"
