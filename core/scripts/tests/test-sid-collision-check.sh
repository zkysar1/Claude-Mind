#!/usr/bin/env bash
# test-sid-collision-check.sh — regression test for the SID-collision gate.
#
# Built for the 2026-05-13 zeta same-agent cross-binding fix. The pre-fix
# script had `[ "$EXISTING" = "$MY_AGENT" ] && exit 0` as an unconditional
# same-agent fast-path. Post-autocompact SID rotation that landed on a
# same-agent observer binding passed all 8 prior witnesses, so the writer
# in session-save-id.sh stomped running-session-id. The fix replaces the
# fast-path with a runner-vs-observer collision check.
#
# Sandbox: a fresh temp dir mirroring PROJECT_ROOT/core/scripts/ with the
# real sid-collision-check.sh copied in, plus stubs for _paths.sh,
# heartbeat-stale.sh, and _resolve_agent_from_sid.py that drive AGENT_DIR /
# heartbeat / binding-resolution decisions from easily-controlled disk state
# inside the sandbox. This avoids touching the real PROJECT_ROOT and keeps the
# test independent of the real aspirations.yaml.
#
# Agent dirs live at $SANDBOX/agents/<name> to match the real _paths.sh
# AGENTS_PARENT_DIR="agents" layout (post-Phase-2.5.D). The gate reads
# agent-state / running-session-id / heartbeat via agent_dir(), so the disk
# layout MUST match agent_dir's output or every state read returns empty.
#
# Run: bash core/scripts/tests/test-sid-collision-check.sh
# Exit 0 = all pass, exit 1 = any case fails.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REAL_SCRIPT="$SCRIPT_DIR/../sid-collision-check.sh"

if [ ! -f "$REAL_SCRIPT" ]; then
    echo "FATAL: cannot find $REAL_SCRIPT" >&2
    exit 1
fi

SANDBOX=$(mktemp -d -t sid-collision-test-XXXXXX)
trap 'rm -rf "$SANDBOX"' EXIT

mkdir -p "$SANDBOX/core/scripts"
cp "$REAL_SCRIPT" "$SANDBOX/core/scripts/sid-collision-check.sh"

# Minimal _paths.sh stub. Mirrors the real script's PROJECT_ROOT derivation
# (BASH_SOURCE-anchored) and AGENT_DIR computation, but drops everything
# else the production _paths.sh does (external-paths config, python shim,
# windows shell detection). Those are unrelated to the gate's logic.
cat > "$SANDBOX/core/scripts/_paths.sh" <<'PATHS_EOF'
#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$CORE_ROOT/.." && pwd)"
AGENTS_PARENT_DIR="agents"
agents_root() {
    if [ -n "$AGENTS_PARENT_DIR" ]; then
        printf '%s/%s' "$PROJECT_ROOT" "$AGENTS_PARENT_DIR"
    else
        printf '%s' "$PROJECT_ROOT"
    fi
}
agent_dir() {
    if [ -n "$AGENTS_PARENT_DIR" ]; then
        printf '%s/%s/%s' "$PROJECT_ROOT" "$AGENTS_PARENT_DIR" "$1"
    else
        printf '%s/%s' "$PROJECT_ROOT" "$1"
    fi
}
# The production gate passes the agent to heartbeat-stale.sh via MIND_AGENT.
AGENT_NAME="${MIND_AGENT:-}"
if [ -n "$AGENT_NAME" ]; then
    AGENT_DIR="$(agent_dir "$AGENT_NAME")"
else
    AGENT_DIR=""
fi
PATHS_EOF

# Stub heartbeat-stale.sh: returns "fresh" iff the heartbeat file exists.
# The real script reads aspirations.yaml for the stale-minutes threshold —
# we don't replicate that since the collision gate only branches on
# "fresh" vs anything else. File existence = fresh is sufficient for unit
# coverage of the gate logic.
cat > "$SANDBOX/core/scripts/heartbeat-stale.sh" <<'HB_EOF'
#!/usr/bin/env bash
set -uo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
HB="$AGENT_DIR/session/runner-heartbeat"
if [ -f "$HB" ]; then
    echo fresh
else
    echo stale
fi
HB_EOF
chmod +x "$SANDBOX/core/scripts/heartbeat-stale.sh"

