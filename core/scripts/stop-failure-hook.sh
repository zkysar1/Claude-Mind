#!/usr/bin/env bash
# StopFailure hook — crash recovery breadcrumb for context exhaustion
#
# Fires when the session cannot continue (context exhaustion, API error).
# StopFailure is NOTIFICATION-ONLY: stdout and exit code are ignored.
# Cannot block the stop — but can save state for the next session to detect.
#
# Writes <agent>/session/crash-marker so /boot can report the abnormal shutdown.

set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"

# --- Read stdin ---
STDIN_JSON=$(cat)

# --- Resolve agent (same pattern as stop-hook.sh) ---
HOOK_SID=$(printf '%s' "$STDIN_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")
HOOK_AGENT=""
if [ -n "$HOOK_SID" ] && [ -f "$PROJECT_ROOT/.active-agent-$HOOK_SID" ]; then
    HOOK_AGENT=$(cat "$PROJECT_ROOT/.active-agent-$HOOK_SID" 2>/dev/null | tr -d '\r\n')
fi
HOOK_AGENT="${HOOK_AGENT:-$AGENT_NAME}"

# --- Write crash marker ---
if [ -n "$HOOK_AGENT" ]; then
    AGENT_SESSION_DIR="$PROJECT_ROOT/$HOOK_AGENT/session"
    if [ -d "$AGENT_SESSION_DIR" ]; then
        echo "$(date +%Y-%m-%dT%H:%M:%S) context_exhaustion sid=$HOOK_SID" > "$AGENT_SESSION_DIR/crash-marker"
    fi
    # Best-effort insight capture
    printf '%s' "$STDIN_JSON" | AYOAI_AGENT="$HOOK_AGENT" python3 "$CORE_ROOT/scripts/capture-insights.py" 2>/dev/null || true
fi

# --- Housekeeping ---
find "$PROJECT_ROOT" -maxdepth 1 -name '.active-agent-*' -mmin +1440 -delete 2>/dev/null || true
rm -f "$PROJECT_ROOT/.stop-hook-stdin.json"

exit 0
