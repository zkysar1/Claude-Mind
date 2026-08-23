#!/usr/bin/env bash
# run-asp-257-suite.sh — runs the full defer-to-unblock auto-conversion test
# suite (asp-257 walk: /03/04/05). Reports per-suite pass/fail and
# aggregate counts. Exits non-zero if any suite fails.
#
# Six suites, 39 total cases:
#   1. test-capability-gate.sh                     — 14 cases (gate keyword matching)
#   2. test_capability_gate_narrative.py           —  3 cases (narrative-pattern detection)
#   3. test_capability_gate_suggest_unblock.py     —  4 cases (--suggest-unblock flag)
#   4. test_defer_gate_unblock_filing.py           —  5 cases (filing helper)
#   5. test_defer_gate_unblock_dedup.py            —  7 cases (dedup helper)
#   6. test_defer_to_unblock_integration.py        —  6 cases (end-to-end integration, )
#
# All Python tests use sys.executable (no `py -3` / `python3` shim hazards from
# Bash). The .sh suite uses python3 inside its heredoc (per rb-370/rb-471).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/../_paths.sh"
cd "$PROJECT_ROOT"

# INCIDENT rb-2983/guard-955 (2026-07-09): pin the storage backend to local so no
# suite here can PUT to the production own-cloud S3 store. Suite 6/6 runs the
# main()-style test_defer_to_unblock_integration.py DIRECTLY (not via pytest —
# pytest collects 0 from it, so a conftest autouse fixture never protects it);
# its update-goal subprocess does env=os.environ.copy(), so under a global
# STORAGE_BACKEND=own-cloud that copy PUT the asp-555 fixture to the production
# S3 key (MIND_WORLD redirects only the local path + _rel base, NOT the S3 key —
# customer_prefix+env_id are unchanged), truncating world/aspirations.jsonl from
# 22 aspirations to 1. Pinning local HERE makes the tests' "production state is
# never touched" contract true regardless of the caller's global backend.
export STORAGE_BACKEND=local

PASSES=0
FAILS=0
declare -a FAILED_SUITES

# : truncation is applied HERE, by outcome — never inside the command
# string. Every suite used to be piped through `| tail -N` at its call site, which
# on a PASS is a fine terse summary and on a FAIL is actively misleading: the
# named "FAIL: <case>" lines sit in the MIDDLE of the output, so the trailing
# window shows only the last few PASS lines plus the count. The operator learned
# that a suite failed and not which case — measured on suite 1/6, where
# "FAIL: c_user_trust_confirmation" was invisible behind `tail -3` while the
# aggregate line read "PASS: 13   FAIL: 1".
#
# Capturing instead of piping also removes the reliance on `set -o pipefail` for
# correct exit-code propagation (guard-696): the suite's own rc is read directly
# rather than a pipeline's last-non-zero.
run_suite() {
    local label="$1"
    local cmd="$2"
    local tail_n="${3:-6}"
    local out
    out="$(mktemp)"
    echo "── $label ──"
    if eval "$cmd" > "$out" 2>&1; then
        PASSES=$((PASSES + 1))
        tail -n "$tail_n" "$out" | sed 's/^/  /'
        echo "  → SUITE PASS"
    else
        FAILS=$((FAILS + 1))
        FAILED_SUITES+=("$label")
        echo "  ── failing cases ──"
        if ! grep -aE '^[[:space:]]*(FAIL|FAILED|ERROR|Traceback|AssertionError|E   )' "$out" | sed 's/^/  /'; then
            echo "  (no FAIL-prefixed lines — see tail below)"
        fi
        echo "  ── tail ──"
        tail -n "$tail_n" "$out" | sed 's/^/  /'
        echo "  → SUITE FAIL"
    fi
    rm -f "$out"
    echo
}

run_suite "1/6 capability-gate regression (14 cases)" \
    "bash $CORE_ROOT/scripts/test-capability-gate.sh" 3

run_suite "2/6 capability-gate narrative-pattern (3 cases)" \
    "python3 $CORE_ROOT/scripts/tests/test_capability_gate_narrative.py" 5

run_suite "3/6 capability-gate suggest-unblock (4 cases)" \
    "python3 $CORE_ROOT/scripts/tests/test_capability_gate_suggest_unblock.py" 6

run_suite "4/6 defer-gate Unblock filing (5 cases)" \
    "python3 $CORE_ROOT/scripts/tests/test_defer_gate_unblock_filing.py" 7

run_suite "5/6 defer-gate Unblock dedup (7 cases)" \
    "python3 $CORE_ROOT/scripts/tests/test_defer_gate_unblock_dedup.py" 9

run_suite "6/6 defer-to-unblock integration (6 cases)" \
    "python3 $CORE_ROOT/scripts/tests/test_defer_to_unblock_integration.py" 8

echo "════════════════════════════════════════"
TOTAL=$((PASSES + FAILS))
echo "asp-257 test suite: $PASSES/$TOTAL suites passed (39 total cases)"
if [ $FAILS -gt 0 ]; then
    echo "Failed suites:"
    for s in "${FAILED_SUITES[@]}"; do
        echo "  - $s"
    done
    exit 1
fi
echo "All 39 cases verified."
exit 0
