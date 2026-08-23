#!/usr/bin/env bash
# test_interruptible_sleep_bgjob_registration.sh — regression for 
# (quiescence-sleep pacing via background-jobs registration, 2026-07-10).
#
# Verifies that interruptible-sleep.sh under QUIESCENCE_SLEEP=1 registers a
# Tier-A background job (the stop-hook Gate 2.6 has-pending carve-out that
# lets quiescence turn-ends be ALLOWed so the sleep actually paces), and that
# every exit path deregisters — the no-orphaned-rows rail:
#   1. registered-during-quiescent-sleep: row present + has-pending rc=0 while
#      sleeping; natural completion rc=0 clears the row (has-pending rc=1).
#   2. no-registration-default-mode: QUIESCENCE_SLEEP unset → no row, ever
#      (hot-path backoff sleeps stay python-spawn-free).
#   3. stop-request-interrupt-deregisters: stop-requested breaks the sleep
#      within ~1s, exit 0, row deregistered (the /stop-during-sleep rail).
#   4. term-kill-deregisters: SIGTERM → trap chain deregisters the row.
#   5. sigkill-dead-row-not-pending: SIGKILL orphans the row (untrappable) BUT
#      has-pending returns 1 anyway — pid_alive strictness makes the orphan
#      inert for stop-hook/recovery-gate (the safety property that makes this
#      whole mechanism admissible).
#
# Pattern: mirrors test_interruptible_sleep_signal_class.sh — isolated test
# agent dir under PROJECT_ROOT/agents, short sleeps, cleanup per case.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORE_SCRIPTS="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$CORE_SCRIPTS/../.." && pwd)"
# Phase 2.5.C: sync with _paths.sh AGENTS_PARENT_DIR
_AGENTS_PARENT_DIR="agents"
_test_agent_dir() { if [ -n "$_AGENTS_PARENT_DIR" ]; then printf '%s/%s/%s' "$PROJECT_ROOT" "$_AGENTS_PARENT_DIR" "$1"; else printf '%s/%s' "$PROJECT_ROOT" "$1"; fi; }
INT_SLEEP="$CORE_SCRIPTS/interruptible-sleep.sh"
BG_JOBS="$CORE_SCRIPTS/background-jobs.sh"

PASS_COUNT=0
FAIL_COUNT=0
FAILURES=()

_pass() { echo "  [PASS] $1"; PASS_COUNT=$((PASS_COUNT + 1)); }
_fail() { FAILURES+=("[FAIL] $1"); FAIL_COUNT=$((FAIL_COUNT + 1)); }

# Poll until the agent's ledger contains a quiescence-sleep row (or timeout).
# Registration includes a python spawn (~1-3s on Windows); poll generously.
_wait_for_row() {  # $1=agent $2=timeout_s ; rc 0 = row appeared
  local agent="$1" timeout="$2" i
  local yaml
  yaml="$(_test_agent_dir "$agent")/session/background-jobs.yaml"
  for (( i=0; i<timeout*2; i++ )); do
    grep -q "quiescence-sleep-" "$yaml" 2>/dev/null && return 0
    sleep 0.5
  done
  return 1
}

_row_present() {  # $1=agent ; rc 0 = row present
  grep -q "quiescence-sleep-" "$(_test_agent_dir "$1")/session/background-jobs.yaml" 2>/dev/null
}

_has_pending_rc() {  # $1=agent ; echoes has-pending rc
  local rc=0
  MIND_AGENT="$1" bash "$BG_JOBS" has-pending >/dev/null 2>&1 || rc=$?
  echo "$rc"
}

