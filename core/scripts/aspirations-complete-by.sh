#!/usr/bin/env bash
# Mark a goal completed with agent attribution.
# Usage: aspirations-complete-by.sh [--source world|agent] <goal-id> [agent-name]
# Agent name defaults to AYOAI_AGENT env var.
# Recurring goals cycle back to pending with updated tracking fields.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
# Support --source flag before positional args
SOURCE_ARGS=""
while [[ "${1:-}" == --source ]]; do
    SOURCE_ARGS="--source $2"
    shift 2
done
GOAL_ID="${1:?Usage: aspirations-complete-by.sh [--source world|agent] <goal-id> [agent-name]}"
AGENT="${2:-$AGENT_NAME}"
if [ -z "$AGENT" ]; then
    echo "Error: No agent name provided and AYOAI_AGENT not set" >&2
    exit 1
fi
# shellcheck disable=SC2086
exec python3 "$CORE_ROOT/scripts/aspirations.py" $SOURCE_ARGS complete-by "$GOAL_ID" "$AGENT"
