#!/usr/bin/env bash
# Claim a world goal for the current agent.
# Usage: aspirations-claim.sh <goal-id> [agent-name]
# Agent name defaults to AYOAI_AGENT env var.
# Exits non-zero if already claimed by a different agent.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
GOAL_ID="${1:?Usage: aspirations-claim.sh <goal-id> [agent-name]}"
AGENT="${2:-$AGENT_NAME}"
if [ -z "$AGENT" ]; then
    echo "Error: No agent name provided and AYOAI_AGENT not set" >&2
    exit 1
fi
exec python3 "$CORE_ROOT/scripts/aspirations.py" claim "$GOAL_ID" "$AGENT"