# ── Case 1: registered during quiescent sleep; natural completion clears ──
case1() {
  local agent="_qsbg-test-$RANDOM"
  local adir; adir="$(_test_agent_dir "$agent")"
  mkdir -p "$adir/session"
  local rc=0
  MIND_AGENT="$agent" QUIESCENCE_SLEEP=1 bash "$INT_SLEEP" 8 >/dev/null 2>&1 &
  local spid=$!
  if ! _wait_for_row "$agent" 6; then
    _fail "case1: row never appeared during QUIESCENCE_SLEEP=1 sleep"
    kill -9 "$spid" 2>/dev/null; wait "$spid" 2>/dev/null; rm -rf "$adir"; return
  fi
  local pending_mid; pending_mid="$(_has_pending_rc "$agent")"
  wait "$spid" || rc=$?
  local row_after="absent"; _row_present "$agent" && row_after="present"
  local pending_after; pending_after="$(_has_pending_rc "$agent")"
  rm -rf "$adir"
  if [ "$pending_mid" = "0" ] && [ "$rc" = "0" ] && [ "$row_after" = "absent" ] && [ "$pending_after" = "1" ]; then
    _pass "case1 registered-during-quiescent-sleep: mid has-pending=0, exit rc=0, row cleared, after has-pending=1"
  else
    _fail "case1: pending_mid=$pending_mid (want 0) rc=$rc (want 0) row_after=$row_after (want absent) pending_after=$pending_after (want 1)"
  fi
}

# ── Case 2: default mode (no QUIESCENCE_SLEEP) never registers ────────────
case2() {
  local agent="_qsbg-test-$RANDOM"
  local adir; adir="$(_test_agent_dir "$agent")"
  mkdir -p "$adir/session"
  local rc=0
  MIND_AGENT="$agent" bash "$INT_SLEEP" 4 >/dev/null 2>&1 &
  local spid=$!
  local seen="no" i
  for (( i=0; i<6; i++ )); do
    _row_present "$agent" && seen="yes"
    sleep 0.5
  done
  wait "$spid" || rc=$?
  rm -rf "$adir"
  if [ "$seen" = "no" ] && [ "$rc" = "0" ]; then
    _pass "case2 no-registration-default-mode: no row ever, rc=0"
  else
    _fail "case2: row_seen=$seen (want no) rc=$rc (want 0)"
  fi
}

# ── Case 3: stop-requested interrupt exits 0 fast + deregisters ───────────
case3() {
  local agent="_qsbg-test-$RANDOM"
  local adir; adir="$(_test_agent_dir "$agent")"
  mkdir -p "$adir/session"
  local rc=0 t0 t1
  t0=$(date +%s)
  MIND_AGENT="$agent" QUIESCENCE_SLEEP=1 bash "$INT_SLEEP" 60 >/dev/null 2>&1 &
  local spid=$!
  if ! _wait_for_row "$agent" 6; then
    _fail "case3: row never appeared"; kill -9 "$spid" 2>/dev/null; wait "$spid" 2>/dev/null; rm -rf "$adir"; return
  fi
  : > "$adir/session/stop-requested"
  wait "$spid" || rc=$?
  t1=$(date +%s)
  local row_after="absent"; _row_present "$agent" && row_after="present"
  rm -rf "$adir"
  if [ "$rc" = "0" ] && [ "$row_after" = "absent" ] && [ $((t1 - t0)) -lt 30 ]; then
    _pass "case3 stop-request-interrupt: rc=0 in $((t1-t0))s (<30), row deregistered"
  else
    _fail "case3: rc=$rc (want 0) row_after=$row_after (want absent) elapsed=$((t1-t0))s (want <30)"
  fi
}

# ── Case 4: SIGTERM → trap chain deregisters ──────────────────────────────
case4() {
  local agent="_qsbg-test-$RANDOM"
  local adir; adir="$(_test_agent_dir "$agent")"
  mkdir -p "$adir/session"
  local rc=0
  MIND_AGENT="$agent" QUIESCENCE_SLEEP=1 bash "$INT_SLEEP" 60 >/dev/null 2>&1 &
  local spid=$!
  if ! _wait_for_row "$agent" 6; then
    _fail "case4: row never appeared"; kill -9 "$spid" 2>/dev/null; wait "$spid" 2>/dev/null; rm -rf "$adir"; return
  fi
  kill -TERM "$spid" 2>/dev/null
  wait "$spid" || rc=$?
  # Deregister runs post-trap; allow a short settle for the python spawn.
  local settled="present" i
  for (( i=0; i<10; i++ )); do
    if ! _row_present "$agent"; then settled="absent"; break; fi
    sleep 0.5
  done
  rm -rf "$adir"
  if [ "$settled" = "absent" ] && [ "$rc" != "0" ]; then
    _pass "case4 term-kill-deregisters: row cleared after SIGTERM (rc=$rc, nonzero as expected)"
  else
    _fail "case4: row=$settled (want absent) rc=$rc (want nonzero)"
  fi
}

