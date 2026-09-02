#!/usr/bin/env bash
# test_runner_dead_check.sh — 
#
# Verifies the runner-dead-check.sh helper's exit-code semantics and
# output shape. The helper is the SINGLE SOURCE OF TRUTH for the
# 6-condition liveness gate used by /start --recover Step 0.7,
# recovery-gate.sh PATH A run_gate_for_agent, and /start auto-recovery
# (inline copy).
#
# Strategy:
#   1. Exit code 2 when MIND_AGENT unset (error path).
#   2. Exit code 2 when MIND_AGENT names a nonexistent agent dir.
#   3. Exit code 1 for the bound agent; JSON shape branches on the observed
#      role — reducer (RUNNING, heartbeat fresh) vs worker/IDLE ().
#   4. JSON output is well-formed and contains the 6 condition booleans.
#   5. Stderr contains a verdict line + 7 per-condition labels (incl. [5], ).
#
# Out-of-scope: rc=0 (all 7 conditions met → dead) requires constructing
# a fixture agent dir with controlled state across 6 sub-probes. Manual
# verification is documented in the goal description ( check #3:
# two-terminal reproduction). The /start --recover behavior tests live
# in /start SKILL.md's LLM-orchestrated invocation pseudocode.
#
# Run: bash core/scripts/tests/test_runner_dead_check.sh
# Exit 0 = all pass, 1 = any failure.

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="$SCRIPT_DIR/../runner-dead-check.sh"

if [[ ! -f "$HELPER" ]]; then
    echo "FAIL: helper not found at $HELPER"
    exit 1
fi

failures=0
pass_count=0

# Determine current agent (for the live-runner test). Skip live tests if
# MIND_AGENT is not set in the parent env — the live test requires a
# real running agent with session/agent-state == RUNNING.
SELF_AGENT="${MIND_AGENT:-}"

# ─── Scenario 1: MIND_AGENT unset → rc=2 ──────────────────────────────
out=$(unset MIND_AGENT; bash "$HELPER" 2>/dev/null); rc=$?
if [[ "$rc" -eq 2 ]]; then
    pass_count=$((pass_count+1))
    echo "PASS: scenario 1 (MIND_AGENT unset → rc=2)"
else
    failures=$((failures+1))
    echo "FAIL: scenario 1 expected rc=2, got rc=$rc, stdout=$out"
fi

# JSON output even on error case
if echo "$out" | py -3 -c "import json,sys; d=json.loads(sys.stdin.read()); assert 'error' in d, 'expected error key'; sys.exit(0)" 2>/dev/null; then
    pass_count=$((pass_count+1))
    echo "PASS: scenario 1 (JSON output well-formed with 'error' key)"
else
    failures=$((failures+1))
    echo "FAIL: scenario 1 JSON not well-formed or missing error key. stdout=$out"
fi

# ─── Scenario 2: MIND_AGENT=nonexistent → rc=2 ────────────────────────
out=$(MIND_AGENT="nonexistent_agent_xyz_test" bash "$HELPER" 2>/dev/null); rc=$?
if [[ "$rc" -eq 2 ]]; then
    pass_count=$((pass_count+1))
    echo "PASS: scenario 2 (nonexistent agent dir → rc=2)"
else
    failures=$((failures+1))
    echo "FAIL: scenario 2 expected rc=2, got rc=$rc, stdout=$out"
fi

if echo "$out" | py -3 -c "import json,sys; d=json.loads(sys.stdin.read()); assert d.get('dead') is False, 'expected dead=false on error'; sys.exit(0)" 2>/dev/null; then
    pass_count=$((pass_count+1))
    echo "PASS: scenario 2 (JSON: dead=false on error)"
else
    failures=$((failures+1))
    echo "FAIL: scenario 2 JSON missing dead=false. stdout=$out"
fi

# ─── Scenario 3: bound agent → rc=1 (not dead), JSON shape by ROLE ─────
# Runs whenever MIND_AGENT is set in the parent env. That does NOT mean
# "this box is the live RUNNING reducer": under one-mind-two-bodies a
# worker Body box (and any assistant/reader session) binds the agent with
# agent-state=IDLE and writes no runner-heartbeat BY DESIGN. Asserting
# state_running=true + heartbeat_stale=false here encoded "the bound agent
# on this box is a live RUNNING reducer" as an invariant, so the scenario
# was RED on every worker-role box permanently while the helper was
# correct (; a reducer box cannot observe that red at all).
# The expectations therefore BRANCH on the observed role rather than
# SKIP: a skip would silently drop scenario 3 on every worker box, which
# is the coverage-loss shape this test family exists to prevent.
#   reducer (agent-state == RUNNING): dead=false, state_running=true,
#       heartbeat_stale=false (heartbeat-tick fired this turn).
#   any other role (IDLE): dead=false, state_running=false — nothing to
#       recover. heartbeat_stale is NOT asserted there: it is not
#       role-determined (a reducer that stopped inside the staleness
#       window reads fresh while IDLE), and the helper's rc=1 already
#       proves the gate short-circuited on condition 1.
if [[ -n "$SELF_AGENT" ]]; then
    self_state=$(MIND_AGENT="$SELF_AGENT" bash "$SCRIPT_DIR/../session-state-get.sh" 2>/dev/null | tr -d '[:space:]')
    if [[ "$self_state" == "RUNNING" ]]; then
        role="reducer"; expect_running="True"
    else
        role="non-reducer (agent-state=${self_state:-unknown})"; expect_running="False"
    fi
    out=$(MIND_AGENT="$SELF_AGENT" bash "$HELPER" 2>/dev/null); rc=$?
    if [[ "$rc" -eq 1 ]]; then
        pass_count=$((pass_count+1))
        echo "PASS: scenario 3 (bound agent=$SELF_AGENT role=$role → rc=1 not-dead)"
    else
        failures=$((failures+1))
        echo "FAIL: scenario 3 expected rc=1 (not dead), got rc=$rc — role=$role; stdout=$out"
    fi

    # JSON validation: dead=false always; state_running mirrors the observed
    # agent-state; heartbeat_stale=false only where a heartbeat is owed (reducer).
    if echo "$out" | EXPECT_RUNNING="$expect_running" py -3 -c "
