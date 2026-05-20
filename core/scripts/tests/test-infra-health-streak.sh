#!/usr/bin/env bash
# test-infra-health-streak.sh — synthetic test for  streak-alert.
#
# Exercises the three decision paths in cmd_failing_streak by varying
# --threshold and --window-hours, verifying exit codes and alert_count
# against the live infra-health.yaml without mutating it.
#
# Pass: all 3 cases match expected (exit 0, prints "TEST PASS").
# Fail: exit 1 on any mismatch (prints the mismatch and "TEST FAIL").
#
# Uses the live world/infra-health.yaml — requires at least one component
# with consecutive_failures >= 3 and last_failure within the past few hours
# for cases 2+3 to be meaningful. bitnet currently satisfies this from the
# asp-249 original motivation. If future maintenance clears bitnet, this
# test needs a seeded fixture file + --health-file override.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/../_paths.sh"

INFRA="$CORE_ROOT/scripts/infra-health.py"

run_case() {
    local label="$1"
    local expected_exit="$2"
    local expected_alert_min="$3"
    local expected_alert_max="$4"
    shift 4
    local out
    local rc=0
    out=$(python3 "$INFRA" streak-alert "$@" 2>&1) || rc=$?
    local alert_count
    alert_count=$(echo "$out" | python3 -c "import sys, json; print(json.load(sys.stdin).get('alert_count', -1))")
    if [ "$rc" != "$expected_exit" ]; then
        echo "CASE $label FAIL: exit=$rc (expected $expected_exit)"
        echo "$out"
        return 1
    fi
    if [ "$alert_count" -lt "$expected_alert_min" ] || [ "$alert_count" -gt "$expected_alert_max" ]; then
        echo "CASE $label FAIL: alert_count=$alert_count (expected $expected_alert_min..$expected_alert_max)"
        echo "$out"
        return 1
    fi
    echo "CASE $label PASS: exit=$rc alert_count=$alert_count"
    return 0
}

# Case 1: threshold=100 — no component meets it, alert_count=0, exit 0
run_case "1/high-threshold" 0 0 0 --threshold 100 --window-hours 24

# Case 2: default threshold, 24h window — bitnet (consecutive>=3, recent) should alert
run_case "2/default" 1 1 99 --threshold 3 --window-hours 24

# Case 3: threshold=3 but recency window=0.001h — all recent failures excluded
run_case "3/tight-window" 0 0 0 --threshold 3 --window-hours 0.001

echo "TEST PASS: 3 streak-alert cases verified"
