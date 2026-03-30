#!/usr/bin/env bash
# FileChanged hook — propagate agent binding to session env.
#
# Watches .active-agent-* files. When /start writes
# the agent name to .active-agent-<session_id>, this hook writes
# export AYOAI_AGENT="<name>" to CLAUDE_ENV_FILE so all subsequent
# Bash calls in this session resolve to the correct agent.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"

# Extract this session's ID from hook stdin JSON
SID=$(python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")
[ -n "$SID" ] || exit 0

ACTIVE_FILE="$PROJECT_ROOT/.active-agent-$SID"

# Only act if THIS session's agent file exists and has content.
# Do NOT clear on absence — another session's file change is irrelevant to us.
if [ -f "$ACTIVE_FILE" ] && [ -s "$ACTIVE_FILE" ]; then
    AGENT_NAME=$(cat "$ACTIVE_FILE" | tr -d '\r\n')
    if [ -n "$AGENT_NAME" ] && [ -n "${CLAUDE_ENV_FILE:-}" ]; then
        echo "export AYOAI_AGENT=\"$AGENT_NAME\"" >> "$CLAUDE_ENV_FILE"
    fi
fi
