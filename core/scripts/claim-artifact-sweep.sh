#!/usr/bin/env bash
# claim-artifact-sweep.sh — wrapper for claim_artifact_sweep.py
#
# Find EVERY artifact still asserting a claim that a measurement just falsified,
# so the correction lands in ONE change instead of leaving a half-corrected
# state (guard-1710). Read-only: it never edits anything, it produces the
# work-list.
#
# Usage:
#   bash core/scripts/claim-artifact-sweep.sh --tokens "tok1,tok2,tok3" \
#        [--claim "one-line statement of the falsified claim"] \
#        [--min-tokens 2] [--radius 2] [--product-repo /path/to/repo] \
#        [--output json|text] [--show N]
#
# Exit: 0 ok (CLEAN or CORRECTIONS_REQUIRED) · 2 bad args · 3 a surface was
# UNREADABLE (its zero is vacuous — never read it as "clean", rb-245).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || true

exec py -3 "$SCRIPT_DIR/claim_artifact_sweep.py" "$@"
