#!/usr/bin/env bash
# Scorer-override detective audit wrapper — Scorer Sovereignty Layer C ().
#
# Runs the pure audit engine (scorer-override-audit.py) and, on HITS, files an
# Investigate goal into . This wrapper is the "file a goal" half of Layer
# C (the engine is side-effect-free; same split as aspirations-rejection-audit.py).
# The recurring goal (interval 24h, ) invokes THIS wrapper — the script IS
# the offload (the LLM only reads the report the filed Investigate goal carries).
#
# Usage: scorer-override-audit.sh [since_hours]                    (default 24)
#        scorer-override-audit.sh --derive-from <goal-id> [floor_hours]
#
# --derive-from (): a FIXED lookback silently stops covering its own
# gap the moment the scorer demotes the goal that runs it. If this audit's
# recurring goal declares 24h but has not actually fired in 107h, `--since 24`
# examines 24h and reports clean over an 83h hole — and the "clean" is what
# keeps it demoted, so the hole widens on its own. Deriving the window from the
# ACHIEVED interval closes the loop; the positional form stays supported so
# every existing caller and hand-run keeps working unchanged.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DERIVE_FROM=""
POSITIONAL=""
while [ $# -gt 0 ]; do
    case "$1" in
        --derive-from) DERIVE_FROM="${2:-}"; shift 2 ;;
        *)             POSITIONAL="$1";      shift ;;
    esac
done
SINCE="${POSITIONAL:-24}"

if [ -n "$DERIVE_FROM" ]; then
    # derive-lookback.py is fail-open by contract: on any error it prints
    # --default and exits 0, so this can only widen the window, never narrow
    # or break it. stderr is deliberately NOT discarded — it carries the
    # one-line reason for the chosen window, and a window nobody can explain
    # is the condition this flag exists to end (guard-1675).
    derived="$(py -3 "$SCRIPT_DIR/derive-lookback.py" \
                   --goal-id "$DERIVE_FROM" --default "$SINCE" || true)"
    if printf '%s' "$derived" | grep -Eq '^[0-9]+$' && [ "$derived" -ge "$SINCE" ]; then
        SINCE="$derived"
    else
        # Loud, not silent: falling back to the floor is the OLD behaviour, so
        # a quiet fallback would restore the exact blindness being fixed.
        echo "[scorer-override-audit] WARN: derive-lookback returned '${derived}' " \
             "(not an integer >= floor ${SINCE}) — using floor ${SINCE}h" >&2
    fi
fi

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
