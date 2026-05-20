#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# PreCompact serialization gate — ensures at most one agent compacts at a time.
#
# WHY: Eliminates the cross-agent SessionStart race when two agents autocompact
# simultaneously. Claude Code does not provide previous_session_id at compact,
# so a SessionStart hook with source=compact cannot deterministically identify
# its own prior SID. Two hooks racing on the same readdir order can cross-claim
# breadcrumbs even with the four-witness match. This gate serializes
# temporally so that AT MOST ONE agent is between PreCompact and post-compact
# SessionStart at any moment — eliminating the temporal overlap that creates
# the race. See world/knowledge/tree/system/hook-authoring-patterns/
# hook-source-discrimination.md and rb-348.
#
# Lock: $PROJECT_ROOT/.autocompact-serialize-lock/ (mkdir-atomic on POSIX + mingw)
# Released by: core/scripts/session-save-id.sh when source=compact match succeeds
# Stale recovery: lock older than 5 minutes is treated as orphaned and removed
# Fail-open: any error path exits 0 — never block compaction
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"

STDIN_JSON=$(cat 2>/dev/null || echo "{}")
SID=$(printf '%s' "$STDIN_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")
[ -n "$SID" ] || exit 0

ACTIVE_FILE="$PROJECT_ROOT/.active-agent-$SID"
[ -f "$ACTIVE_FILE" ] || exit 0
MY_AGENT=$(cat "$ACTIVE_FILE" 2>/dev/null | tr -d '\r\n' || echo "")
[ -n "$MY_AGENT" ] || exit 0

LOCK_DIR="$PROJECT_ROOT/.autocompact-serialize-lock"
POISON_FILE="$PROJECT_ROOT/.autocompact-serialize-poison"

# Stale cleanup — orphaned lock from a crashed session
if [ -d "$LOCK_DIR" ]; then
    HOLDER_TS=$(cat "$LOCK_DIR/timestamp" 2>/dev/null || echo 0)
    NOW=$(date +%s)
    if [ "$((NOW - HOLDER_TS))" -gt 300 ]; then
        rm -rf "$LOCK_DIR" 2>/dev/null || true
    fi
fi

# Stale poison cleanup — same 5-min TTL as the lock. A poison file from a
# prior timeout means session-save-id.sh already consumed it and either
# deleted it (clean cycle) OR a follow-on crash left it stranded. Either
# way, an old poison shouldn't block fresh compacts indefinitely.
if [ -f "$POISON_FILE" ]; then
    POISON_TS=$(cat "$POISON_FILE" 2>/dev/null || echo 0)
    NOW=$(date +%s)
    if [ "$((NOW - POISON_TS))" -gt 300 ]; then
        rm -f "$POISON_FILE" 2>/dev/null || true
    fi
fi

# Atomic claim with bounded backoff (max 60s)
WAITED=0
while [ "$WAITED" -lt 60 ]; do
    if mkdir "$LOCK_DIR" 2>/dev/null; then
        echo "$(date +%s)" > "$LOCK_DIR/timestamp"
        echo "$MY_AGENT"   > "$LOCK_DIR/holder"
        echo "$SID"        > "$LOCK_DIR/sid"
        exit 0
    fi
    sleep 5
    WAITED=$((WAITED + 5))
done

# Timed out — write a POISON marker at PROJECT_ROOT so session-save-id.sh's
# source=compact branch refuses ALL breadcrumb consumption this cycle.
#
# Why not the original `rm -rf "$LOCK_DIR"`: clearing the lock made
# session-save-id.sh fall back to the 4-witness behavior. That fallback
# re-opens the cross-agent breadcrumb-stomp class that the 5th/7th witnesses
# were specifically designed to close (rb-348, ). A timeout is NOT
# a clean handoff: two genuinely-fresh agents whose compacts overlap can
# both pass the content-witness check on each other's breadcrumb when the
# holder/sid filters are gone.
#
# Why a SEPARATE file (not inside LOCK_DIR): the lock dir may still be live
# (the other agent is genuinely mid-compact). Writing into another agent's
# lock dir corrupts their witness data. Project-root poison is observable
# by both agents' session-save-id calls without touching either's lock state.
#
# Lifecycle:
#   - Written here on 60s timeout
#   - Consumed (read + deleted) by session-save-id.sh source=compact path
#   - Stale cleanup: precompact-serialize next run clears poison > 5min old
#     (same TTL as the lock-dir stale-cleanup at the top of this script)
#
# Fail-open invariant preserved: this script STILL exits 0. It changes WHAT
# downstream sees, not WHETHER compaction proceeds.
echo "$(date +%s)" > "$PROJECT_ROOT/.autocompact-serialize-poison" 2>/dev/null
rm -rf "$LOCK_DIR" 2>/dev/null || true
exit 0
