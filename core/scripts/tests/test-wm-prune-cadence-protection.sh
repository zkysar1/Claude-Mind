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

# HERMETIC BY CONSTRUCTION (). This test used to seed its fixtures
# into the agent's LIVE working memory, `cp` a backup, and restore with a
# whole-file `mv` from `trap cleanup EXIT`. Both halves were unsafe and both
# fired in production:
#   (1) the trap does NOT cover SIGKILL or a killed process group (a Bash-tool
#       timeout kills the group), so fixtures leaked into a live agent's memory
#       and SURVIVED an autocompact — bravo/cc-05 2026-08-28 found
#       `last_strategic_scan_tick: test_value_for_last_strategic_scan_tick`
#       sitting in production state;
#   (2) the restore was a whole-file `mv` of a snapshot taken BEFORE the run, so
#       any write the live loop made in between was silently discarded — a lost
#       update on the agent's own memory caused by running the tests. cc-09
#       2026-09-03 measured the near-miss (7 capture appends survived only
#       because the `cp` happened to land after them); cc-10 2026-09-04 hit hard
#       reds 3/3 with real live values quoted in the failure text.
# The exposure is not rare, it is the DEFAULT: run-full-suite.sh invokes
# run-invisible-suites.sh automatically, and a Body running the suite is by
# definition mid-unit writing captures.
#
# THE FIX USES AN EXISTING SEAM, not a new override. The daemon resolves the
# working-memory path per request from the `X-Mind-Sid` header (`_runtime.sh`
# adds it; `wm_write.py::_wm_path` reads it), and `agent_paths.py::wm_path()`
# routes to `sessions/<unit_key>/working-memory.yaml` WHENEVER THAT FILE EXISTS,
# falling back to the agent-wide file otherwise. So a throwaway SID with a
# seeded per-session file gives both `wm-prune.sh` and `wm-read.sh` a private
# target, and the live file is never opened for writing at all. Nothing to
# restore means SIGKILL is harmless: failure mode (1) is removed by construction
# rather than guarded against, and (2) cannot occur because no snapshot of live
# state is ever taken.
# NOTE `BODY_WM_PATH` is deliberately NOT the seam — it is honored by
# `core/scripts/wm.py` but appears NOWHERE under `mind_api/src/`, and these
# wrappers are daemon-only, so setting it would redirect nothing.
#
# _platform.sh converts MSYS /c/... paths to Windows C:/... paths for Python
# subprocesses launched via python3.
TEST_SID="test-wm-prune-$$"
TEST_SESSION_DIR="$AGENT_DIR/sessions/$TEST_SID"
WM_FILE="$TEST_SESSION_DIR/working-memory.yaml"

# Route every daemon-backed wm-*.sh call in this test at the throwaway target.
export MIND_SID="$TEST_SID"

cleanup() {
    # Best-effort ONLY. Nothing live depends on this running: the live file was
    # never written, so a SIGKILL here leaks at most a temp session dir, which
    # cleanup-stale-bindings.sh reaps.
    [ -n "${TEST_SESSION_DIR:-}" ] && rm -rf "$TEST_SESSION_DIR"
}
trap cleanup EXIT

mkdir -p "$TEST_SESSION_DIR"

# Set up test state: inject 4 slots with stale timestamps.
python3 -c "
import yaml
from datetime import datetime, timedelta
wm_path = r'$WM_FILE'
# Built from scratch, never copied from live state: the fixture must not depend
# on — or be able to leak — whatever the agent happens to have in memory.
data = {}
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

# PRE-FLIGHT ROUTING GUARD — placed AFTER the seed and BEFORE prune, and that
# order is the whole point. The hazard is not that $WM_FILE is wrong (it is
# assigned literally, above); it is that the DAEMON might not honour the sid and
# fall back to the agent-wide target, in which case `wm-prune.sh` would mutate
# live state. The case assertions below would then fail — but only AFTER prune
# had already run, which is precisely the damage being prevented. So prove the
# routing works while nothing destructive has happened yet: read one seeded slot
# back THROUGH THE DAEMON and require the fixture value.
#
# Deliberately expressed WITHOUT naming the agent-wide path. `analyze_shell()` in
# check-tests-no-live-agent-wm.py flags any code line carrying the WM filename
# beside an ambient-agent-dir token, and cannot tell a comparison from a write —
# so a guard phrased as "!= the live path" would keep this file flagged forever
# and freeze its SHELL_ALLOWLIST entry in place, defeating the self-retirement
# that allowlist is built around.
_routing_probe=$(bash "$CORE_ROOT/scripts/wm-read.sh" last_fresh_eyes_review 2>/dev/null || echo "ERR")
if [ "$_routing_probe" != "test_value_for_last_fresh_eyes_review" ]; then
    echo "TEST FAIL: refusing to run prune — per-session routing is not in effect."
    echo "  read-back of a seeded slot returned '$_routing_probe', not the fixture value."
    echo "  Running prune now would mutate the agent's live working memory (g-115-8189)."
    exit 1
fi

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
