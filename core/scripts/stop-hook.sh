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
#   Lives at: mind/session/stop-block-count

set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"

# --- Gate 0: Session identity — only block the runner session ---
# The aspirations loop writes its session UUID to running-session-id on entry.
# Any other Claude Code session (different UUID) stops freely.
HOOK_SID=$(python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")
RUNNER_FILE="$REPO_ROOT/mind/session/running-session-id"
if [ -f "$RUNNER_FILE" ] && [ -n "$HOOK_SID" ]; then
    RUNNER_SID=$(cat "$RUNNER_FILE" 2>/dev/null || echo "")
    if [ -n "$RUNNER_SID" ] && [ "$HOOK_SID" != "$RUNNER_SID" ]; then
        exit 0  # Different session — not the autonomous loop runner, allow stop
    fi
fi

# --- Gate 1: Not RUNNING → allow stop ---
STATE=$(bash "$CORE_ROOT/scripts/session-state-get.sh" 2>/dev/null || echo "UNINITIALIZED")
if [ "$STATE" != "RUNNING" ]; then
    exit 0
fi

# --- Gate 2: stop-loop signal → allow stop + cleanup ---
if bash "$CORE_ROOT/scripts/session-signal-exists.sh" stop-loop 2>/dev/null; then
    bash "$CORE_ROOT/scripts/session-signal-clear.sh" stop-loop
    bash "$CORE_ROOT/scripts/session-counter-clear.sh"
    exit 0
fi

# --- Gate 2.5: Pending background agents → allow stop ---
# Parent agent is idle-waiting for background agents to complete.
# Allow stop — agent completion notifications will re-engage the parent.
# has-pending prunes stale agents (>timeout_minutes), so orphaned entries self-heal.
if bash "$CORE_ROOT/scripts/pending-agents.sh" has-pending 2>/dev/null; then
    # Counter clear is critical: without it, each idle-wait increments the counter
    # and after 5 episodes the safety valve would permanently stop the loop.
    bash "$CORE_ROOT/scripts/session-counter-clear.sh" 2>/dev/null
    exit 0
fi

# --- Atomic increment counter ---
COUNT=$(bash "$CORE_ROOT/scripts/session-counter-increment.sh")

# --- Tier 5+: Safety valve (block 5+) → allow stop, clean up ---
if [ "$COUNT" -ge 5 ]; then
    bash "$CORE_ROOT/scripts/session-counter-clear.sh"
    exit 0
fi

# --- Tier 1-3: Re-enter aspirations loop ---
if [ "$COUNT" -le 3 ]; then
    CKPT_MSG=""
    if [ -f "$REPO_ROOT/mind/session/compact-checkpoint.yaml" ]; then
        CKPT_MSG=" Encoding checkpoint saved -- Phase -0.5c will process it on re-entry."
    fi
    echo "{\"decision\":\"block\",\"reason\":\"[Recovery Tier ${COUNT}/3] Context compressed -- this is NORMAL. You MUST invoke /aspirations loop NOW. Do NOT set stop-loop. Do NOT write handoff. Do NOT consolidate. Re-enter the loop immediately.${CKPT_MSG}\"}"
    exit 0
fi

# --- Tier 4: Invoke /recover ---
echo "{\"decision\":\"block\",\"reason\":\"[Recovery Tier 4] Re-entry failed 3 times. Invoke /recover to diagnose the situation and report status to the user.\"}"
exit 0