# ── Case 5: SIGKILL orphan row is INERT for has-pending (dead pid) ────────
case5() {
  local agent="_qsbg-test-$RANDOM"
  local adir; adir="$(_test_agent_dir "$agent")"
  mkdir -p "$adir/session"
  MIND_AGENT="$agent" QUIESCENCE_SLEEP=1 bash "$INT_SLEEP" 60 >/dev/null 2>&1 &
  local spid=$!
  if ! _wait_for_row "$agent" 6; then
    _fail "case5: row never appeared"; kill -9 "$spid" 2>/dev/null; wait "$spid" 2>/dev/null; rm -rf "$adir"; return
  fi
  kill -9 "$spid" 2>/dev/null
  wait "$spid" 2>/dev/null
  # The row may legitimately REMAIN (SIGKILL is untrappable). The safety
  # property under test: has-pending must ignore it because its PID is dead.
  # Give the OS a moment to reap the killed process tree.
  sleep 2
  local pending_after; pending_after="$(_has_pending_rc "$agent")"
  local row_state="absent"; _row_present "$agent" && row_state="present(orphan-ok)"
  rm -rf "$adir"
  if [ "$pending_after" = "1" ]; then
    _pass "case5 sigkill-orphan-inert: has-pending=1 with row $row_state — dead-PID strictness holds"
  else
    _fail "case5: has-pending=$pending_after (want 1) row=$row_state — orphan row still gates stop-hook!"
  fi
}

# ── Case 6: EXTERNAL_WAIT (mid-goal external wait, ) registers ──
# Mirrors case1 but for the external-wait-sleep job type. Self-contained row
# checks (the shared helpers grep the quiescence-sleep- prefix) so cases 1-5
# are untouched.
case6() {
  local agent="_qsbg-test-$RANDOM"
  local adir; adir="$(_test_agent_dir "$agent")"
  mkdir -p "$adir/session"
  local yaml="$adir/session/background-jobs.yaml"
  local rc=0
  MIND_AGENT="$agent" EXTERNAL_WAIT=1 bash "$INT_SLEEP" 8 >/dev/null 2>&1 &
  local spid=$!
  local seen="no" i
  for (( i=0; i<12; i++ )); do
    grep -q "external-wait-sleep-" "$yaml" 2>/dev/null && { seen="yes"; break; }
    sleep 0.5
  done
  local pending_mid; pending_mid="$(_has_pending_rc "$agent")"
  wait "$spid" || rc=$?
  local row_after="absent"; grep -q "external-wait-sleep-" "$yaml" 2>/dev/null && row_after="present"
  local pending_after; pending_after="$(_has_pending_rc "$agent")"
  rm -rf "$adir"
  if [ "$seen" = "yes" ] && [ "$pending_mid" = "0" ] && [ "$rc" = "0" ] && [ "$row_after" = "absent" ] && [ "$pending_after" = "1" ]; then
    _pass "case6 external-wait-registers: row seen, mid has-pending=0, exit rc=0, row cleared, after has-pending=1"
  else
    _fail "case6: seen=$seen (want yes) pending_mid=$pending_mid (want 0) rc=$rc (want 0) row_after=$row_after (want absent) pending_after=$pending_after (want 1)"
  fi
}

case1; case2; case3; case4; case5; case6

echo ""
if [ "$FAIL_COUNT" -gt 0 ]; then
  for f in "${FAILURES[@]}"; do echo "$f"; done
  echo ""
  echo "$FAIL_COUNT/$((PASS_COUNT + FAIL_COUNT)) test(s) failed"
  exit 1
fi
echo "All $PASS_COUNT bg-job registration cases verified (register/natural-clear, default-scoping, stop-interrupt, TERM-trap, SIGKILL-inert, external-wait-registers)."
exit 0
