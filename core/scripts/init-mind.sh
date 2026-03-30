#!/usr/bin/env bash
# init-mind.sh — Legacy wrapper for 4-tier initialization
#
# In the old architecture, mind/ held everything. Now:
#   world/         — Collective domain state  (init-world.sh)
#   <agent-name>/  — Per-agent private state  (init-agent.sh)
#   meta/          — Meta-strategies          (init-meta.sh)
#
# This wrapper calls all three for backward compatibility.
# New code should call init-world.sh + init-agent.sh + init-meta.sh directly.
#
# Usage:
#   AYOAI_AGENT=<name> bash core/scripts/init-mind.sh
#   bash core/scripts/init-mind.sh <agent-name>

set -euo pipefail

source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"

# Accept agent name as argument or from environment
AGENT_NAME_ARG="${1:-${AYOAI_AGENT:-}}"
if [ -z "$AGENT_NAME_ARG" ]; then
    echo "ERROR: Agent name required." >&2
    echo "Usage: bash core/scripts/init-mind.sh <agent-name>" >&2
    echo "   or: AYOAI_AGENT=<name> bash core/scripts/init-mind.sh" >&2
    exit 1
fi

export AYOAI_AGENT="$AGENT_NAME_ARG"

echo "=== Full initialization (world + agent + meta) ==="
echo ""

# 1. Initialize collective domain state
bash "$CORE_ROOT/scripts/init-world.sh"
echo ""

# 2. Initialize per-agent private state
bash "$CORE_ROOT/scripts/init-agent.sh" "$AGENT_NAME_ARG"
echo ""

# 3. Initialize meta-strategies
bash "$CORE_ROOT/scripts/init-meta.sh"
echo ""

echo "=== Full initialization complete ==="
