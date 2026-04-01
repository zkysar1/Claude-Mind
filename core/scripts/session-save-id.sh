#!/usr/bin/env bash
# SessionStart hook — save session UUID and carry forward agent binding.
#
# 1. Writes SID to .latest-session-id (read by _paths.sh for Tier 2 resolution)
# 2. Carries forward agent binding across autocompact (old SID → new SID)
# 3. Writes SID to <agent>/session/latest-session-id (+ syncs running-session-id)
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"

# Extract session_id from hook stdin JSON
SID=$(python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")
[ -n "$SID" ] || exit 0

# --- 1. Read OLD SID before overwriting, then persist new SID ---
OLD_SID=""
if [ -f "$PROJECT_ROOT/.latest-session-id" ]; then
    OLD_SID=$(cat "$PROJECT_ROOT/.latest-session-id" 2>/dev/null | tr -d '\r\n')
fi
echo "$SID" > "$PROJECT_ROOT/.latest-session-id"

# --- Breadcrumb from stop hook (authoritative for autocompact) ---
# The stop hook resolves the correct agent from .active-agent-$OLD_SID and writes
# .compact-agent. This is more reliable than carry-forward for concurrent agents.
COMPACT_AGENT=""
if [ -f "$PROJECT_ROOT/.compact-agent" ]; then
    COMPACT_AGENT=$(cat "$PROJECT_ROOT/.compact-agent" 2>/dev/null | tr -d '\r\n')
    rm -f "$PROJECT_ROOT/.compact-agent"
fi

# --- 2. Resolve agent binding for the new SID ---
# Priority: existing binding > breadcrumb > carry-forward from old SID
ACTIVE_FILE="$PROJECT_ROOT/.active-agent-$SID"
RESTORED_AGENT=""
if [ -f "$ACTIVE_FILE" ] && [ -s "$ACTIVE_FILE" ]; then
    # Session reconnect: .active-agent-$SID already exists
    RESTORED_AGENT=$(cat "$ACTIVE_FILE" | tr -d '\r\n')
elif [ -n "$COMPACT_AGENT" ] && [ -d "$PROJECT_ROOT/$COMPACT_AGENT" ]; then
    # Breadcrumb: stop hook identified the correct agent for this autocompact
    echo "$COMPACT_AGENT" > "$PROJECT_ROOT/.active-agent-$SID"
    RESTORED_AGENT="$COMPACT_AGENT"
elif [ -n "$OLD_SID" ] && [ -f "$PROJECT_ROOT/.active-agent-$OLD_SID" ]; then
    # Carry-forward: copy old SID's agent to new SID (single-agent reliable)
    # CONCURRENT LIMITATION: OLD_SID from .latest-session-id may be wrong.
    # The LLM prefix (AYOAI_AGENT) compensates for concurrent agents.
    CARRIED_AGENT=$(cat "$PROJECT_ROOT/.active-agent-$OLD_SID" 2>/dev/null | tr -d '\r\n')
    if [ -n "$CARRIED_AGENT" ] && [ -d "$PROJECT_ROOT/$CARRIED_AGENT" ]; then
        echo "$CARRIED_AGENT" > "$PROJECT_ROOT/.active-agent-$SID"
        RESTORED_AGENT="$CARRIED_AGENT"
    fi
fi

# --- 3. Write SID to agent session dir (if agent is active) ---
RESOLVED_AGENT="${COMPACT_AGENT:-${AYOAI_AGENT:-${RESTORED_AGENT:-}}}"
if [ -n "$RESOLVED_AGENT" ]; then
    AGENT_SESSION_DIR="$PROJECT_ROOT/$RESOLVED_AGENT/session"
    if [ -d "$AGENT_SESSION_DIR" ]; then
        echo "$SID" > "$AGENT_SESSION_DIR/latest-session-id"
        # Keep running-session-id in sync across autocompact.
        # Without this, Gate 0 in stop-hook.sh sees a SID mismatch and allows premature stops.
        if [ -f "$AGENT_SESSION_DIR/running-session-id" ]; then
            echo "$SID" > "$AGENT_SESSION_DIR/running-session-id"
        fi
    fi
fi
