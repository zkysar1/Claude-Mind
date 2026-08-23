#!/usr/bin/env bash
# Flag goals whose deferred_until is PAST while a structured-defer marker
# persists (deferred_readiness selector pollution). Detective only — never
# mutates goal state. See defer-drift-check.py docstring for full semantics
# and the canonical  incident.
# Sibling pattern: precondition-defer-recheck.sh, defer-recheck.sh.
#
# Usage: defer-drift-check.sh [--output json|human] [--min-hours-past N]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/defer-drift-check.py" "$@"
