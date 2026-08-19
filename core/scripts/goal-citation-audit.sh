#!/usr/bin/env bash
# goal-citation-audit.sh — wrapper for goal-citation-audit.py (gap-119, ).
#
# Audits the file citations in goal descriptions:
#   SPATIAL  — does the cited path exist, and contain the token it is cited for?
#   TEMPORAL — has the cited path changed since the goal was written?
#
# Report-only. Never edits a goal. See the .py docstring for the coverage
# limits, which are load-bearing: the temporal half covers git-tracked paths
# only, so world/ and meta/ citations report `no-history`, NOT `unchanged`.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/_paths.sh"
exec py -3 "$SCRIPT_DIR/goal-citation-audit.py" "$@"
