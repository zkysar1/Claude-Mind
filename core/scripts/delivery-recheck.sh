#!/usr/bin/env bash
# delivery-recheck.sh — bash wrapper for the  delivery-hold release sweep.
#
# The read-time half of the delivery gate: re-probes every dependent held because
# its (already-terminal) blocker's deliverable was not reachable, and releases
# the ones that have since landed on origin/main. Without this the hold is
# permanent — both gate sites fire only on a predecessor's terminal TRANSITION,
# which a held dependent's predecessor has already made.
#
# Usage:
#   bash delivery-recheck.sh            # dry-run report (default)
#   bash delivery-recheck.sh --apply    # perform the releases
#
# See delivery-recheck.py docstring for full semantics and stdout JSON shape.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/delivery-recheck.py" "$@"