# Stub _resolve_agent_from_sid.py: the gate (Phase 2.6 refactor) resolves the
# SID->agent binding through this script (sid-collision-check.sh line ~80), NOT
# by reading .active-agent-<SID> directly. Without this stub the resolver call
# fails, EXISTING is empty, and EVERY collision case short-circuits at
# `[ -n "$EXISTING" ] || exit 0` before any collision logic runs — the silent
# stub-drift that made cases 7 & 9 vacuously "pass" (exit 0 where 2 was
# expected). The stub mirrors the real resolver's legacy fallback: read
# PROJECT_ROOT/.active-agent-<SID> (what mk_bind writes).
cat > "$SANDBOX/core/scripts/_resolve_agent_from_sid.py" <<'RESOLVE_EOF'
#!/usr/bin/env python3
import os, sys
sid = sys.argv[1] if len(sys.argv) > 1 else ""
root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    with open(os.path.join(root, ".active-agent-" + sid), encoding="utf-8") as fh:
        sys.stdout.write(fh.read().strip())
except Exception:
    pass
RESOLVE_EOF

GATE="$SANDBOX/core/scripts/sid-collision-check.sh"
PASS_COUNT=0
FAIL_COUNT=0

# reset_state — wipe the sandbox between cases. core/ is kept; agent dirs
# and binding files are recreated per case.
reset_state() {
    rm -rf "$SANDBOX/agents"
    find "$SANDBOX" -maxdepth 1 -name '.active-agent-*' -delete 2>/dev/null || true
}

# run_case <label> <expected_exit> <expected_stderr_substring|""> <my_agent> <sid>
# Setup commands come after via repeated `setup <cmd>` calls into a temp file.
# Simpler model: caller pre-creates the disk state, then calls run_case.
run_case() {
    local label="$1"
    local expected_exit="$2"
    local expected_stderr="$3"
    local my_agent="$4"
    local sid="$5"

    local stderr_file
    stderr_file=$(mktemp -t sid-collision-stderr-XXXXXX)
    local rc=0
    bash "$GATE" "$my_agent" "$sid" 2> "$stderr_file" || rc=$?

    local stderr_content
    stderr_content=$(cat "$stderr_file")
    rm -f "$stderr_file"

    local ok=1
    if [ "$rc" != "$expected_exit" ]; then
        ok=0
    fi
    if [ -n "$expected_stderr" ] && ! printf '%s' "$stderr_content" | grep -qF "$expected_stderr"; then
        ok=0
    fi

    if [ "$ok" = "1" ]; then
        echo "PASS $label (exit=$rc)"
        PASS_COUNT=$((PASS_COUNT + 1))
    else
        echo "FAIL $label"
        echo "  expected_exit=$expected_exit got=$rc"
        echo "  expected_stderr=\"$expected_stderr\""
        echo "  actual_stderr=\"$stderr_content\""
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
}

# Helper to create an agent dir with state/running-session-id/heartbeat
# in one call. Cuts repetition across cases.
mk_agent() {
    local name="$1"
    local state="$2"
    local rsid="$3"      # pass "" to skip running-session-id
    local heartbeat="$4" # "fresh" creates file, "stale" omits it
    mkdir -p "$SANDBOX/agents/$name/session"
    echo "$state" > "$SANDBOX/agents/$name/session/agent-state"
    if [ -n "$rsid" ]; then
        echo "$rsid" > "$SANDBOX/agents/$name/session/running-session-id"
    fi
    if [ "$heartbeat" = "fresh" ]; then
        touch "$SANDBOX/agents/$name/session/runner-heartbeat"
    fi
}

mk_bind() {
    local sid="$1"
    local agent="$2"
    echo "$agent" > "$SANDBOX/.active-agent-$sid"
}

# ─── Case 0 (CANARY): the harness can drive the gate to a collision verdict ──
# Minimal end-to-end smoke that ISOLATES "sandbox/harness broken" from "gate
# broken". If this fails, a sandbox dependency drifted (a missing stub, a
# layout mismatch) — fix the harness, not sid-collision-check.sh. Guards the
# exact silent stub-drift that let cases 7 & 9 degrade to vacuous passes: when
# the gate gained the _resolve_agent_from_sid.py dependency, every collision
# case quietly became a no-op exit-0. A loud canary turns that regression class
# into an obvious, specific failure.
reset_state
mk_agent alpha RUNNING alphaRsid fresh
mk_bind canarySID alpha
run_case "0-CANARY-harness-can-detect-collision" 2 "ERROR:SID_COLLISION" zeta canarySID

