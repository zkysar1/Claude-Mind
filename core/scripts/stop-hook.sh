#!/usr/bin/env bash
# Stop hook — session-scoped escalating recovery for autonomous loop
#
# Global hook registered in .claude/settings.json. Fires on every stop attempt.
#
# Gate 0:   Non-runner sessions pass through immediately (session identity check)
# Gate 1:   Not RUNNING → allow stop
# Gate 2:   stop-loop signal → allow stop
# Gate 2.5: Pending background agents → allow stop (agent completions re-engage parent)
# Tier 1-3: Block stop, tell Claude to re-enter the aspirations loop
# Tier 4:   Block stop, tell Claude to invoke /recover skill
# Tier 5+:  Safety valve — allow stop unconditionally
#
# Counter lifecycle:
#   Incremented: each stop attempt while RUNNING (before tier decision)
#   Reset by: /boot (stale cleanup), aspirations loop entry (Phase -0.5),
#             Gate 2.5 (pending agents), safety valve (block 5+)
#   Lives at: <agent>/session/stop-block-count

set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"

# --- Resolve agent for THIS session (not from shared files) ---
# _paths.sh resolves via .latest-session-id (shared) — wrong for concurrent agents.
# .active-agent-$SID was written by /start and is per-session. Use it directly.
#
# stdin may be empty: capture-insights.sh runs first in the same hooks array and
# consumes stdin. It saves the payload to .stop-hook-stdin.json for us.
STOP_HOOK_STDIN="$PROJECT_ROOT/.stop-hook-stdin.json"
HOOK_SID=""
if [ -f "$STOP_HOOK_STDIN" ]; then
    HOOK_SID=$(python3 -c "import json; print(json.load(open('$STOP_HOOK_STDIN')).get('session_id',''))" 2>/dev/null || echo "")
    rm -f "$STOP_HOOK_STDIN"
else
    HOOK_SID=$(python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")
fi
HOOK_AGENT=""
if [ -n "$HOOK_SID" ] && [ -f "$PROJECT_ROOT/.active-agent-$HOOK_SID" ]; then
    HOOK_AGENT=$(cat "$PROJECT_ROOT/.active-agent-$HOOK_SID" 2>/dev/null | tr -d '\r\n')
fi
HOOK_AGENT="${HOOK_AGENT:-$AGENT_NAME}"
HOOK_AGENT_DIR=""
if [ -n "$HOOK_AGENT" ]; then
    HOOK_AGENT_DIR="$PROJECT_ROOT/$HOOK_AGENT"
fi

# --- Gate 0: Session identity — only block the runner session ---
# running-session-id is set by Phase -0.5 (loop entry) and kept in sync by
# session-save-id.sh (on compact). Do not assume it is write-once.
# If no running-session-id exists, no loop is running — allow stop.
# If file exists but session IDs differ, this is a non-runner session — allow stop.
RUNNER_FILE="${HOOK_AGENT_DIR:+$HOOK_AGENT_DIR/session/running-session-id}"
if [ -n "$RUNNER_FILE" ] && [ -n "$HOOK_SID" ]; then
    if [ ! -f "$RUNNER_FILE" ]; then
        exit 0  # No running-session-id — no autonomous loop active, allow stop
    fi
    RUNNER_SID=$(cat "$RUNNER_FILE" 2>/dev/null || echo "")
    if [ -n "$RUNNER_SID" ] && [ "$HOOK_SID" != "$RUNNER_SID" ]; then
        exit 0  # Different session — not the autonomous loop runner, allow stop
    fi
fi

# All sub-scripts source _paths.sh internally. Without this prefix, they resolve
# the agent via shared .latest-session-id — wrong for concurrent agents.
_A="AYOAI_AGENT=$HOOK_AGENT"

# --- Gate 1: Not RUNNING → allow stop ---
STATE=$($_A bash "$CORE_ROOT/scripts/session-state-get.sh" 2>/dev/null || echo "UNINITIALIZED")
if [ "$STATE" != "RUNNING" ]; then
    exit 0
fi

# --- Gate 2: stop-loop signal → allow stop + cleanup ---
if $_A bash "$CORE_ROOT/scripts/session-signal-exists.sh" stop-loop 2>/dev/null; then
    $_A bash "$CORE_ROOT/scripts/session-signal-clear.sh" stop-loop
    $_A bash "$CORE_ROOT/scripts/session-counter-clear.sh"
    exit 0
fi

# --- Gate 2.5: Pending background agents → allow stop ---
# Parent agent is idle-waiting for background agents to complete.
# Allow stop — agent completion notifications will re-engage the parent.
# has-pending prunes stale agents (>timeout_minutes), so orphaned entries self-heal.
if $_A bash "$CORE_ROOT/scripts/pending-agents.sh" has-pending 2>/dev/null; then
    # Counter clear is critical: without it, each idle-wait increments the counter
    # and after 5 episodes the safety valve would permanently stop the loop.
    $_A bash "$CORE_ROOT/scripts/session-counter-clear.sh" 2>/dev/null
    exit 0
fi

# --- Atomic increment counter ---
COUNT=$($_A bash "$CORE_ROOT/scripts/session-counter-increment.sh")

# --- Tier 5+: Safety valve (block 5+) → allow stop, clean up ---
if [ "$COUNT" -ge 5 ]; then
    $_A bash "$CORE_ROOT/scripts/session-counter-clear.sh"
    exit 0
fi

# Breadcrumb for session-save-id.sh: written ONLY on blocked stops (autocompact recovery).
# SessionStart hook reads this to resolve the correct agent during carry-forward.
# NOT written on allowed stops (Gates/safety valve) — no SessionStart follows those.
_write_breadcrumb() { [ -n "$HOOK_AGENT" ] && echo "$HOOK_AGENT" > "$PROJECT_ROOT/.compact-agent"; }

# --- Tier 1-3: Re-enter aspirations loop ---
if [ "$COUNT" -le 3 ]; then
    CKPT_MSG=""
    if [ -n "$HOOK_AGENT_DIR" ] && [ -f "$HOOK_AGENT_DIR/session/compact-checkpoint.yaml" ]; then
        CKPT_MSG=" Encoding checkpoint saved -- Phase -0.5c will process it on re-entry."
    fi
    _write_breadcrumb
    echo "{\"decision\":\"block\",\"reason\":\"[Recovery Tier ${COUNT}/3] Context compressed -- this is NORMAL. You MUST invoke /aspirations loop NOW. Do NOT set stop-loop. Do NOT write handoff. Do NOT consolidate. Re-enter the loop immediately. Agent: ${HOOK_AGENT}. Prefix all Bash calls with AYOAI_AGENT=${HOOK_AGENT}.${CKPT_MSG}\"}"
    exit 0
fi

# --- Tier 4: Invoke /recover ---
_write_breadcrumb
echo "{\"decision\":\"block\",\"reason\":\"[Recovery Tier 4] Re-entry failed 3 times. Invoke /recover to diagnose the situation and report status to the user. Agent: ${HOOK_AGENT}. Prefix all Bash calls with AYOAI_AGENT=${HOOK_AGENT}.\"}"
exit 0
