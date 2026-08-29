#!/usr/bin/env bash
# test_aspirations_update_goal_source_value.sh — the --source VALUE check on
# aspirations-update-goal.sh.
#
# --source names the STORE a goal lives in (world|agent). The daemon reads it as
# "agent or not-agent" (mind_api/src/endpoints/aspirations_write.py: `if source ==
# "agent" … else world`), so any other token silently became a world lookup and the
# caller was told `goal_not_found … (world)` — an error naming the wrong cause.
#
# Canonical incident (coach reducer, zc-03, 2026-08-29): a small model wrote
# `--source aspirations-execute` (the calling SKILL's name) on its state-update
# writes for a goal that lived in the agent queue; three identical goal_not_found
# replies, ~3 minutes of pod time each, before the model tried something else. The
# wrapper now refuses any value but world|agent, exit 2, naming both values.
#
# Strategy mirrors test_aspirations_claim_source_flag.sh: fake goal ids, so a
# VALID value never mutates real state (the daemon answers goal_not_found), and a
# `bash -x` trace to prove the valid values reach QUERY assembly while the bad one
# never does.
#
# Run: bash core/scripts/tests/test_aspirations_update_goal_source_value.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRAPPER="$SCRIPT_DIR/../aspirations-update-goal.sh"

if [[ ! -f "$WRAPPER" ]]; then
  echo "FAIL: wrapper not found at $WRAPPER" >&2
  exit 1
fi

PASS=0
FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS+1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

run_wrapper() {
  # stdout+stderr and the exit code, never aborting this script.
  local out rc
  out=$(MIND_AGENT=zeta bash -x "$WRAPPER" "$@" 2>&1) && rc=0 || rc=$?
  printf '%s\n' "$out"
  printf 'RC=%s\n' "$rc"
}

echo "Test 1: a bogus --source value is refused, exit 2, both values named, no daemon call"
out=$(run_wrapper g-fake-test-101 status pending --source aspirations-execute)
rc=$(printf '%s\n' "$out" | { grep -E '^RC=' || true; } | tail -1 | sed 's/^RC=//')
if [[ "$rc" == "2" ]]; then
  pass "exit 2 on --source aspirations-execute"
else
  fail "expected exit 2 on a bogus --source, got rc=$rc"
fi
if printf '%s\n' "$out" | grep -q "takes 'world' or 'agent'" \
   && printf '%s\n' "$out" | grep -q "got 'aspirations-execute'"; then
  pass "refusal names the two accepted values and the offending token"
else
  fail "refusal message missing the accepted values / offending token"
fi
if printf '%s\n' "$out" | grep -qE '^\+ QUERY='; then
  fail "the bogus value still reached QUERY assembly (a daemon call would follow)"
else
  pass "refused before QUERY assembly — no daemon round-trip"
fi

for src in world agent; do
  echo "Test: --source $src passes the value check and reaches QUERY assembly"
  out=$(run_wrapper g-fake-test-102 status pending --source "$src")
  if printf '%s\n' "$out" | grep -q "takes 'world' or 'agent'"; then
    fail "--source $src was refused by the value check"
  elif printf '%s\n' "$out" | grep -qE "^\+ QUERY=.*source=$src"; then
    pass "--source $src reached QUERY with source=$src"
  else
    fail "--source $src produced no QUERY line carrying source=$src. Last 5 lines:"
    printf '%s\n' "$out" | tail -5 | sed 's/^/          /' >&2
  fi
done

echo "Test: no --source at all keeps the world default"
out=$(run_wrapper g-fake-test-103 status pending)
if printf '%s\n' "$out" | grep -qE '^\+ QUERY=.*source=world'; then
  pass "default source=world reached QUERY"
else
  fail "default invocation produced no QUERY line with source=world"
fi

echo
echo "Summary: $PASS passed, $FAIL failed"
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
