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
#   3. Exit code 1 for the live runner (heartbeat fresh → alive).
#   4. JSON output is well-formed and contains the 6 condition booleans.
#   5. Stderr contains a verdict line + 6 per-condition labels.
#
# Out-of-scope: rc=0 (all 6 conditions met → dead) requires constructing
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

# ─── Scenario 3: live runner → rc=1 (ALIVE; heartbeat fresh) ───────────
# Only runs when MIND_AGENT is set in parent env (means this test is
# running in an actively-bound autonomous session — heartbeat-tick has
# fired on this turn).
if [[ -n "$SELF_AGENT" ]]; then
    out=$(MIND_AGENT="$SELF_AGENT" bash "$HELPER" 2>/dev/null); rc=$?
    if [[ "$rc" -eq 1 ]]; then
        pass_count=$((pass_count+1))
        echo "PASS: scenario 3 (live agent=$SELF_AGENT → rc=1 ALIVE)"
    else
        failures=$((failures+1))
        echo "FAIL: scenario 3 expected rc=1 (alive), got rc=$rc — heartbeat may not be fresh; stdout=$out"
    fi

    # JSON validation: dead=false AND state_running=true (since we ARE running)
    if echo "$out" | py -3 -c "
import json, sys
d = json.loads(sys.stdin.read())
assert d.get('dead') is False, f'expected dead=false, got {d.get(\"dead\")}'
conds = d.get('conditions', {})
assert conds.get('state_running') is True, f'expected state_running=true, got {conds.get(\"state_running\")}'
# heartbeat_stale should be false (heartbeat fresh for live runner)
assert conds.get('heartbeat_stale') is False, f'expected heartbeat_stale=false, got {conds.get(\"heartbeat_stale\")}'
" 2>/dev/null; then
        pass_count=$((pass_count+1))
        echo "PASS: scenario 3 (JSON: dead=false, state_running=true, heartbeat_stale=false)"
    else
        failures=$((failures+1))
        echo "FAIL: scenario 3 JSON validation failed. stdout=$out"
    fi
else
    echo "SKIP: scenario 3 (no MIND_AGENT in parent env — live-runner test requires bound session)"
fi

# ─── Scenario 4: JSON output structure (general) ───────────────────────
if [[ -n "$SELF_AGENT" ]]; then
    out=$(MIND_AGENT="$SELF_AGENT" bash "$HELPER" 2>/dev/null)
    if echo "$out" | py -3 -c "
import json, sys
d = json.loads(sys.stdin.read())
expected_conds = {'state_running', 'heartbeat_stale', 'no_recent_block',
                  'diary_stale', 'no_stop_requested', 'no_background_jobs'}
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
        echo "PASS: scenario 4 (JSON has all 6 condition booleans + messages + diary_age_min)"
    else
        failures=$((failures+1))
        echo "FAIL: scenario 4 JSON structure incomplete. stdout=$out"
    fi
else
    echo "SKIP: scenario 4 (no MIND_AGENT)"
fi

# ─── Scenario 5: stderr has 6 per-condition labels ─────────────────────
if [[ -n "$SELF_AGENT" ]]; then
    err=$(MIND_AGENT="$SELF_AGENT" bash "$HELPER" 2>&1 >/dev/null)
    label_count=$(echo "$err" | grep -cE '^\s*\[(1|2|2\.5|2\.7|3|4)\]')
    if [[ "$label_count" -eq 6 ]]; then
        pass_count=$((pass_count+1))
        echo "PASS: scenario 5 (stderr has 6 per-condition labels)"
    else
        failures=$((failures+1))
        echo "FAIL: scenario 5 expected 6 condition labels, got $label_count. stderr=$err"
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
