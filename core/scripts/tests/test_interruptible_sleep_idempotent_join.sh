#!/usr/bin/env bash
# test_interruptible_sleep_idempotent_join.sh -- regression for 
# (idempotent idle-sleep launch, 2026-09-03).
#
# A harness with no run_in_background timed out the B7.2 launch call five
# times and each retry spawned another 1800s sleep process (three live at
# once). The script now absorbs the retry: a second IDLE launch (QUIESCENCE /
# DRY) while the first is alive JOINS it -- registers nothing, sleeps only
# until the existing wake time -- via the "<pid> <wake_epoch> <job-id>"
# marker at session/.idle-sleep-active.
#   1. join: second DRY launch prints "idle-sleep JOINED", exits 0 when the
#      first sleep ends, and exactly ONE dry-idle-sleep job id was ever
#      registered.
#   2. stale marker (dead pid) is ignored: the launch registers normally,
#      prints "idle-sleep REGISTERED", and the trap retires the marker.
#   3. EXTERNAL_WAIT never writes the marker (mid-goal waits are not idle
#      sleeps and must not be joined by one).
#
# Pattern: mirrors test_interruptible_sleep_bgjob_registration.sh -- isolated
# test agent dir under PROJECT_ROOT/agents, short sleeps, cleanup per case.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORE_SCRIPTS="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$CORE_SCRIPTS/../.." && pwd)"
_AGENTS_PARENT_DIR="agents"
_test_agent_dir() { if [ -n "$_AGENTS_PARENT_DIR" ]; then printf '%s/%s/%s' "$PROJECT_ROOT" "$_AGENTS_PARENT_DIR" "$1"; else printf '%s/%s' "$PROJECT_ROOT" "$1"; fi; }
INT_SLEEP="$CORE_SCRIPTS/interruptible-sleep.sh"

PASS_COUNT=0
FAIL_COUNT=0
FAILURES=()
_pass() { echo "  [PASS] $1"; PASS_COUNT=$((PASS_COUNT + 1)); }
_fail() { FAILURES+=("[FAIL] $1"); FAIL_COUNT=$((FAIL_COUNT + 1)); }

_wait_for_marker() {  # $1=agent $2=timeout_s
  local marker; marker="$(_test_agent_dir "$1")/session/.idle-sleep-active"
  local i
  for (( i=0; i<$2*2; i++ )); do
    [ -s "$marker" ] && return 0
    sleep 0.5
  done
  return 1
}

# -- Case 1: second idle launch joins the live one ---------------------------
case1() {
  local agent="_join-test-$RANDOM"
  local adir; adir="$(_test_agent_dir "$agent")"
  mkdir -p "$adir/session"
  local yaml="$adir/session/background-jobs.yaml"
  MIND_AGENT="$agent" DRY_SLEEP=1 bash "$INT_SLEEP" 14 >/dev/null 2>&1 &
  local first=$!
  if ! _wait_for_marker "$agent" 8; then
    _fail "case1: marker never appeared for the first DRY launch"
    kill -9 "$first" 2>/dev/null; wait "$first" 2>/dev/null; rm -rf "$adir"; return
  fi
  local t0 t1 rc=0 out
  t0=$(date +%s)
  out="$(MIND_AGENT="$agent" DRY_SLEEP=1 bash "$INT_SLEEP" 120 2>/dev/null)" || rc=$?
  t1=$(date +%s)
  local ids; ids="$(grep -o 'dry-idle-sleep-[0-9]*' "$yaml" 2>/dev/null | sort -u | wc -l | tr -d ' ')"
  wait "$first" 2>/dev/null
  rm -rf "$adir"
  if [ "$rc" = "0" ] && [[ "$out" == *"idle-sleep JOINED"* ]] && [ $((t1 - t0)) -lt 40 ] && [ "${ids:-0}" -le 1 ]; then
    _pass "case1 join: second launch joined (rc=0, $((t1-t0))s < 40, distinct job ids=${ids:-0})"
  else
    _fail "case1: rc=$rc (want 0) joined=$([[ "$out" == *"idle-sleep JOINED"* ]] && echo yes || echo no) elapsed=$((t1-t0))s (want <40) ids=${ids:-0} (want <=1) out=[$out]"
  fi
}

# -- Case 2: stale marker (dead pid) is ignored and retired -------------------
case2() {
  local agent="_join-test-$RANDOM"
  local adir; adir="$(_test_agent_dir "$agent")"
  mkdir -p "$adir/session"
  sleep 0.1 &
  local dead=$!; wait "$dead" 2>/dev/null
  echo "$dead $(( $(date +%s) + 300 )) dry-idle-sleep-$dead" > "$adir/session/.idle-sleep-active"
  local rc=0 out
  out="$(MIND_AGENT="$agent" DRY_SLEEP=1 bash "$INT_SLEEP" 3 2>/dev/null)" || rc=$?
  local marker_after="absent"; [ -e "$adir/session/.idle-sleep-active" ] && marker_after="present"
  rm -rf "$adir"
  if [ "$rc" = "0" ] && [[ "$out" == *"idle-sleep REGISTERED"* ]] && [[ "$out" != *"JOINED"* ]] && [ "$marker_after" = "absent" ]; then
    _pass "case2 stale-marker: registered normally, marker retired by the trap"
  else
    _fail "case2: rc=$rc (want 0) marker_after=$marker_after (want absent) out=[$out]"
  fi
}

# -- Case 3: EXTERNAL_WAIT never writes the idle marker ----------------------
case3() {
  local agent="_join-test-$RANDOM"
  local adir; adir="$(_test_agent_dir "$agent")"
  mkdir -p "$adir/session"
  local rc=0 out seen="no" i
  MIND_AGENT="$agent" EXTERNAL_WAIT=1 bash "$INT_SLEEP" 4 >/dev/null 2>&1 &
  local spid=$!
  for (( i=0; i<8; i++ )); do
    [ -e "$adir/session/.idle-sleep-active" ] && seen="yes"
    sleep 0.5
  done
  wait "$spid" || rc=$?
  rm -rf "$adir"
  if [ "$seen" = "no" ] && [ "$rc" = "0" ]; then
    _pass "case3 external-wait: no idle marker written, rc=0"
  else
    _fail "case3: marker_seen=$seen (want no) rc=$rc (want 0)"
  fi
}

case1; case2; case3

echo ""
if [ "$FAIL_COUNT" -gt 0 ]; then
  for f in "${FAILURES[@]}"; do echo "$f"; done
  echo ""
  echo "$FAIL_COUNT/$((PASS_COUNT + FAIL_COUNT)) test(s) failed"
  exit 1
fi
echo "All $PASS_COUNT idempotent-join cases verified (join, stale-marker, external-wait-unmarked)."
exit 0
