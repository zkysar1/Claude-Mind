#!/usr/bin/env bash
# test-runner-identity-check.sh — regression test for the runner-identity gate.
#
# Verifies the Phase -1.45 multi-runner ejection gate (2026-05-23):
#   - runner passes (exit 0)
#   - non-runner ejects (exit 1 + actionable diagnostic)
#   - every ambiguity fails OPEN (exit 0) so a transient-empty
#     running-session-id never kills the legitimate runner
#   - CRLF in running-session-id is stripped (a Windows runner must not
#     falsely eject itself by comparing a clean SID against a CRLF-suffixed one)
#
# Sandbox mirrors PROJECT_ROOT/core/scripts/ with the real
# runner-identity-check.sh copied in + a minimal _paths.sh stub. Agent dirs
# are created at $SANDBOX/agents/<name>/session to match the stub's agent_dir
# (PROJECT_ROOT/agents/<name>, the post-Phase-2.5.D layout). NOTE: the sibling
# test-sid-collision-check.sh writes to the legacy $SANDBOX/<name> layout,
# which no longer matches agent_dir — this test uses the correct layout.
#
# Run: bash core/scripts/tests/test-runner-identity-check.sh
# Exit 0 = all pass, exit 1 = any case fails.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REAL_SCRIPT="$SCRIPT_DIR/../runner-identity-check.sh"

if [ ! -f "$REAL_SCRIPT" ]; then
    echo "FATAL: cannot find $REAL_SCRIPT" >&2
    exit 1
fi

SANDBOX=$(mktemp -d -t runner-identity-test-XXXXXX)
trap 'rm -rf "$SANDBOX"' EXIT

mkdir -p "$SANDBOX/core/scripts"
cp "$REAL_SCRIPT" "$SANDBOX/core/scripts/runner-identity-check.sh"

# Minimal _paths.sh stub — BASH_SOURCE-anchored PROJECT_ROOT + agent_dir,
# matching the real _paths.sh AGENTS_PARENT_DIR="agents" layout. Drops the
# external-paths config / python shim / windows detection the real script does;
# none are relevant to this gate.
cat > "$SANDBOX/core/scripts/_paths.sh" <<'PATHS_EOF'
#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$CORE_ROOT/.." && pwd)"
AGENTS_PARENT_DIR="agents"
agent_dir() {
    printf '%s/%s/%s' "$PROJECT_ROOT" "$AGENTS_PARENT_DIR" "$1"
}
PATHS_EOF

GATE="$SANDBOX/core/scripts/runner-identity-check.sh"
PASS_COUNT=0
FAIL_COUNT=0

reset_state() {
    rm -rf "$SANDBOX/agents"
}

# mk_runner <agent> <running-session-id-content>
#   ""         → create the session dir but NO running-session-id file
#   "__EMPTY__" → create an empty running-session-id file
#   other      → write that content as running-session-id
mk_runner() {
    local name="$1"
    local rsid="$2"
    mkdir -p "$SANDBOX/agents/$name/session"
    if [ "$rsid" = "__EMPTY__" ]; then
        : > "$SANDBOX/agents/$name/session/running-session-id"
    elif [ -n "$rsid" ]; then
        printf '%s' "$rsid" > "$SANDBOX/agents/$name/session/running-session-id"
    fi
}

