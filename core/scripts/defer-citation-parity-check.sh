#!/usr/bin/env bash
# Flag a defer_reason that NAMES a dependency goal in a form
# defer-recheck._extract_dep_ids cannot capture — the defer then never clears
# and the goal churns back into the pool on the fail-open TTL forever.
# See defer-citation-parity-check.py for the full docstring. Report-only:
# there is no --apply and none may be added.
#
# Usage: defer-citation-parity-check.sh [--output json|human]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/defer-citation-parity-check.py" "$@"