import json, os, sys
d = json.loads(sys.stdin.read())
assert d.get('dead') is False, f'expected dead=false, got {d.get(\"dead\")}'
conds = d.get('conditions', {})
expect_running = os.environ['EXPECT_RUNNING'] == 'True'
assert conds.get('state_running') is expect_running, f'expected state_running={expect_running}, got {conds.get(\"state_running\")}'
if expect_running:
    # heartbeat_stale should be false (heartbeat fresh for a live reducer)
    assert conds.get('heartbeat_stale') is False, f'expected heartbeat_stale=false, got {conds.get(\"heartbeat_stale\")}'
" 2>/dev/null; then
        pass_count=$((pass_count+1))
        if [[ "$expect_running" == "True" ]]; then
            echo "PASS: scenario 3 (JSON: dead=false, state_running=true, heartbeat_stale=false)"
        else
            echo "PASS: scenario 3 (JSON: dead=false, state_running=false — $role, nothing to recover)"
        fi
    else
        failures=$((failures+1))
        echo "FAIL: scenario 3 JSON validation failed (role=$role, expected state_running=$expect_running). stdout=$out"
    fi
else
    echo "SKIP: scenario 3 (no MIND_AGENT in parent env — bound-agent test requires a bound session)"
fi

# ─── Scenario 4: JSON output structure (general) ───────────────────────
if [[ -n "$SELF_AGENT" ]]; then
    out=$(MIND_AGENT="$SELF_AGENT" bash "$HELPER" 2>/dev/null)
    if echo "$out" | py -3 -c "
import json, sys
d = json.loads(sys.stdin.read())
expected_conds = {'state_running', 'heartbeat_stale', 'no_recent_block',
                  'diary_stale', 'no_stop_requested', 'no_background_jobs',
                  'no_life_evidence'}  # [5] pre-kill re-check, 
assert d.get('heartbeat') in ('fresh', 'stale', 'absent'), f'heartbeat must be three-way, got {d.get("heartbeat")!r}'
assert 'life_evidence' in d, 'life_evidence key must be present (null when the re-check did not run)'
got_conds = set(d.get('conditions', {}).keys())
missing = expected_conds - got_conds
assert not missing, f'missing conditions: {missing}'
# Messages must mirror conditions
expected_msgs = expected_conds
got_msgs = set(d.get('messages', {}).keys())
assert not (expected_msgs - got_msgs), f'missing messages: {expected_msgs - got_msgs}'
# diary_age_min must be numeric
assert isinstance(d.get('diary_age_min'), int), 'diary_age_min must be int'
" 2>/dev/null; then
        pass_count=$((pass_count+1))
        echo "PASS: scenario 4 (JSON has all 7 condition booleans + messages + diary_age_min + heartbeat/life_evidence)"
    else
        failures=$((failures+1))
        echo "FAIL: scenario 4 JSON structure incomplete. stdout=$out"
    fi
else
    echo "SKIP: scenario 4 (no MIND_AGENT)"
fi

# ─── Scenario 5: stderr has 7 per-condition labels ─────────────────────
if [[ -n "$SELF_AGENT" ]]; then
    err=$(MIND_AGENT="$SELF_AGENT" bash "$HELPER" 2>&1 >/dev/null)
    label_count=$(echo "$err" | grep -cE '^\s*\[(1|2|2\.5|2\.7|3|4|5)\]')
    if [[ "$label_count" -eq 7 ]]; then
        pass_count=$((pass_count+1))
        echo "PASS: scenario 5 (stderr has 7 per-condition labels)"
    else
        failures=$((failures+1))
        echo "FAIL: scenario 5 expected 7 condition labels, got $label_count. stderr=$err"
    fi

    # Verdict line present
    if echo "$err" | grep -qE '^runner-dead-check: agent=.* verdict=(DEAD|ALIVE)'; then
        pass_count=$((pass_count+1))
        echo "PASS: scenario 5 (stderr has verdict line)"
    else
        failures=$((failures+1))
        echo "FAIL: scenario 5 missing verdict line. stderr=$err"
    fi
else
    echo "SKIP: scenario 5 (no MIND_AGENT)"
fi

echo
echo "─────────────────────────────────────"
echo "Summary: $pass_count passed, $failures failed"

if [[ "$failures" -gt 0 ]]; then
    exit 1
fi
exit 0