# run_case <label> <expected_exit> <expected_stderr_substr|""> <agent_env> <sid_env>
# agent_env / sid_env are passed as command-prefix env assignments, which
# override any MIND_* the PreToolUse hook exported into this test's process.
# Empty string => the gate sees an empty (effectively unset) value.
run_case() {
    local label="$1"
    local expected_exit="$2"
    local expected_stderr="$3"
    local agent_env="$4"
    local sid_env="$5"

    local stderr_file
    stderr_file=$(mktemp -t runner-identity-stderr-XXXXXX)
    local rc=0
    MIND_AGENT="$agent_env" MIND_SID="$sid_env" bash "$GATE" 2>"$stderr_file" || rc=$?

    local stderr_content
    stderr_content=$(cat "$stderr_file")
    rm -f "$stderr_file"

    local ok=1
    if [ "$rc" != "$expected_exit" ]; then ok=0; fi
    if [ -n "$expected_stderr" ] && ! printf '%s' "$stderr_content" | grep -qF "$expected_stderr"; then ok=0; fi

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

# ─── Case 1: I AM the runner — continue (exit 0) ───────────────────────────
reset_state
mk_runner zeta sid-abc-111
run_case "1-runner-matches" 0 "" zeta sid-abc-111

# ─── Case 2: I am NOT the runner — eject (exit 1 + diagnostic) ─────────────
reset_state
mk_runner zeta sid-runner-999
run_case "2-non-runner-ejects" 1 "is NOT the runner" zeta sid-observer-222

# ─── Case 3: running-session-id EMPTY — fail-open (exit 0) ─────────────────
reset_state
mk_runner zeta __EMPTY__
run_case "3-empty-running-sid-fail-open" 0 "" zeta sid-abc-111

# ─── Case 4: running-session-id FILE MISSING — fail-open (exit 0) ──────────
reset_state
mk_runner zeta ""
run_case "4-missing-running-sid-fail-open" 0 "" zeta sid-abc-111

# ─── Case 5: empty MIND_SID — fail-open (exit 0) ──────────────────────────
reset_state
mk_runner zeta sid-runner-999
run_case "5-empty-my-sid-fail-open" 0 "" zeta ""

# ─── Case 6: empty MIND_AGENT — fail-open (exit 0) ────────────────────────
reset_state
mk_runner zeta sid-runner-999
run_case "6-empty-agent-fail-open" 0 "" "" sid-abc-111

# ─── Case 7: agent dir entirely missing — fail-open (exit 0) ───────────────
reset_state
run_case "7-missing-agent-dir-fail-open" 0 "" ghostagent sid-abc-111

# ─── Case 8: CRLF in running-session-id — runner still matches (exit 0) ────
# Windows writes running-session-id with a trailing CRLF. The gate strips it
# via `tr -d '\r\n'`; without the strip a legitimate runner would mismatch its
# own clean $MIND_SID and falsely eject itself. Critical Windows case.
reset_state
mkdir -p "$SANDBOX/agents/zeta/session"
printf 'sid-abc-111\r\n' > "$SANDBOX/agents/zeta/session/running-session-id"
run_case "8-crlf-running-sid-still-matches" 0 "" zeta sid-abc-111

# ─── Case 9: CRLF + non-runner — still ejects (exit 1) ─────────────────────
reset_state
mkdir -p "$SANDBOX/agents/zeta/session"
printf 'sid-runner-999\r\n' > "$SANDBOX/agents/zeta/session/running-session-id"
run_case "9-crlf-non-runner-ejects" 1 "is NOT the runner" zeta sid-observer-222

# === Write-attribution runner override (, US-09) ====================
# mk_attrib <agent> <sid> <epoch> — pre-stamp the write-attribution file.
mk_attrib() {
    local name="$1" sid="$2" epoch="$3"
    mkdir -p "$SANDBOX/agents/$name/session"
    printf '%s %s\n' "$sid" "$epoch" > "$SANDBOX/agents/$name/session/runner-write-attribution"
}

# run_case_wa — like run_case but with a RUNNER_WRITE_ATTRIB_WINDOW_SEC override.
# Any attribution file must be pre-created via mk_attrib before calling.
run_case_wa() {
    local label="$1" expected_exit="$2" expected_stderr="$3"
    local agent_env="$4" sid_env="$5" window_env="$6"
    local stderr_file rc=0
    stderr_file=$(mktemp -t runner-identity-stderr-XXXXXX)
    MIND_AGENT="$agent_env" MIND_SID="$sid_env" \
      RUNNER_WRITE_ATTRIB_WINDOW_SEC="$window_env" bash "$GATE" 2>"$stderr_file" || rc=$?
    local stderr_content; stderr_content=$(cat "$stderr_file"); rm -f "$stderr_file"
    local ok=1
    [ "$rc" = "$expected_exit" ] || ok=0
    if [ -n "$expected_stderr" ] && ! printf '%s' "$stderr_content" | grep -qF "$expected_stderr"; then ok=0; fi
    if [ "$ok" = "1" ]; then echo "PASS $label (exit=$rc)"; PASS_COUNT=$((PASS_COUNT + 1));
    else echo "FAIL $label"; echo "  expected_exit=$expected_exit got=$rc";
         echo "  expected_stderr=\"$expected_stderr\""; echo "  actual_stderr=\"$stderr_content\"";
         FAIL_COUNT=$((FAIL_COUNT + 1)); fi
}

NOW_EPOCH=$(date +%s)

# Case 10: stale pointer + FRESH self write-attribution -> override (exit 0).
# The  core case: running-session-id points at a stale SID, but THIS
# session stamped itself the confirmed runner seconds ago -> trust write-attrib.
reset_state
mk_runner zeta sid-runner-999
mk_attrib zeta sid-observer-222 "$NOW_EPOCH"
run_case_wa "10-stale-pointer-fresh-attrib-overrides" 0 \
    "write-attribution shows this session was the confirmed runner" zeta sid-observer-222 300

# Case 11: stale pointer + STALE self-attribution (> window) -> eject (exit 1).
reset_state
mk_runner zeta sid-runner-999
mk_attrib zeta sid-observer-222 "$((NOW_EPOCH - 600))"
run_case_wa "11-stale-pointer-stale-attrib-ejects" 1 "is NOT the runner" zeta sid-observer-222 300

# Case 12: stale pointer + attribution belongs to ANOTHER sid -> eject (takeover).
# A genuine new runner overwrote the shared stamp with its own SID; the old
# runner must eject immediately, not ride its own (now-overwritten) evidence.
reset_state
mk_runner zeta sid-runner-999
mk_attrib zeta sid-newrunner-333 "$NOW_EPOCH"
run_case_wa "12-attrib-other-sid-ejects" 1 "is NOT the runner" zeta sid-observer-222 300

# Case 13: confirmed runner STAMPS attribution (capture side).
reset_state
mk_runner zeta sid-abc-111
run_case "13-runner-matches-still-passes" 0 "" zeta sid-abc-111
ATTRIB="$SANDBOX/agents/zeta/session/runner-write-attribution"
if [ -f "$ATTRIB" ] && grep -Eq '^sid-abc-111 [0-9]+$' "$ATTRIB"; then
    echo "PASS 13b-confirmed-runner-stamps-attribution"
    PASS_COUNT=$((PASS_COUNT + 1))
else
    echo "FAIL 13b-confirmed-runner-stamps-attribution"
    echo "  attrib file: $([ -f "$ATTRIB" ] && cat "$ATTRIB" || echo MISSING)"
    FAIL_COUNT=$((FAIL_COUNT + 1))
fi

# Case 14: window env override — a small window ages out a recent stamp -> eject.
reset_state
mk_runner zeta sid-runner-999
mk_attrib zeta sid-observer-222 "$((NOW_EPOCH - 5))"
run_case_wa "14-window-env-override-ejects" 1 "is NOT the runner" zeta sid-observer-222 1

echo ""
echo "──────────────────────────────────────────"
echo "Total: $((PASS_COUNT + FAIL_COUNT))  Pass: $PASS_COUNT  Fail: $FAIL_COUNT"

if [ "$FAIL_COUNT" -gt 0 ]; then
    echo "TEST FAIL"
    exit 1
fi
echo "TEST PASS"
exit 0
