#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Test session-save-id.sh against the cross-agent SID swap regression.
# Scenarios:
#   1. New-window-hijack (source != compact): no breadcrumb consumption
#   2. Compact event with valid four-witness: breadcrumb consumed correctly
#   3. Compact event with witness mismatch: breadcrumb restored, not consumed
#   4. Concurrent compact for two agents: each claims its own breadcrumb only
#
# Tests run in an isolated sandbox under /tmp; do NOT modify project state.
set -euo pipefail

PROJECT_ROOT_REAL="$(cd "$(dirname "$0")/../.." && pwd)"
SANDBOX="$(mktemp -d)"
trap "rm -rf '$SANDBOX'" EXIT

# Build a minimal sandbox that looks like the project
mkdir -p "$SANDBOX/core/scripts" "$SANDBOX/agent-x/session" "$SANDBOX/agent-y/session"
echo "WORLD_PATH=$SANDBOX/world" > "$SANDBOX/agent-x/local-paths.conf"
echo "META_PATH=$SANDBOX/meta"  >> "$SANDBOX/agent-x/local-paths.conf"
echo "WORLD_PATH=$SANDBOX/world" > "$SANDBOX/agent-y/local-paths.conf"
echo "META_PATH=$SANDBOX/meta"  >> "$SANDBOX/agent-y/local-paths.conf"
mkdir -p "$SANDBOX/world" "$SANDBOX/meta"
cp "$PROJECT_ROOT_REAL/core/scripts/session-save-id.sh" "$SANDBOX/core/scripts/"
cp "$PROJECT_ROOT_REAL/core/scripts/precompact-serialize.sh" "$SANDBOX/core/scripts/"
cp "$PROJECT_ROOT_REAL/core/scripts/_paths.sh" "$SANDBOX/core/scripts/"
cp "$PROJECT_ROOT_REAL/core/scripts/_paths.py" "$SANDBOX/core/scripts/"

PASS=0
FAIL=0

assert_equals() {
    local label="$1"; local actual="$2"; local expected="$3"
    if [ "$actual" = "$expected" ]; then
        echo "  PASS: $label"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: $label  actual=[$actual]  expected=[$expected]"
        FAIL=$((FAIL + 1))
    fi
}

