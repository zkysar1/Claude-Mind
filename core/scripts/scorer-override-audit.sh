#!/usr/bin/env bash
# Scorer-override detective audit wrapper — Scorer Sovereignty Layer C ().
#
# Runs the pure audit engine (scorer-override-audit.py) and, on HITS, files an
# Investigate goal into . This wrapper is the "file a goal" half of Layer
# C (the engine is side-effect-free; same split as aspirations-rejection-audit.py).
# The recurring goal (interval 24h, ) invokes THIS wrapper — the script IS
# the offload (the LLM only reads the report the filed Investigate goal carries).
#
# Usage: scorer-override-audit.sh [since_hours]   (default 24)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SINCE="${1:-24}"

# The engine builds the Investigate-goal JSON itself (testable, no fragile bash
# JSON) and prints it ONLY on hits; clean windows print nothing.
goal_json="$(py -3 "$SCRIPT_DIR/scorer-override-audit.py" --since-hours "$SINCE" --emit-investigate-goal || true)"

if [ -n "$goal_json" ]; then
    printf '%s' "$goal_json" | bash "$SCRIPT_DIR/aspirations-add-goal.sh" --source world asp-115
    echo "[scorer-override-audit] HITS in ${SINCE}h — filed Investigate goal into asp-115"
else
    # Also print the human report for the log/operator when clean.
    py -3 "$SCRIPT_DIR/scorer-override-audit.py" --since-hours "$SINCE"
    echo "[scorer-override-audit] clean (no hits in ${SINCE}h) — no goal filed"
fi
