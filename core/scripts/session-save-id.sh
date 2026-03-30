#!/usr/bin/env bash
# SessionStart hook — save session UUID and set agent env var.
#
# 1. Writes AYOAI_SESSION_ID to CLAUDE_ENV_FILE (persists to all Bash calls)
# 2. Auto-resumes agent if .active-agent-<session_id> exists (session reconnect)
# 3. Writes session_id to <agent>/session/latest-session-id (+ syncs running-session-id)
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"

# Extract session_id from hook stdin JSON
SID=$(python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")
[ -n "$SID" ] || exit 0

# --- 1. Persist session ID to env (all Bash calls will have $AYOAI_SESSION_ID) ---
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
    echo "export AYOAI_SESSION_ID=\"$SID\"" > "$CLAUDE_ENV_FILE"
fi

# --- 2. Auto-resume: restore agent binding from previous session ---
# Skip if agent directory was deleted (user removed the agent).
ACTIVE_FILE="$PROJECT_ROOT/.active-agent-$SID"
if [ -f "$ACTIVE_FILE" ] && [ -s "$ACTIVE_FILE" ]; then
    RESTORED_AGENT=$(cat "$ACTIVE_FILE" | tr -d '\r\n')
    if [ -n "$RESTORED_AGENT" ] && [ -d "$PROJECT_ROOT/$RESTORED_AGENT" ] && [ -n "${CLAUDE_ENV_FILE:-}" ]; then
        echo "export AYOAI_AGENT=\"$RESTORED_AGENT\"" >> "$CLAUDE_ENV_FILE"
    fi
fi

# --- 3. Write session_id to agent session dir (if agent is active) ---
# AGENT_DIR may be set from env (AYOAI_AGENT) or from the auto-resume above.
# Re-resolve since CLAUDE_ENV_FILE writes don't take effect until NEXT Bash call.
RESOLVED_AGENT="${AYOAI_AGENT:-${RESTORED_AGENT:-}}"
if [ -n "$RESOLVED_AGENT" ]; then
    AGENT_SESSION_DIR="$PROJECT_ROOT/$RESOLVED_AGENT/session"
    if [ -d "$AGENT_SESSION_DIR" ]; then
        echo "$SID" > "$AGENT_SESSION_DIR/latest-session-id"
        # If the autonomous loop is running, keep running-session-id in sync.
        # Without this, autocompact (which generates a new session UUID) causes
        # Gate 0 in stop-hook.sh to see a mismatch and allow premature stops.
        if [ -f "$AGENT_SESSION_DIR/running-session-id" ]; then
            echo "$SID" > "$AGENT_SESSION_DIR/running-session-id"
        fi
    fi
fi
