#!/usr/bin/env bash
# q4-provenance-sample.sh — Q4 close-time entity-fact provenance sampling ().
#
#   bash core/scripts/q4-provenance-sample.sh --goal <goal-id> --artifact <path> [--artifact <path>...] \
#        [-n N] [--session-id <sid>] [--source-file <path>] [--json]
#
# Exit 0 = pass or skipped (read the verdict — they are NOT the same answer).
# Exit 1 = at least one sampled claim is uncited, decoratively cited, or reversed
#          against its cited source.
# Exit 2 = usage.
#
# The exit code is the answer, so it must pass through untouched — no trailing
# pipe or echo may replace it (guard-1150). Sibling of provenance-check.sh, which
# answers the same question for ONE citation; this answers it for a scripted
# sample of an artifact's claims.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"

exec python3 "$CORE_ROOT/scripts/q4-provenance-sample.py" "$@"
