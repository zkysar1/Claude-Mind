#!/usr/bin/env bash
# test_aspirations_claim_source_flag.sh —  regression test.
#
# Verifies aspirations-claim.sh accept-and-ignores `--source {world|agent}`
# instead of letting the value leak into the positional agent_name slot.
#
# Canonical incident (alpha session 2026-05-16, iter post-compaction):
# Skill digests broadly instruct passing `--source {source}` to all
# downstream aspirations-*.sh calls. aspirations-claim.sh used to fall
# through to its `-*` catch-all (shift 1, not 2), leaving the flag's
# value to be parsed as the positional agent_name. With invocation
# `aspirations-claim.sh g-NNN --source world`, AGENT became "world",
# producing a phantom claimed_by=world that required three manual
# repair calls to clear.
#
# Strategy: use `bash -x` trace to inspect the QUERY string built by
# the wrapper. The wrapper exits 0 with goal_not_found for fake goal
# ids, so QUERY assembly happens BEFORE the daemon round-trip exits.
# Grep the trace for `QUERY=id=<id>&agent=<agent>` and assert the
# agent portion is the expected value (not 'world').
#
# Run: bash core/scripts/tests/test_aspirations_claim_source_flag.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRAPPER="$SCRIPT_DIR/../aspirations-claim.sh"

if [[ ! -f "$WRAPPER" ]]; then
  echo "FAIL: wrapper not found at $WRAPPER" >&2
  exit 1
fi

PASS=0
FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS+1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

# Capture the QUERY string by tracing the wrapper. The fake goal id ensures
# the daemon will return goal_not_found regardless of what's claimed, so the
# wrapper never mutates real state.
extract_query() {
  local args=("$@")
  local trace
  # bash -x writes to stderr; redirect 2>&1 so we can grep both streams.
  trace=$(MIND_AGENT=zeta bash -x "$WRAPPER" "${args[@]}" 2>&1 || true)
  # The wrapper sets QUERY="id=...&agent=..." on a single line. Match a `+`
  # trace line so we don't accidentally pick up the daemon's own response.
  # bash -x quotes the value with single-quotes if it contains shell-special
  # characters (& is special); strip the outer quotes for clean comparison.
  printf '%s\n' "$trace" | grep -E '^\+ QUERY=' | tail -1 | sed "s/^+ QUERY=//; s/^'//; s/'$//"
}

echo "Test 1: --source first then goal-id"
q=$(extract_query --source world g-fake-test-001)
if [[ "$q" == "id=g-fake-test-001&agent=zeta" ]]; then
  pass "--source world g-fake-test-001 → '$q'"
else
  fail "expected 'id=g-fake-test-001&agent=zeta', got '$q'"
fi

echo "Test 2: goal-id first then --source (alpha-incident shape)"
q=$(extract_query g-fake-test-002 --source world)
if [[ "$q" == "id=g-fake-test-002&agent=zeta" ]]; then
  pass "g-fake-test-002 --source world → '$q'"
else
  fail "expected 'id=g-fake-test-002&agent=zeta', got '$q'"
fi

echo "Test 3: --source agent variant"
q=$(extract_query --source agent g-fake-test-003)
if [[ "$q" == "id=g-fake-test-003&agent=zeta" ]]; then
  pass "--source agent g-fake-test-003 → '$q'"
else
  fail "expected 'id=g-fake-test-003&agent=zeta', got '$q'"
fi

echo "Test 4: explicit positional agent overrides MIND_AGENT (no --source)"
q=$(extract_query g-fake-test-004 alpha)
if [[ "$q" == "id=g-fake-test-004&agent=alpha" ]]; then
  pass "g-fake-test-004 alpha → '$q'"
else
  fail "expected 'id=g-fake-test-004&agent=alpha', got '$q'"
fi

echo "Test 5: --cross-lane still works (regression)"
q=$(extract_query g-fake-test-005 --cross-lane "regression check")
if [[ "$q" == *"id=g-fake-test-005"* ]] && [[ "$q" == *"agent=zeta"* ]] && [[ "$q" == *"cross_lane="* ]]; then
  pass "--cross-lane preserved: '$q'"
else
  fail "expected id+agent+cross_lane query parts, got '$q'"
fi

echo "Test 6: --source AND --cross-lane combined"
q=$(extract_query --source world g-fake-test-006 --cross-lane "combo")
if [[ "$q" == *"id=g-fake-test-006"* ]] && [[ "$q" == *"agent=zeta"* ]] && [[ "$q" == *"cross_lane="* ]]; then
  pass "--source + --cross-lane combined: '$q'"
else
  fail "expected id+agent+cross_lane query parts with --source consumed, got '$q'"
fi

echo
echo "Summary: $PASS passed, $FAIL failed"
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
