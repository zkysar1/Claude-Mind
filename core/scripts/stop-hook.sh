#!/usr/bin/env bash
# Stop hook — keeps the autonomous loop alive
#
# Gates: allow stop when appropriate (SID mismatch, not RUNNING, stop-loop, pending agents)
# Otherwise: BLOCK unconditionally. No counter. No tiers. No safety valve.
# The user has /stop and Ctrl+C. The hook's job is to keep the loop alive.

set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"

# --- Audit log (persistent across sessions — diagnose why sessions die) ---
# NOTE: Under set -e, a failed >> append kills the script (= fail open, allows stop).
# This is acceptable — if the filesystem is broken, blocking would be worse.
LOG="$PROJECT_ROOT/.stop-hook-log"

# --- Read stdin ONCE (sole Stop hook — no stdin sharing, no race) ---
STDIN_JSON=$(cat)

# --- Resolve agent for THIS session (not from shared files) ---
# _paths.sh resolves via .latest-session-id (shared) — wrong for concurrent agents.
# .active-agent-$SID was written by /start and is per-session. Use it directly.
HOOK_SID=$(printf '%s' "$STDIN_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")

# Can't identify this session — don't risk blocking the wrong window
if [ -z "$HOOK_SID" ]; then
    echo "$(date +%Y-%m-%dT%H:%M:%S) ALLOW gate=no-sid" >> "$LOG"
    exit 0
fi

HOOK_AGENT=""
if [ -f "$PROJECT_ROOT/.active-agent-$HOOK_SID" ]; then
    HOOK_AGENT=$(cat "$PROJECT_ROOT/.active-agent-$HOOK_SID" 2>/dev/null | tr -d '\r\n')
fi
HOOK_AGENT="${HOOK_AGENT:-$AGENT_NAME}"

# No agent resolved — nothing to block for
if [ -z "$HOOK_AGENT" ]; then
    echo "$(date +%Y-%m-%dT%H:%M:%S) ALLOW gate=no-agent sid=$HOOK_SID" >> "$LOG"
    exit 0
fi

HOOK_AGENT_DIR="$PROJECT_ROOT/$HOOK_AGENT"

# --- Insight capture (non-critical — must not affect blocking decision) ---
printf '%s' "$STDIN_JSON" | AYOAI_AGENT="$HOOK_AGENT" python3 "$CORE_ROOT/scripts/capture-insights.py" 2>/dev/null || true

# --- Housekeeping: stale session files + legacy artifacts ---
find "$PROJECT_ROOT" -maxdepth 1 -name '.active-agent-*' -mmin +1440 -delete 2>/dev/null || true
rm -f "$PROJECT_ROOT/.stop-hook-stdin.json"

# --- Gate 0: Session identity — only block the runner session ---
# running-session-id is set by /start (autonomous mode) and kept in sync by
# session-save-id.sh (on compact). If missing, no loop is running — allow stop.
RUNNER_FILE="$HOOK_AGENT_DIR/session/running-session-id"
if [ ! -f "$RUNNER_FILE" ]; then
    echo "$(date +%Y-%m-%dT%H:%M:%S) ALLOW gate=no-runner sid=$HOOK_SID agent=$HOOK_AGENT" >> "$LOG"
    exit 0
fi
RUNNER_SID=$(cat "$RUNNER_FILE" 2>/dev/null || echo "")
if [ -n "$RUNNER_SID" ] && [ "$HOOK_SID" != "$RUNNER_SID" ]; then
    echo "$(date +%Y-%m-%dT%H:%M:%S) ALLOW gate=sid-mismatch sid=$HOOK_SID runner=$RUNNER_SID agent=$HOOK_AGENT" >> "$LOG"
    exit 0  # Different session — not the autonomous loop runner, allow stop
fi

# DO NOT use "$_A bash ..." — variable expansion is not recognized as an env
# assignment prefix by bash. Use export so all child processes inherit the agent.
export AYOAI_AGENT="$HOOK_AGENT"

# --- Gate 1: Not RUNNING → allow stop ---
STATE=$(bash "$CORE_ROOT/scripts/session-state-get.sh" 2>/dev/null || echo "UNINITIALIZED")
if [ "$STATE" != "RUNNING" ]; then
    echo "$(date +%Y-%m-%dT%H:%M:%S) ALLOW gate=not-running sid=$HOOK_SID agent=$HOOK_AGENT state=$STATE" >> "$LOG"
    exit 0
fi

# --- Gate 2: stop-loop signal (set by /stop) → allow stop + cleanup ---
if bash "$CORE_ROOT/scripts/session-signal-exists.sh" stop-loop 2>/dev/null; then
    bash "$CORE_ROOT/scripts/session-signal-clear.sh" stop-loop
    echo "$(date +%Y-%m-%dT%H:%M:%S) ALLOW gate=stop-loop sid=$HOOK_SID agent=$HOOK_AGENT" >> "$LOG"
    exit 0
fi

# --- Gate 2.5: Pending background agents → allow stop ---
if bash "$CORE_ROOT/scripts/pending-agents.sh" has-pending 2>/dev/null; then
    echo "$(date +%Y-%m-%dT%H:%M:%S) ALLOW gate=pending-agents sid=$HOOK_SID agent=$HOOK_AGENT" >> "$LOG"
    exit 0
fi

# --- BLOCK: Agent is RUNNING, no stop signal — keep the loop alive ---
# WHY this file: Autocompact changes the session ID. session-save-id.sh (the
# SessionStart hook) needs to know which agent just compacted so it can update
# running-session-id with the new SID. This file contains the OLD SID — if it
# matches running-session-id, session-save-id.sh knows this agent just compacted.
# Lives in the agent's session dir (not project root) to avoid multi-agent races.
echo "$HOOK_SID" > "$HOOK_AGENT_DIR/session/compact-pending"
CKPT_MSG=""
if [ -f "$HOOK_AGENT_DIR/session/compact-checkpoint.yaml" ]; then
    CKPT_MSG=" Encoding checkpoint saved -- Phase -0.5c will process it on re-entry."
fi
echo "$(date +%Y-%m-%dT%H:%M:%S) BLOCK sid=$HOOK_SID agent=$HOOK_AGENT" >> "$LOG"
echo "{\"decision\":\"block\",\"reason\":\"Context was compressed -- this is normal autocompact. Your FIRST action MUST be: Skill('aspirations') with args='loop'. Do NOT manually select goals. Do NOT run Bash commands first. Call the Skill tool IMMEDIATELY. Agent: ${HOOK_AGENT}. Prefix all Bash with AYOAI_AGENT=${HOOK_AGENT}.${CKPT_MSG}\"}"
exit 0
