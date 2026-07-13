#!/usr/bin/env bash
# test_interruptible_sleep_signal_class.sh — regression for Magic Wand #2
# (alpha session-60 reflection, 2026-05-07).
#
# Verifies that interruptible-sleep.sh splits wake signals into two classes
# and demotes informational signals during QUIESCENCE_SLEEP=1 mode:
#   - blocker class (blocker-cleared, pq-resolved, email-received): always
#     exits 2 outside the debounce window, regardless of QUIESCENCE_SLEEP.
#   - informational class (board-activity, goal-claim-released): exits 2
#     when QUIESCENCE_SLEEP unset/0; consumes file but exits 0 (sleep
#     completes) when QUIESCENCE_SLEEP=1.
#
# Pattern: each case sets up an isolated test agent dir under PROJECT_ROOT,
# pre-creates the wake signal, runs interruptible-sleep.sh with a short
# duration (2s), captures exit code, and verifies file consumption + exit.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORE_SCRIPTS="$(cd "$SCRIPT_DIR/.." && pwd)"
# core/scripts → core/scripts/.. = core; need one more "/.." to reach project root.
PROJECT_ROOT="$(cd "$CORE_SCRIPTS/../.." && pwd)"
# Phase 2.5.C: sync with _paths.sh AGENTS_PARENT_DIR
_AGENTS_PARENT_DIR="agents"
_test_agent_dir() { if [ -n "$_AGENTS_PARENT_DIR" ]; then printf '%s/%s/%s' "$PROJECT_ROOT" "$_AGENTS_PARENT_DIR" "$1"; else printf '%s/%s' "$PROJECT_ROOT" "$1"; fi; }
INT_SLEEP="$CORE_SCRIPTS/interruptible-sleep.sh"

PASS_COUNT=0
FAIL_COUNT=0
FAILURES=()

run_case() {
  local cid="$1"
  local signal_file_basename="$2"
  local quiescence_env="$3"
  local expected_rc="$4"
  local expected_file_consumed="$5"  # "yes" or "no"

  local agent_name="_mw2-test-$RANDOM"
  local agent_dir="$(_test_agent_dir "$agent_name")"
  mkdir -p "$agent_dir/session"

  # Clear any debounce state from prior runs (different agent dir, but for
  # safety).
  rm -f "$agent_dir/session/.wake-debounce-mtime"

  # Pre-create the wake signal — with mtime stamped 3s in the FUTURE so it
  # reads as a mid-sleep arrival. Without the stamp, this harness races the
  #  first-fire stale-signal fix: whenever the epoch second ticks
  # between file-create and the script's SLEEP_START capture (likelier the
  # slower the box / the more the script does before its loop), production
  # CORRECTLY classifies the file as stale (mtime < SLEEP_START → consume,
  # no wake) and the case flakes rc=0. Pre-fix this suite passed only by
  # same-second luck (observed 3-7/10 flaky failures, 2026-07-10). The
  # future mtime is what the cases semantically mean: a live wake event
  # arriving during the sleep.
  # +30s (not +3): the stamp must exceed WORST-CASE script startup (bash
  # spawn + _paths.sh source + the 7 quiescent-mode bg-job python
  # registration) on a cold/AV-loaded box — at +3s the quiescent cases still
  # flaked when startup crossed the stamp. Cases exit at i=0/1 regardless,
  # so the far-future mtime adds zero runtime.
  local signal_path="$agent_dir/session/$signal_file_basename"
  : > "$signal_path"
  touch -m -d "@$(( $(date +%s) + 30 ))" "$signal_path" 2>/dev/null || true

  # Run the sleep with a short duration. With the signal pre-present, the
  # polling loop should hit it on iteration 0 (or 1).
  local rc=0
  local actual_rc
  MIND_AGENT="$agent_name" QUIESCENCE_SLEEP="$quiescence_env" \
    bash "$INT_SLEEP" 2 >/dev/null 2>&1 || rc=$?
  actual_rc=$rc

  # Verify file consumption.
  local actual_consumed
  if [ -e "$signal_path" ]; then
    actual_consumed="no"
  else
    actual_consumed="yes"
  fi

  # Cleanup
  rm -rf "$agent_dir"

  if [ "$actual_rc" = "$expected_rc" ] && [ "$actual_consumed" = "$expected_file_consumed" ]; then
    echo "  [PASS] $cid: rc=$actual_rc consumed=$actual_consumed"
    PASS_COUNT=$((PASS_COUNT + 1))
  else
    FAILURES+=("[FAIL] $cid: rc=$actual_rc (expected $expected_rc), consumed=$actual_consumed (expected $expected_file_consumed)")
    FAIL_COUNT=$((FAIL_COUNT + 1))
  fi
}

# Cases:
#   case-id, signal-file-basename, QUIESCENCE_SLEEP env, expected-rc, expected-consumed

# ── Blocker class: always exits 2, regardless of QUIESCENCE_SLEEP ───────
run_case "blocker-cleared-default"   "blocker-cleared" ""  2 "yes"
run_case "blocker-cleared-quiescent" "blocker-cleared" "1" 2 "yes"
run_case "pq-resolved-default"       "pq-resolved"     ""  2 "yes"
run_case "pq-resolved-quiescent"     "pq-resolved"     "1" 2 "yes"
run_case "email-received-default"    "email-received"  ""  2 "yes"
run_case "email-received-quiescent"  "email-received"  "1" 2 "yes"

# ── Informational class: exits 2 in default mode, demoted in quiescent ──
run_case "board-activity-default"    "board-activity"      ""  2 "yes"
run_case "board-activity-quiescent"  "board-activity"      "1" 0 "yes"
run_case "claim-released-default"    "goal-claim-released" ""  2 "yes"
run_case "claim-released-quiescent"  "goal-claim-released" "1" 0 "yes"

echo ""
if [ "$FAIL_COUNT" -gt 0 ]; then
  for f in "${FAILURES[@]}"; do
    echo "$f"
  done
  echo ""
  echo "$FAIL_COUNT/$((PASS_COUNT + FAIL_COUNT)) test(s) failed"
  exit 1
fi
echo "All $PASS_COUNT signal-class cases verified (6 blocker, 4 informational, including 2 demoted)."
exit 0