reset_sandbox() {
    rm -f "$SANDBOX"/.active-agent-*
    rm -f "$SANDBOX"/agent-x/session/* "$SANDBOX"/agent-y/session/*
}

run_hook() {
    local sid="$1"; local source="$2"
    local payload
    payload=$(printf '{"session_id":"%s","source":"%s"}' "$sid" "$source")
    (cd "$SANDBOX" && echo "$payload" | bash core/scripts/session-save-id.sh) >/dev/null 2>&1
}

# ===== Scenario 1: New-window-hijack prevention =====
echo "Scenario 1: new-window event (source!=compact) must NOT consume breadcrumbs"
reset_sandbox
echo "OLD_X" > "$SANDBOX/agent-x/session/compact-pending"
echo "OLD_X" > "$SANDBOX/agent-x/session/running-session-id"
echo "OLD_X" > "$SANDBOX/agent-x/session/latest-session-id"
echo "agent-x" > "$SANDBOX/.active-agent-OLD_X"
run_hook "NEW_FRESH" "startup"
assert_equals "compact-pending preserved" "$(cat "$SANDBOX/agent-x/session/compact-pending")" "OLD_X"
assert_equals "agent-x running-session-id unchanged" "$(cat "$SANDBOX/agent-x/session/running-session-id")" "OLD_X"
[ -f "$SANDBOX/.active-agent-NEW_FRESH" ] && {
    echo "  FAIL: new-window should NOT have created .active-agent-NEW_FRESH"
    FAIL=$((FAIL + 1))
} || {
    echo "  PASS: no rogue .active-agent-NEW_FRESH binding created"
    PASS=$((PASS + 1))
}

# ===== Scenario 2: Valid compact with four-witness =====
echo "Scenario 2: compact event with all four witnesses agreeing"
reset_sandbox
echo "OLD_X" > "$SANDBOX/agent-x/session/compact-pending"
echo "OLD_X" > "$SANDBOX/agent-x/session/running-session-id"
echo "OLD_X" > "$SANDBOX/agent-x/session/latest-session-id"
echo "agent-x" > "$SANDBOX/.active-agent-OLD_X"
run_hook "NEW_X" "compact"
assert_equals "compact-pending consumed" "$([ -f "$SANDBOX/agent-x/session/compact-pending" ] && echo "exists" || echo "gone")" "gone"
assert_equals "running-session-id updated to NEW_X" "$(cat "$SANDBOX/agent-x/session/running-session-id")" "NEW_X"
assert_equals "latest-session-id updated to NEW_X" "$(cat "$SANDBOX/agent-x/session/latest-session-id")" "NEW_X"
assert_equals ".active-agent-NEW_X created" "$(cat "$SANDBOX/.active-agent-NEW_X" 2>/dev/null)" "agent-x"

# ===== Scenario 3: Compact with witness mismatch — breadcrumb restored =====
# Mismatch a CURRENT witness. session-save-id.sh's witness list (post 264f354)
# is: breadcrumb SID == running-session-id AND .active-agent-<old-SID> binding
# == agent dir. latest-session-id was deliberately dropped from the witness
# list in 264f354 ("pair-written with running-session-id, no independent
# signal"), so mismatching latest-session-id alone no longer triggers the
# check. Mismatch running-session-id to exercise the first content-witness.
echo "Scenario 3: compact event with witness mismatch must NOT consume"
reset_sandbox
echo "OLD_X" > "$SANDBOX/agent-x/session/compact-pending"
echo "DIFFERENT" > "$SANDBOX/agent-x/session/running-session-id"   # mismatch
echo "OLD_X" > "$SANDBOX/agent-x/session/latest-session-id"
echo "agent-x" > "$SANDBOX/.active-agent-OLD_X"
run_hook "NEW_X" "compact"
assert_equals "compact-pending preserved on mismatch" "$([ -f "$SANDBOX/agent-x/session/compact-pending" ] && cat "$SANDBOX/agent-x/session/compact-pending" || echo "gone")" "OLD_X"
assert_equals "running-session-id unchanged on mismatch" "$(cat "$SANDBOX/agent-x/session/running-session-id")" "DIFFERENT"

# ===== Scenario 4: Concurrent compact resumes (BYPASSES PRECOMPACT GATE) =====
# This scenario tests session-save-id.sh in isolation, with two simulated
# concurrent source=compact SessionStarts. In production this code path is
# protected upstream by precompact-serialize.sh (rb-356), which serializes
# PreCompact events so two SessionStarts almost never reach session-save-id.sh
# at the same time. Scenario 5 covers that gate. This scenario remains as a
# regression check on the four-witness logic itself: even if someone disables
# the gate, the witness check should produce a degraded-but-not-swapped
# outcome. Test logs outcome informationally, does NOT fail.
echo "Scenario 4 (informational): concurrent compact race (gate-bypassed)"
reset_sandbox
echo "OLD_X" > "$SANDBOX/agent-x/session/compact-pending"
echo "OLD_X" > "$SANDBOX/agent-x/session/running-session-id"
echo "OLD_X" > "$SANDBOX/agent-x/session/latest-session-id"
echo "agent-x" > "$SANDBOX/.active-agent-OLD_X"
echo "OLD_Y" > "$SANDBOX/agent-y/session/compact-pending"
echo "OLD_Y" > "$SANDBOX/agent-y/session/running-session-id"
echo "OLD_Y" > "$SANDBOX/agent-y/session/latest-session-id"
echo "agent-y" > "$SANDBOX/.active-agent-OLD_Y"
run_hook "NEW_X" "compact" &
run_hook "NEW_Y" "compact" &
wait
X_RSI=$(cat "$SANDBOX/agent-x/session/running-session-id" 2>/dev/null || echo "missing")
Y_RSI=$(cat "$SANDBOX/agent-y/session/running-session-id" 2>/dev/null || echo "missing")
echo "  INFO: agent-x running-session-id=$X_RSI"
echo "  INFO: agent-y running-session-id=$Y_RSI"
if [ "$X_RSI" = "NEW_Y" ] || [ "$Y_RSI" = "NEW_X" ]; then
    echo "  INFO: cross-agent swap occurred (known limitation, see SKILL docs)"
elif [ "$X_RSI" = "NEW_X" ] && [ "$Y_RSI" = "NEW_Y" ]; then
    echo "  INFO: lucky path — both hooks claimed correct agent"
else
    echo "  INFO: degraded (one agent missed update) — preferable to swap"
fi

# ===== Scenario 5: PreCompact serialization gate =====
# Fire two PreCompact events. The second must wait until the first releases.
# Validates: mkdir-atomic claim, holder file written, second hook spins
# (does not silently take the lock), release frees the second hook.
echo "Scenario 5: PreCompact serialization gate must serialize concurrent compacts"
reset_sandbox
rm -rf "$SANDBOX/.autocompact-serialize-lock" 2>/dev/null || true
echo "agent-x" > "$SANDBOX/.active-agent-SID_X"
echo "agent-y" > "$SANDBOX/.active-agent-SID_Y"

run_precompact() {
    local sid="$1"
    local payload
    payload=$(printf '{"session_id":"%s"}' "$sid")
    (cd "$SANDBOX" && echo "$payload" | bash core/scripts/precompact-serialize.sh) >/dev/null 2>&1
}

# X claims synchronously
run_precompact "SID_X"
HOLDER_AFTER_X=$(cat "$SANDBOX/.autocompact-serialize-lock/holder" 2>/dev/null || echo "missing")
assert_equals "X holds lock after first PreCompact" "$HOLDER_AFTER_X" "agent-x"

# Y fires while X still holds — Y must spin
run_precompact "SID_Y" &
PID_Y=$!
sleep 2  # Y is now mid-spin (first sleep iteration)
HOLDER_DURING_Y_SPIN=$(cat "$SANDBOX/.autocompact-serialize-lock/holder" 2>/dev/null || echo "missing")
assert_equals "X still holds lock while Y spins" "$HOLDER_DURING_Y_SPIN" "agent-x"

# Release X's lock (simulates session-save-id.sh's rm -rf at compact-source SessionStart)
rm -rf "$SANDBOX/.autocompact-serialize-lock"

# Y picks up the lock within its next 5s sleep cycle
wait $PID_Y
HOLDER_AFTER_Y=$(cat "$SANDBOX/.autocompact-serialize-lock/holder" 2>/dev/null || echo "missing")
assert_equals "Y holds lock after X released" "$HOLDER_AFTER_Y" "agent-y"

rm -rf "$SANDBOX/.autocompact-serialize-lock"

# ===== Scenario 6: PreCompact stale-lock recovery =====
# A lock older than 5 minutes (300s) must be reaped and reclaimable.
# Validates the stale-cleanup branch in precompact-serialize.sh.
echo "Scenario 6: PreCompact stale-lock cleanup reaps orphaned locks (>5min)"
reset_sandbox
rm -rf "$SANDBOX/.autocompact-serialize-lock" 2>/dev/null || true
echo "agent-x" > "$SANDBOX/.active-agent-SID_X"

# Manually plant a stale lock from a "crashed" session
mkdir "$SANDBOX/.autocompact-serialize-lock"
echo "$(($(date +%s) - 400))" > "$SANDBOX/.autocompact-serialize-lock/timestamp"
echo "ghost-agent" > "$SANDBOX/.autocompact-serialize-lock/holder"

run_precompact "SID_X"
HOLDER_AFTER_RECLAIM=$(cat "$SANDBOX/.autocompact-serialize-lock/holder" 2>/dev/null || echo "missing")
assert_equals "stale lock reclaimed by new PreCompact" "$HOLDER_AFTER_RECLAIM" "agent-x"
rm -rf "$SANDBOX/.autocompact-serialize-lock"

# ===== Scenario 7: Assistant/reader compact releases the lock =====
# Reader and assistant sessions DO autocompact (any long Claude Code window
# can), but they don't write a compact-pending breadcrumb (the stop hook only
# writes one when the autonomous loop BLOCKs). The release line in
# session-save-id.sh must therefore fire on ANY source=compact event, not
# only when a witness match succeeds. Without this, an assistant session's
# compact would strand the lock until 5-min stale cleanup.
echo "Scenario 7: source=compact releases lock even with no breadcrumb (assistant-mode safety)"
reset_sandbox
mkdir "$SANDBOX/.autocompact-serialize-lock"
echo "$(date +%s)" > "$SANDBOX/.autocompact-serialize-lock/timestamp"
echo "agent-x" > "$SANDBOX/.autocompact-serialize-lock/holder"
echo "OLD_X" > "$SANDBOX/.autocompact-serialize-lock/sid"
# No compact-pending breadcrumb anywhere — simulates assistant-mode compact
# Bind a fresh agent so session-save-id.sh has something resembling reality
echo "agent-x" > "$SANDBOX/.active-agent-NEW_X"
run_hook "NEW_X" "compact"
[ -d "$SANDBOX/.autocompact-serialize-lock" ] && {
    echo "  FAIL: lock should have been released by source=compact SessionStart"
    FAIL=$((FAIL + 1))
} || {
    echo "  PASS: lock released after source=compact SessionStart with no breadcrumb"
    PASS=$((PASS + 1))
}

# Negative case: source=startup must NOT release the lock (would defeat the gate)
echo "Scenario 7b: source=startup must NOT release the lock"
reset_sandbox
mkdir "$SANDBOX/.autocompact-serialize-lock"
echo "$(date +%s)" > "$SANDBOX/.autocompact-serialize-lock/timestamp"
echo "agent-x" > "$SANDBOX/.autocompact-serialize-lock/holder"
echo "OLD_X" > "$SANDBOX/.autocompact-serialize-lock/sid"
echo "agent-x" > "$SANDBOX/.active-agent-NEW_FRESH"
run_hook "NEW_FRESH" "startup"
[ -d "$SANDBOX/.autocompact-serialize-lock" ] && {
    echo "  PASS: lock preserved across non-compact SessionStart"
    PASS=$((PASS + 1))
} || {
    echo "  FAIL: source=startup must not clear the gate (would defeat serialization)"
    FAIL=$((FAIL + 1))
}
rm -rf "$SANDBOX/.autocompact-serialize-lock"

echo
echo "===== Results: $PASS passed, $FAIL failed ====="
[ "$FAIL" -eq 0 ]
