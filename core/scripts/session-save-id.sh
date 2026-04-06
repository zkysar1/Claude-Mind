#!/usr/bin/env bash
# SessionStart hook — carry forward agent binding across autocompact.
#
# 1. Resolves agent via .active-agent-$SID or <agent>/session/compact-pending breadcrumb
# 2. Writes SID to <agent>/session/latest-session-id (+ syncs running-session-id)
#
# .latest-session-id is the bridge — written here, read ONLY by /start.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"

# Extract session_id from hook stdin JSON
SID=$(python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")
[ -n "$SID" ] || exit 0

# --- 1. Bridge hook SID to LLM ---
# .latest-session-id is the ONLY shared file. Written here, read ONLY by /start.
# /start reads it once to create per-agent session bindings, then everything
# else uses those per-agent files. The concurrent race window is seconds
# (between session open and user typing /start) — acceptable.
echo "$SID" > "$PROJECT_ROOT/.latest-session-id.tmp" && mv "$PROJECT_ROOT/.latest-session-id.tmp" "$PROJECT_ROOT/.latest-session-id"

# --- Breadcrumb from stop hook (lives in each agent's session dir — no shared files) ---
# WHY: The stop hook wrote the OLD SID to <agent>/session/compact-pending before blocking.
# If that OLD SID matches running-session-id, this is the agent that just compacted.
# We update running-session-id to the NEW SID so the stop hook recognizes us next time.
# Each agent's breadcrumb is in its own directory — no multi-agent race possible.
COMPACT_AGENT=""
for _CP in "$PROJECT_ROOT"/*/session/compact-pending; do
    [ -f "$_CP" ] || continue
    _SESSION_DIR=$(dirname "$_CP")
    _OLD_SID=$(cat "$_CP" 2>/dev/null | tr -d '\r\n')
    _RUNNER_SID=$(cat "$_SESSION_DIR/running-session-id" 2>/dev/null | tr -d '\r\n')
    if [ -n "$_OLD_SID" ] && [ "$_OLD_SID" = "$_RUNNER_SID" ]; then
        COMPACT_AGENT=$(basename "$(dirname "$_SESSION_DIR")")
        rm -f "$_CP"
        break
    fi
done
unset _CP _SESSION_DIR _OLD_SID _RUNNER_SID

# --- 2. Resolve agent binding for the new SID ---
# Priority: existing binding > breadcrumb
ACTIVE_FILE="$PROJECT_ROOT/.active-agent-$SID"
RESTORED_AGENT=""
if [ -f "$ACTIVE_FILE" ] && [ -s "$ACTIVE_FILE" ]; then
    # Session reconnect: .active-agent-$SID already exists
    RESTORED_AGENT=$(cat "$ACTIVE_FILE" | tr -d '\r\n')
elif [ -n "$COMPACT_AGENT" ] && [ -d "$PROJECT_ROOT/$COMPACT_AGENT" ]; then
    # Breadcrumb: stop hook identified the correct agent for this autocompact
    echo "$COMPACT_AGENT" > "$PROJECT_ROOT/.active-agent-$SID"
    RESTORED_AGENT="$COMPACT_AGENT"
fi

# --- 3. Write SID to agent session dir (if agent is active) ---
RESOLVED_AGENT="${COMPACT_AGENT:-${AYOAI_AGENT:-${RESTORED_AGENT:-}}}"
if [ -n "$RESOLVED_AGENT" ]; then
    AGENT_SESSION_DIR="$PROJECT_ROOT/$RESOLVED_AGENT/session"
    if [ -d "$AGENT_SESSION_DIR" ]; then
        echo "$SID" > "$AGENT_SESSION_DIR/latest-session-id"
        # Only sync running-session-id during autocompact (COMPACT_AGENT confirms this
        # IS the runner session). Carry-forward sessions must NOT overwrite — opening a
        # non-runner window would steal the runner's SID and kill the actual loop.
        if [ -n "$COMPACT_AGENT" ] && [ -f "$AGENT_SESSION_DIR/running-session-id" ]; then
            echo "$SID" > "$AGENT_SESSION_DIR/running-session-id"
        fi
    fi
fi
