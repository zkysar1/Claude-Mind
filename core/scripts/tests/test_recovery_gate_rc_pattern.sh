#!/usr/bin/env bash
# test_recovery_gate_rc_pattern.sh — 
#
# Verifies the rc-pattern fix for stop-requested gates in recovery-gate.sh:
# _check_state_corruption (Path B), _check_hung_autocompact (Path C),
# _check_wedged_loop (Path D, ), and run_gate_for_agent Cond 3
# (Path A, extracted to runner-dead-check.sh). All four sites must mirror
# Cond 2.5/Cond 4 (rb-762): rc=1 is the ONLY "continue" code; rc=0 (signal
# exists) AND rc=2+ (script error) both suppress recovery.
#
# Strategy: probe the rc-pattern idiom in isolation via a simulated wrapper
# that exits with a configurable rc. Doesn't invoke the full gate (would
# have side effects on agent state). Mirrors the per-condition probe style
# of test_recovery_gate_diary_freshness.sh.
#
# Three scenarios cover the idiom's truth table:
#   1. wrapper rc=0  → idiom suppresses (signal exists, /stop in progress)
#   2. wrapper rc=1  → idiom continues (no signal — proceed to next check)
#   3. wrapper rc=2  → idiom suppresses (script error, conservative behavior)
#
# Plus two regression checks against recovery-gate.sh on disk:
#   4. Exactly 3 occurrences of `local sr_rc=$?` (one per site)
#   5. Zero occurrences of the legacy `if subprocess; then return 0; fi`
#      idiom against session-signal-exists.sh stop-requested.
#
# Run: bash core/scripts/tests/test_recovery_gate_rc_pattern.sh
# Exit 0 = all pass, 1 = any failure.

set -uo pipefail
TMP=$(mktemp -d)
trap "rm -rf '$TMP'" EXIT
failures=0

# ─── Build a wrapper that exits with rc from PROBE_RC (default 1) ──
WRAP="$TMP/probe.sh"
cat >"$WRAP" <<'EOF'
#!/usr/bin/env bash
exit "${PROBE_RC:-1}"
EOF
chmod +x "$WRAP"

# Returns 0 if the canonical idiom would SUPPRESS recovery
# (caller's `return 0` fires), returns 1 if the idiom CONTINUES
# (caller falls through to _perform_recovery).
#
# Mirrors the real recovery-gate.sh code path:
#   "$WRAP" >/dev/null 2>&1
#   rc=$?
#   [[ $rc -eq 1 ]] || return 0
probe_pattern_suppresses() {
    local rc
    "$WRAP" >/dev/null 2>&1
    rc=$?
    if [[ $rc -eq 1 ]]; then
        return 1   # idiom continues (no `|| return 0` short-circuit)
    else
        return 0   # idiom triggers `|| return 0` — caller suppresses
    fi
}

# ─── Scenario 1: rc=0 (signal exists) → idiom suppresses ──
PROBE_RC=0 probe_pattern_suppresses
if [[ $? -eq 0 ]]; then
    echo "PASS: scenario 1 (probe rc=0, signal exists) → idiom suppresses recovery"
else
    echo "FAIL: scenario 1 — expected idiom to suppress on rc=0"
    failures=$((failures+1))
fi

# ─── Scenario 2: rc=1 (signal missing) → idiom continues ──
PROBE_RC=1 probe_pattern_suppresses
if [[ $? -eq 1 ]]; then
    echo "PASS: scenario 2 (probe rc=1, no signal) → idiom continues to next condition"
else
    echo "FAIL: scenario 2 — expected idiom to continue on rc=1"
    failures=$((failures+1))
fi

# ─── Scenario 3: rc=2 (script error) → idiom conservatively suppresses ──
PROBE_RC=2 probe_pattern_suppresses
if [[ $? -eq 0 ]]; then
    echo "PASS: scenario 3 (probe rc=2, error) → idiom conservatively suppresses recovery"
else
    echo "FAIL: scenario 3 — expected idiom to suppress on rc=2 (rb-762 conservative)"
    failures=$((failures+1))
fi

# ─── Regression checks against the on-disk scripts ──
#  (2026-05-19) extracted run_gate_for_agent's 6-condition gate to
# core/scripts/runner-dead-check.sh. The sr_rc idiom now lives there for
# the Path-A stop-requested check; Paths B/C still inline their checks in
# recovery-gate.sh. Audit both files together.
RG_SH="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)/recovery-gate.sh"
RDC_SH="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)/runner-dead-check.sh"
if [[ ! -f "$RG_SH" ]]; then
    echo "FAIL: recovery-gate.sh not found at $RG_SH"
    failures=$((failures+1))
elif [[ ! -f "$RDC_SH" ]]; then
    echo "FAIL: runner-dead-check.sh not found at $RDC_SH (g-115-947 helper)"
    failures=$((failures+1))
else
    # Scenario 4: exactly 4 sr_rc sites total across the two files —
    # Path B (recovery-gate.sh:_check_state_corruption), Path C
    # (recovery-gate.sh:_check_hung_autocompact), Path D
    # (recovery-gate.sh:_check_wedged_loop, ), and Path A (now in
    # runner-dead-check.sh after the  extraction).
    # `grep -c` returns 1 on zero matches, so pipe to wc -l for a stable count.
    sr_count_rg=$(grep "local sr_rc=" "$RG_SH" 2>/dev/null | wc -l | tr -d '[:space:]')
    sr_count_rdc=$(grep "^[[:space:]]*sr_rc=" "$RDC_SH" 2>/dev/null | wc -l | tr -d '[:space:]')
    sr_count_total=$((sr_count_rg + sr_count_rdc))
    if [[ "$sr_count_total" == "4" ]]; then
        echo "PASS: scenario 4 — all 4 stop-requested sites use sr_rc idiom (recovery-gate.sh=$sr_count_rg + runner-dead-check.sh=$sr_count_rdc)"
    else
        echo "FAIL: scenario 4 — expected 4 sr_rc sites total, found $sr_count_total (recovery-gate.sh=$sr_count_rg + runner-dead-check.sh=$sr_count_rdc)"
        failures=$((failures+1))
    fi

    # Scenario 5: zero legacy `if subprocess; then return 0; fi` idioms
    # against session-signal-exists.sh stop-requested in either file.
    old_count_rg=$(grep -E "if MIND_AGENT=.*bash.*session-signal-exists\.sh.*stop-requested.*; then" "$RG_SH" 2>/dev/null | wc -l | tr -d '[:space:]')
    old_count_rdc=$(grep -E "if MIND_AGENT=.*bash.*session-signal-exists\.sh.*stop-requested.*; then" "$RDC_SH" 2>/dev/null | wc -l | tr -d '[:space:]')
    old_count_total=$((old_count_rg + old_count_rdc))
    if [[ "$old_count_total" == "0" ]]; then
        echo "PASS: scenario 5 — no remaining legacy 'if subprocess; then return 0' idiom in either file"
    else
        echo "FAIL: scenario 5 — found $old_count_total residual legacy idiom site(s)"
        failures=$((failures+1))
    fi
fi

if [[ $failures -eq 0 ]]; then
    echo ""
    echo "All scenarios passed (rb-762 sister-cases fix verified)."
    exit 0
else
    echo ""
    echo "$failures scenario(s) FAILED"
    exit 1
fi
