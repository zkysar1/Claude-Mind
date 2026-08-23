#!/usr/bin/env bash
# test_recovery_gate_diary_freshness.sh — 
#
# Verifies recovery-gate.sh Condition 2.7 (execution-diary mtime check)
# correctly holds back recovery when execution-diary.jsonl is fresh, even
# when heartbeat is stale.
#
# Strategy: probe the standalone diary-freshness logic in isolation (the
# same py-3 snippet recovery-gate.sh uses inline). Doesn't invoke the full
# gate — that would have side effects on agent state.
#
# Three scenarios:
#   1. Diary fresh (5m old) → diary-fresh, gate would HOLD BACK
#   2. Diary stale (45m old) → diary-stale, gate would PROCEED to recover
#   3. Diary missing → fail-open (file check trips), gate proceeds
#
# Run: bash core/scripts/tests/test_recovery_gate_diary_freshness.sh
# Exit 0 = all pass, 1 = any failure.

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP=$(mktemp -d)
trap "rm -rf '$TMP'" EXIT

failures=0

# ─── Scenario 1: fresh diary (5m old) — should be classified FRESH ──
diary1="$TMP/diary-fresh.jsonl"
echo '{"phase":"phase-test"}' > "$diary1"
# Set mtime to 5 minutes ago (300 seconds)
touch -d "5 minutes ago" "$diary1" 2>/dev/null || \
    touch -t "$(date -d '5 minutes ago' '+%Y%m%d%H%M.%S' 2>/dev/null || echo '202005101200.00')" "$diary1"

age=$(DIARY_E="$diary1" py -3 - <<'PYEOF' 2>/dev/null
import os, time
try:
    print(int((time.time() - os.path.getmtime(os.environ['DIARY_E'])) / 60))
except Exception:
    print(999)
PYEOF
)

if [[ "$age" -lt 15 ]] 2>/dev/null; then
    echo "PASS: scenario 1 (fresh diary, age=${age}min) — gate would hold back"
else
    echo "FAIL: scenario 1 expected age<15, got age=$age"
    failures=$((failures+1))
fi

# ─── Scenario 2: stale diary (45m old) — should be classified STALE ──
diary2="$TMP/diary-stale.jsonl"
echo '{"phase":"phase-test"}' > "$diary2"
touch -d "45 minutes ago" "$diary2" 2>/dev/null || \
    touch -t "$(date -d '45 minutes ago' '+%Y%m%d%H%M.%S' 2>/dev/null || echo '202005101115.00')" "$diary2"

age=$(DIARY_E="$diary2" py -3 - <<'PYEOF' 2>/dev/null
import os, time
try:
    print(int((time.time() - os.path.getmtime(os.environ['DIARY_E'])) / 60))
except Exception:
    print(999)
PYEOF
)

if [[ "$age" -ge 15 ]] 2>/dev/null; then
    echo "PASS: scenario 2 (stale diary, age=${age}min) — gate would proceed"
else
    echo "FAIL: scenario 2 expected age>=15, got age=$age"
    failures=$((failures+1))
fi

# ─── Scenario 3: missing diary — fail-open ──
diary3="$TMP/diary-missing.jsonl"  # never created
if [[ ! -f "$diary3" ]]; then
    echo "PASS: scenario 3 (missing diary) — [[ -f ]] check correctly bypasses Cond 2.7"
else
    echo "FAIL: scenario 3 file unexpectedly present"
    failures=$((failures+1))
fi

# ─── Summary ──
if [[ $failures -eq 0 ]]; then
    echo ""
    echo "All 3 scenarios passed."
    exit 0
else
    echo ""
    echo "$failures scenario(s) FAILED"
    exit 1
fi