# ─── Case 1: binding file missing — safe ───────────────────────────────────
reset_state
run_case "1-bind-file-missing" 0 "" zeta abc12345

# ─── Case 2: binding file empty — safe ─────────────────────────────────────
reset_state
: > "$SANDBOX/.active-agent-empty1"
run_case "2-bind-file-empty" 0 "" zeta empty1

# ─── Case 3: same-agent, state IDLE — safe (recovery / observer rebind) ────
reset_state
mk_agent zeta IDLE "" stale
mk_bind abc12345 zeta
run_case "3-same-agent-state-idle" 0 "" zeta abc12345

# ─── Case 4: same-agent, no running-session-id — safe ──────────────────────
reset_state
mk_agent zeta RUNNING "" fresh
mk_bind abc12345 zeta
run_case "4-same-agent-no-running-sid" 0 "" zeta abc12345

# ─── Case 5: same-agent, runner SID == $SID — safe (runner reconnect) ──────
reset_state
mk_agent zeta RUNNING abc12345 fresh
mk_bind abc12345 zeta
run_case "5-same-agent-runner-sid-matches" 0 "" zeta abc12345

# ─── Case 6: same-agent, runner-RECORDED on DIFFERENT SID, heartbeat stale ─
# State+rsid say a runner exists but its heartbeat is stale → crashed-runner
# recovery path. Must allow rebind.
reset_state
mk_agent zeta RUNNING runner1234 stale
mk_bind observer1 zeta
run_case "6-same-agent-different-runner-hb-stale" 0 "" zeta observer1

# ─── Case 7: SAME-AGENT SID-REUSE COLLISION (the 2026-05-13 zeta bug) ──────
# .active-agent-<observer> binds to zeta. zeta is RUNNING on a different
# SID, heartbeat fresh. Caller (post-autocompact session-save-id.sh) must
# refuse so the runner's binding is not silently stomped.
reset_state
mk_agent zeta RUNNING runner1234 fresh
mk_bind observer1 zeta
run_case "7-same-agent-sid-reuse-COLLISION" 2 "ERROR:SID_COLLISION_SAME_AGENT" zeta observer1

# ─── Case 8: cross-agent, IDLE — safe (recovery) ───────────────────────────
reset_state
mk_agent alpha IDLE "" stale
mk_bind shared01 alpha
run_case "8-cross-agent-state-idle" 0 "" zeta shared01

# ─── Case 9: cross-agent, RUNNING + fresh — COLLISION ──────────────────────
reset_state
mk_agent alpha RUNNING alphaRsid fresh
mk_bind shared01 alpha
run_case "9-cross-agent-running-fresh-COLLISION" 2 "ERROR:SID_COLLISION sid=" zeta shared01

# ─── Case 10: cross-agent, RUNNING + heartbeat stale — safe (crashed) ──────
reset_state
mk_agent alpha RUNNING alphaRsid stale
mk_bind shared01 alpha
run_case "10-cross-agent-running-hb-stale" 0 "" zeta shared01

# ─── Case 11: missing args — usage error ───────────────────────────────────
# Inline (not via run_case) because run_case requires both my_agent and sid
# arguments; this case tests the "called with neither" path.
reset_state
case11_rc=0
bash "$GATE" 2>/dev/null || case11_rc=$?
if [ "$case11_rc" = "1" ]; then
    echo "PASS 11-missing-args (exit=1)"
    PASS_COUNT=$((PASS_COUNT + 1))
else
    echo "FAIL 11-missing-args: expected exit=1 got=$case11_rc"
    FAIL_COUNT=$((FAIL_COUNT + 1))
fi

# ─── Case 12: cross-agent, target dir missing — safe ───────────────────────
# Stale binding pointing to a deleted agent.
reset_state
mk_bind shared01 ghostagent
run_case "12-cross-agent-target-dir-missing" 0 "" zeta shared01

echo ""
echo "──────────────────────────────────────────"
echo "Total: $((PASS_COUNT + FAIL_COUNT))  Pass: $PASS_COUNT  Fail: $FAIL_COUNT"

if [ "$FAIL_COUNT" -gt 0 ]; then
    echo "TEST FAIL"
    exit 1
fi
echo "TEST PASS"
exit 0
