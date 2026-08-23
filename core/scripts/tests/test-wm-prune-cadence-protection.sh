#!/usr/bin/env bash
# test-wm-prune-cadence-protection.sh — regression for .
#
# Verify wm-prune.sh does NOT evict cadence-tracker slots even when they
# exceed evict_threshold_minutes. Cadence trackers fire every N goals or
# N hours — they are stale BY DESIGN. Eviction destroys cadence memory
# and causes duplicate firings (the incident that motivated this fix).
#
# Test cases:
#   1. last_fresh_eyes_review (200 min old) → must survive (matches _review)
#   2. last_evolution_at_time (200 min old) → must survive (matches _at_time)
#   3. last_strategic_scan_tick (200 min old) → must survive (matches _tick)
#   4. last_felt_sense_checkin (200 min old) → must survive (matches _checkin, )
#   5. last_goal_category (200 min old) → MUST be evicted (not a cadence tracker)
#      — the negative control ensuring eviction still works. Since 
#      it is also the control for the TOP_LEVEL_KEYS exclusion in
#      _is_cadence_tracker: it OPENS WITH `last_` and so matches the `^last_`
#      class pattern, but holds a category STRING rather than a timestamp, so
#      it must stay evictable.
#
#      ⚠ THIS CASE MUST NOT BE READ BACK WITH `wm-read.sh` ().
#      `last_goal_category` is a TOP_LEVEL_KEY, so wm-read resolves it to the
#      TOP-LEVEL key — which this test never seeds and prune never touches —
#      instead of to `slots['last_goal_category']`, which is what was seeded
#      and what prune acts on. On any agent with a live category the read-back
#      returned that live value and the case reported
#      `FAIL: expected evicted, got val='<real category>'` while eviction had
#      in fact worked perfectly. The test therefore passed ONLY on an agent
#      whose top-level key happened to be unset, and its failure accused
#      whoever last touched the predicate. Read the SLOT directly instead.
#
# Pass: all 5 cases match expected (exit 0, prints "TEST PASS").
# Fail: exit 1 on any mismatch (prints the mismatch and "TEST FAIL").

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/../_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"

# Backup and restore WM file to avoid polluting live state.
# _platform.sh converts MSYS /c/... paths to Windows C:/... paths for Python
# subprocesses launched via python3.
WM_FILE="$AGENT_DIR/session/working-memory.yaml"
BACKUP="$WM_FILE.test-backup.$$"

cleanup() {
    if [ -f "$BACKUP" ]; then
        mv "$BACKUP" "$WM_FILE"
    fi
}
trap cleanup EXIT

# Snapshot current WM state.
cp "$WM_FILE" "$BACKUP"

# Set up test state: inject 4 slots with stale timestamps.
python3 -c "
import yaml
from datetime import datetime, timedelta
wm_path = r'$WM_FILE'
with open(wm_path, 'r', encoding='utf-8') as f:
    data = yaml.safe_load(f) or {}
stale_ts = (datetime.now() - timedelta(minutes=200)).strftime('%Y-%m-%dT%H:%M:%S')
data.setdefault('slots', {})
data.setdefault('slot_meta', {})
for slot in ['last_fresh_eyes_review', 'last_evolution_at_time', 'last_strategic_scan_tick', 'last_felt_sense_checkin', 'last_goal_category']:
    data['slots'][slot] = 'test_value_for_' + slot
    data['slot_meta'][slot] = {'updated_at': stale_ts, 'accessed_at': stale_ts}
with open(wm_path, 'w', encoding='utf-8') as f:
    yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
print('seeded 5 stale slots (200 min old)')
"

# Run prune.
bash "$CORE_ROOT/scripts/wm-prune.sh" >/tmp/wm-prune-out.json 2>&1
prune_rc=$?
if [ $prune_rc -ne 0 ]; then
    echo "TEST FAIL: wm-prune.sh exited $prune_rc"
    cat /tmp/wm-prune-out.json
    exit 1
fi

# Check each slot.
check_slot() {
    local slot="$1"
    local expect="$2"  # "present" or "evicted"
    local val
    val=$(bash "$CORE_ROOT/scripts/wm-read.sh" "$slot" 2>/dev/null || echo "ERR")
    if [ "$expect" = "present" ]; then
        if [ "$val" = "test_value_for_$slot" ]; then
            echo "CASE $slot PASS: preserved (value intact)"
            return 0
        else
            echo "CASE $slot FAIL: expected preserved, got val='$val'"
            return 1
        fi
    else
        # expect evicted → value should be null / None / empty
        if [ "$val" = "null" ] || [ "$val" = "None" ] || [ -z "$val" ]; then
            echo "CASE $slot PASS: evicted (value cleared)"
            return 0
        else
            echo "CASE $slot FAIL: expected evicted, got val='$val'"
            return 1
        fi
    fi
}

# Read a SLOT by name, bypassing wm-read.sh's TOP_LEVEL_KEYS resolution.
# Required for any case whose name is also a TOP_LEVEL_KEY — see the header.
check_slot_direct() {
    local slot="$1"
    local expect="$2"  # "present" or "evicted"
    local val
    val=$(WM_FILE="$WM_FILE" SLOT="$slot" python3 -c "
import os, yaml
with open(os.environ['WM_FILE'], 'r', encoding='utf-8') as f:
    data = yaml.safe_load(f) or {}
v = (data.get('slots') or {}).get(os.environ['SLOT'])
print('null' if v is None else v)
" 2>/dev/null || echo "ERR")
    if [ "$expect" = "present" ]; then
        if [ "$val" = "test_value_for_$slot" ]; then
            echo "CASE $slot PASS: preserved (slot value intact)"; return 0
        fi
        echo "CASE $slot FAIL: expected preserved, got slots['$slot']='$val'"; return 1
    fi
    if [ "$val" = "null" ]; then
        echo "CASE $slot PASS: evicted (slot cleared)"; return 0
    fi
    echo "CASE $slot FAIL: expected evicted, got slots['$slot']='$val'"; return 1
}

fails=0
check_slot "last_fresh_eyes_review"     "present" || fails=$((fails+1))
check_slot "last_evolution_at_time"     "present" || fails=$((fails+1))
check_slot "last_strategic_scan_tick"   "present" || fails=$((fails+1))
check_slot "last_felt_sense_checkin"    "present" || fails=$((fails+1))
# TOP_LEVEL_KEY name — must read the slot directly (see header).
check_slot_direct "last_goal_category"  "evicted" || fails=$((fails+1))

if [ $fails -gt 0 ]; then
    echo "TEST FAIL: $fails case(s) failed"
    exit 1
fi
echo "TEST PASS: 5 wm-prune cadence-protection cases verified"
