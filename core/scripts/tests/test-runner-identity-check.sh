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

# The REAL _runner_proc.sh, not a stub (). The owning-process predicate
# moved out of runner-identity-check.sh so stop-hook.sh Gate 0 could consume the
# SAME implementation instead of a second copy; this sandbox must therefore carry
# it or the script sources a missing file and every proc-stamp case fails with a
# "No such file or directory" that reads as a logic regression. Copied rather than
# stubbed because the predicate IS what cases 15b-18b exercise.
cp "$(dirname "$REAL_SCRIPT")/_runner_proc.sh" "$SANDBOX/core/scripts/_runner_proc.sh"

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

# === Same-SID duplicate-instance eject () =========================
# The gap: every case above turns on a SID MISMATCH. Two processes carrying the
# SAME $MIND_SID both pass, forever. These cases pin the process-identity
# discriminator that closes it — and, critically, pin that a LEGITIMATE single
# runner stays GREEN, because a gate that ejects the real runner is far worse
# than the bug it fixes.
#
# RUNNER_PROC_ID injects the owning-process identity (a sandbox has no `claude`
# ancestor to resolve). Case 19 leaves it UNSET to exercise the real /proc walk.

# live_proc_id <pid> -> "<pid>:<starttime>" for a process that is genuinely alive.
# Uses the same comm-safe parse as the gate (strip through the LAST paren).
live_proc_id() {
    local p="$1" line rest
    line=$(cat "/proc/$p/stat" 2>/dev/null) || return 1
    rest="${line##*)}"
    printf '%s:%s' "$p" "$(printf '%s' "$rest" | awk '{print $20}')"
}

mk_proc_stamp() {  # <agent> <proc-id>
    mkdir -p "$SANDBOX/agents/$1/session"
    printf '%s\n' "$2" > "$SANDBOX/agents/$1/session/runner-proc"
}

run_case_proc() {  # <label> <exit> <stderr-substr> <agent> <sid> <proc-id-or-__UNSET__>
    local label="$1" expected_exit="$2" expected_stderr="$3"
    local agent_env="$4" sid_env="$5" proc_env="$6"
    local stderr_file rc=0
    stderr_file=$(mktemp -t runner-identity-stderr-XXXXXX)
    if [ "$proc_env" = "__UNSET__" ]; then
        MIND_AGENT="$agent_env" MIND_SID="$sid_env" bash "$GATE" 2>"$stderr_file" || rc=$?
    else
        MIND_AGENT="$agent_env" MIND_SID="$sid_env" \
          RUNNER_PROC_ID="$proc_env" bash "$GATE" 2>"$stderr_file" || rc=$?
    fi
    local stderr_content; stderr_content=$(cat "$stderr_file"); rm -f "$stderr_file"
    local ok=1
    [ "$rc" = "$expected_exit" ] || ok=0
    if [ -n "$expected_stderr" ] && ! printf '%s' "$stderr_content" | grep -qF "$expected_stderr"; then ok=0; fi
    if [ "$ok" = "1" ]; then echo "PASS $label (exit=$rc)"; PASS_COUNT=$((PASS_COUNT + 1));
    else echo "FAIL $label"; echo "  expected_exit=$expected_exit got=$rc";
         echo "  expected_stderr=\"$expected_stderr\""; echo "  actual_stderr=\"$stderr_content\"";
         FAIL_COUNT=$((FAIL_COUNT + 1)); fi
}

PROC_FILE_PATH="$SANDBOX/agents/zeta/session/runner-proc"

# Case 15: sole runner, no prior stamp -> passes AND claims the stamp.
reset_state
mk_runner zeta sid-abc-111
run_case_proc "15-sole-runner-claims-proc-stamp" 0 "" zeta sid-abc-111 "4242:99999"
if [ -f "$PROC_FILE_PATH" ] && [ "$(cat "$PROC_FILE_PATH")" = "4242:99999" ]; then
    echo "PASS 15b-proc-stamp-written"; PASS_COUNT=$((PASS_COUNT + 1))
