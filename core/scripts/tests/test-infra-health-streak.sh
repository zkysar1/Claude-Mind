#!/usr/bin/env bash
# test-infra-health-streak.sh — synthetic test for  streak-alert.
#
# Exercises the three decision paths in cmd_failing_streak by varying
# --threshold and --window-hours against a SEEDED fixture (--health-file),
# verifying exit codes and alert_count.
#
# HERMETIC since : the original form read the LIVE
# world/infra-health.yaml and required some component to HAPPEN to be
# mid-streak ("bitnet currently satisfies this... if future maintenance
# clears bitnet, this test needs a seeded fixture file + --health-file
# override" — its own header). Maintenance did clear the live streak, CASE 2
# went red with the checker perfectly healthy, and this is the override the
# author prescribed. --no-sync-blockers keeps the run free of WM writes.
#
# Pass: all 3 cases match expected (exit 0, prints "TEST PASS").
# Fail: exit 1 on any mismatch (prints the mismatch and "TEST FAIL").

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/../_paths.sh"

INFRA="$CORE_ROOT/scripts/infra-health.py"

FIXTURE="$(mktemp)"
trap 'rm -f "$FIXTURE"' EXIT
RECENT="$(date +%Y-%m-%dT%H:%M:%S -d '1 hour ago' 2>/dev/null || python3 -c "from datetime import datetime, timedelta; print((datetime.now()-timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%S'))")"
cat > "$FIXTURE" <<EOF
components:
  synthetic-streaker:
    consecutive_failures: 5
    last_failure: '$RECENT'
  synthetic-healthy:
    consecutive_failures: 0
    last_failure: null
EOF

run_case() {
    local label="$1"
    local expected_exit="$2"
    local expected_alert_min="$3"
    local expected_alert_max="$4"
    shift 4
    local out
    local rc=0
    out=$(python3 "$INFRA" streak-alert --health-file "$FIXTURE" --no-sync-blockers "$@" 2>&1) || rc=$?
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

# Case 2: default threshold, 24h window — synthetic-streaker (5 consecutive,
# last_failure 1h ago) must alert: exactly 1, exit 1
run_case "2/default" 1 1 1 --threshold 3 --window-hours 24

# Case 3: threshold=3 but recency window=0.001h — the 1h-old failure is
# excluded by the recency filter, exit 0
run_case "3/tight-window" 0 0 0 --threshold 3 --window-hours 0.001

echo "TEST PASS: 3 streak-alert cases verified"
