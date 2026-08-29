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
# THAT PREMISE HAS ONE EXCEPTION, and it is why this file went red
# (). Scorer Sovereignty Layer B () added a gate
# that runs BEFORE QUERY is assembled: aspirations-claim.sh calls
# scorer-verdict-gate.py and `exit 2`s when the agent has a FRESH
# scorer verdict (<10 min) whose top pick is not the claimed id. A
# fake goal id can never BE the top pick, so whenever the probe agent
# has recently run the selector the wrapper dies before QUERY exists.
# The gate is correct; the probe was coupled to live state.
#
# Measured 2026-08-29 (bravo, cc-05), both directions, same wrapper:
#   fresh verdict, other top pick -> 0 QUERY lines in an 8,155-byte
#                                    trace, `scorer-sovereignty` deny,
#                                    GATE_RC=2, exit 2
#   stale verdict                 -> 3 QUERY lines, 33,188-byte trace
# So extract_query() now passes --verdict-file at a deliberately STALE
# fixture (below), taking the gate's documented fail-open branch. This
# does NOT loosen the gate for production — --verdict-file exists for
# exactly this ("explicit verdict path (tests)", scorer-verdict-gate.py)
# and is already accepted by aspirations-claim.sh.
#
# It also explains why this red was intermittent and un-chaseable: the
# coupling is to whether THE PROBE'S HARDCODED AGENT (zeta, below) ran
# the selector in the last 10 minutes on the reading box — not the
# agent running the suite. One box read 111/111 at 15:41 and 109/111 at
# 22:49 with no code change between, and a solo re-run 'fixed' it.
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
# A deliberately STALE scorer verdict, so scorer-verdict-gate.py takes its
# documented fail-open branch ("stale or unparseable -> fail-open") on every
# probe. Without this the gate's verdict — and therefore whether this file
# passes — depends on when the probe agent last ran the selector. Written once
# per run, removed on exit.
VERDICT_FIXTURE="$(mktemp -t claim-source-verdict.XXXXXX)"
trap 'rm -f "$VERDICT_FIXTURE"' EXIT
printf '{"top_goal_id":"g-fixture-not-a-real-goal","ts":"2000-01-01T00:00:00"}\n' \
  > "$VERDICT_FIXTURE"

extract_query() {
  local args=("$@")
  local trace query
  # bash -x writes to stderr; redirect 2>&1 so we can grep both streams.
  trace=$(MIND_AGENT=zeta bash -x "$WRAPPER" "${args[@]}" \
            --verdict-file "$VERDICT_FIXTURE" 2>&1 || true)
  # The wrapper sets QUERY="id=...&agent=..." on a single line. Match a `+`
  # trace line so we don't accidentally pick up the daemon's own response.
  # bash -x quotes the value with single-quotes if it contains shell-special
  # characters (& is special); strip the outer quotes for clean comparison.
  #
  # `|| true` on the grep is LOAD-BEARING, and its absence is the whole
  # reported symptom of : under `set -euo pipefail` a grep that
  # matches nothing fails the pipeline and kills this script instantly — no
  # PASS, no FAIL, no message, just a header line and rc=1. A test that can
  # die without saying anything is a detector that reports clean forever. Any
  # future short-circuit before QUERY assembly must now surface as a LOUD,
  # nameable failure instead.
  query=$(printf '%s\n' "$trace" | { grep -E '^\+ QUERY=' || true; } \
            | tail -1 | sed "s/^+ QUERY=//; s/^'//; s/'$//")
  if [[ -z "$query" ]]; then
    echo "  FAIL: wrapper produced NO QUERY line for args: ${args[*]}" >&2
    echo "        It short-circuited before QUERY assembly. Last 5 trace lines:" >&2
    printf '%s\n' "$trace" | tail -5 | sed 's/^/          /' >&2
    return 0   # emit empty on stdout; the caller's assertion reports the FAIL
  fi
  printf '%s\n' "$query"
}

# The wrapper appends `&sid=<MIND_SID>` when a session id is present — ADDITIVE,
# records-only ( slice 1, commit 19f05706a). Tests 1-4 were exact-equality
# against the pre-3176 shape and went RED the moment it landed; nobody updated them
# because this file was a main()-style .sh test referenced by NO aggregator (not
# run-full-suite.sh, and not run-invisible-suites.sh — that runner globbed test_*.py
# only), so no runner had ever reported it. Found 2026-07-29 only because a
# fresh-eyes pass on an unrelated change to the same wrapper ran it by hand: 4 of 6
# red. That gap is CLOSED (): run-invisible-suites.sh now globs test_*.sh
# and test-*.sh as well, and run-full-suite.sh invokes that runner, so a future red
# here is reported without anyone remembering this filename.
#
# Assert the base EXACTLY and allow ONLY a sid suffix. Do NOT relax to substring
# matching (the shape tests 5-6 use): `[[ $q == *agent=zeta* ]]` is satisfied by a
# query that ALSO carries a phantom `agent=world`, which is the precise defect this
# file exists to catch. Tolerating the new field must not cost the old assertion.
assert_query() {
  local q="$1" base="$2" label="$3"
  if [[ "$q" == "$base" ]] || [[ "$q" == "$base&sid="?* ]]; then
    pass "$label → '$q'"
  else
    fail "expected '$base' (optionally + '&sid=<id>'), got '$q'"
  fi
}

echo "Test 1: --source first then goal-id"
q=$(extract_query --source world g-fake-test-001)
assert_query "$q" "id=g-fake-test-001&agent=zeta" "--source world g-fake-test-001"

echo "Test 2: goal-id first then --source (alpha-incident shape)"
q=$(extract_query g-fake-test-002 --source world)
assert_query "$q" "id=g-fake-test-002&agent=zeta" "g-fake-test-002 --source world"

echo "Test 3: --source agent variant"
q=$(extract_query --source agent g-fake-test-003)
assert_query "$q" "id=g-fake-test-003&agent=zeta" "--source agent g-fake-test-003"

echo "Test 4: explicit positional agent overrides MIND_AGENT (no --source)"
q=$(extract_query g-fake-test-004 alpha)
assert_query "$q" "id=g-fake-test-004&agent=alpha" "g-fake-test-004 alpha"

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
