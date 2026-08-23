#!/usr/bin/env bash
# Startup surface for lapsed lane-pin review dates ().
#
# Prints a confirm-or-retire prompt when a pin on this agent is past its
# `review_by` date, and keeps printing at every startup until a human edits the
# registry row. The pin stays FULLY ENFORCED throughout -- this never retires,
# voids, or weakens a pin, and the claim gate does not consult it.
#
# ALWAYS exits 0: a startup advisory that can break startup gets removed from
# startup, and then it advises nobody.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || true
python3 "$SCRIPT_DIR/lane_pin_review.py" "$@" || true
exit 0