else
    echo "FAIL 15b-proc-stamp-written"
    echo "  runner-proc: $([ -f "$PROC_FILE_PATH" ] && cat "$PROC_FILE_PATH" || echo MISSING)"
    FAIL_COUNT=$((FAIL_COUNT + 1))
fi

# Case 16: stamp is MINE -> passes (the steady-state single-runner iteration).
reset_state
mk_runner zeta sid-abc-111
mk_proc_stamp zeta "4242:99999"
run_case_proc "16-own-proc-stamp-passes" 0 "" zeta sid-abc-111 "4242:99999"

# Case 17: THE BUG. Same SID, stamp held by a DIFFERENT LIVE process -> EJECT.
# Before this change the gate returned 0 here, which is the whole defect.
reset_state
mk_runner zeta sid-abc-111
mk_proc_stamp zeta "$(live_proc_id $$)"
run_case_proc "17-same-sid-live-duplicate-ejects" 1 "SAME-SID DUPLICATE INSTANCE" \
    zeta sid-abc-111 "4242:99999"

# Case 18: stamp holds a DEAD owner -> take over, do not wedge. Uses a LIVE pid
# with a WRONG starttime, which is precisely the PID-reuse shape: pid alone
# would read as alive, the (pid,starttime) pair correctly reads as dead.
reset_state
mk_runner zeta sid-abc-111
mk_proc_stamp zeta "$$:1"
run_case_proc "18-dead-owner-taken-over" 0 "" zeta sid-abc-111 "4242:99999"
if [ "$(cat "$PROC_FILE_PATH" 2>/dev/null)" = "4242:99999" ]; then
    echo "PASS 18b-takeover-rewrites-stamp"; PASS_COUNT=$((PASS_COUNT + 1))
else
    echo "FAIL 18b-takeover-rewrites-stamp"
    echo "  runner-proc: $(cat "$PROC_FILE_PATH" 2>/dev/null || echo MISSING)"
    FAIL_COUNT=$((FAIL_COUNT + 1))
fi

# Case 19: RUNNER_PROC_ID UNSET -> the real /proc ancestor walk runs. Asserts
# only the SHAPE (<digits>:<digits>) and that the runner still passes, so the
# case holds on any box: where an ancestor resolves it stamps a real identity,
# and where none does the check is skipped and no stamp appears. Either way a
# legitimate sole runner must exit 0 — that is the invariant under test.
reset_state
mk_runner zeta sid-abc-111
run_case_proc "19-real-proc-walk-runner-still-passes" 0 "" zeta sid-abc-111 __UNSET__
if [ ! -f "$PROC_FILE_PATH" ] || grep -Eq '^[0-9]+:[0-9]+$' "$PROC_FILE_PATH"; then
    echo "PASS 19b-real-walk-stamp-well-formed-or-absent"; PASS_COUNT=$((PASS_COUNT + 1))
else
    echo "FAIL 19b-real-walk-stamp-well-formed-or-absent"
    echo "  runner-proc: $(cat "$PROC_FILE_PATH")"
    FAIL_COUNT=$((FAIL_COUNT + 1))
fi

# Case 20: a NON-runner with a duplicate-looking stamp still ejects via the
# ORIGINAL mismatch path, not the new one. Pins that the new block did not
# swallow or reword the pre-existing eject.
reset_state
mk_runner zeta sid-runner-999
mk_proc_stamp zeta "$(live_proc_id $$)"
run_case_proc "20-sid-mismatch-still-uses-original-eject" 1 "is NOT the runner" \
    zeta sid-observer-222 "4242:99999"

echo ""
echo "──────────────────────────────────────────"
echo "Total: $((PASS_COUNT + FAIL_COUNT))  Pass: $PASS_COUNT  Fail: $FAIL_COUNT"

if [ "$FAIL_COUNT" -gt 0 ]; then
    echo "TEST FAIL"
    exit 1
fi
echo "TEST PASS"
exit 0
