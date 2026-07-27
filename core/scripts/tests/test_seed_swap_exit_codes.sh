#!/usr/bin/env bash
# test_seed_swap_exit_codes.sh —  regression.
#
# Guards seed-transplant.sh's swap rc-check — the block that maps a non-zero
# `_seed_engine.py swap` exit to shell exit 6 vs 8. Before  the
# rc-check blindly `exit 8`'d on ANY non-zero engine rc, so the per-file-failure
# exit-6 branch was UNREACHABLE dead code and a genuine PARTIAL swap printed the
# misleading "moves may have FULLY COMPLETED — do NOT assume corruption"
# diagnostic. The fix parses the engine's stdout: a structured result with
# failures[] -> exit 6 (partial swap, moves did NOT all complete); empty or
# unparseable stdout -> exit 8 (engine crashed BEFORE printing a result).
#
# test_seed_swap_fail_loud.py (the  engine test) explicitly leaves the
# shell-level exit-6/exit-8 distinction "[not exercised here — bash-level]" in
# its docstring. This closes that gap.
#
# Two layers:
#   1. BEHAVIORAL — drive the REAL engine in both scenarios and assert the
#      stdout/rc CONTRACT the shell fix depends on (per-file-fail -> rc!=0 +
#      valid JSON with failures>0; no-staging crash -> rc!=0 + no parseable
#      result). If the engine ever stops printing JSON-before-exit, the shell
#      fix silently breaks — Layer 1 catches that at the source.
#   2. STRUCTURAL — assert seed-transplant.sh's rc-check parses stdout
#      (SWAP_NFAIL), keeps `exit 6` REACHABLE after the parse, and no longer
#      carries the old dead N_FAIL-based exit-6 block.
#
# Run: bash core/scripts/tests/test_seed_swap_exit_codes.sh
# Exit 0 = all pass, exit 1 = any case fails.

set -uo pipefail
export STORAGE_BACKEND=local   # engine swap is filesystem-only; local is correct + guard-955 safe

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CORE_SCRIPTS="$(cd "$SCRIPT_DIR/.." && pwd)"
ENGINE="$CORE_SCRIPTS/_seed_engine.py"
WRAPPER="$CORE_SCRIPTS/seed-transplant.sh"

FAILS=0
pass() { echo "  PASS: $1"; }
fail() { echo "  FAIL: $1" >&2; FAILS=$((FAILS+1)); }

for f in "$ENGINE" "$WRAPPER"; do
    [ -f "$f" ] || { echo "FATAL: missing $f" >&2; exit 1; }
done

# The exact stdout-parse the fixed rc-check runs (kept in lockstep with
# seed-transplant.sh's SWAP_NFAIL block). -1 = no parseable structured result.
nfail_of() {
    echo "$1" | py -3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(len(d.get('failures', [])) if isinstance(d, dict) else -1)
except Exception:
    print(-1)" 2>/dev/null
}

echo "== Layer 1: engine stdout/rc contract =="

# Case A: per-file failure — the dst target is a NON-EMPTY DIR, so os.replace
# fails even as root (root bypasses perms;  controlled-repro trick).
TA=$(mktemp -d)
mkdir -p "$TA/.seed-staging/sub"
echo "new" > "$TA/.seed-staging/sub/foo.txt"
mkdir -p "$TA/sub/foo.txt"; echo "blk" > "$TA/sub/foo.txt/blk"
A_JSON="$(py -3 "$ENGINE" swap --dest "$TA" 2>/dev/null)"; A_RC=$?
A_NFAIL="$(nfail_of "$A_JSON")"
[ "$A_RC" -ne 0 ] && pass "per-file-fail: engine rc!=0 (rc=$A_RC)" || fail "per-file-fail: expected rc!=0, got $A_RC"
[ "${A_NFAIL:--1}" -gt 0 ] && pass "per-file-fail: stdout parses to failures>0 ($A_NFAIL) -> shell exit 6" || fail "per-file-fail: expected failures>0, got '$A_NFAIL'"
rm -rf "$TA"

# Case B: no staging dir — the engine raises SystemExit BEFORE printing (do_swap
# "No staging dir to swap"). stdout empty/unparseable -> shell exit 8 (crash).
TB=$(mktemp -d)   # deliberately no .seed-staging
B_JSON="$(py -3 "$ENGINE" swap --dest "$TB" 2>/dev/null)"; B_RC=$?
B_NFAIL="$(nfail_of "$B_JSON")"
[ "$B_RC" -ne 0 ] && pass "no-staging: engine rc!=0 (rc=$B_RC)" || fail "no-staging: expected rc!=0, got $B_RC"
[ "${B_NFAIL:--1}" -eq -1 ] && pass "no-staging: stdout unparseable (nfail=$B_NFAIL) -> shell exit 8" || fail "no-staging: expected unparseable(-1), got '$B_NFAIL'"
rm -rf "$TB"

echo "== Layer 2: seed-transplant.sh rc-check wiring =="

grep -q 'SWAP_NFAIL=' "$WRAPPER" \
    && pass "rc-check parses stdout (SWAP_NFAIL present)" \
    || fail "SWAP_NFAIL parse missing — rc-check may have regressed to blanket exit 8"

# Match the `exit 6` STATEMENT (leading whitespace only), NOT a comment that
# mentions "(exit 6)" — anchoring on ^\s*exit avoids the false first-match.
NFAIL_LINE="$(grep -n 'SWAP_NFAIL=' "$WRAPPER" | head -1 | cut -d: -f1)"
EXIT6_LINE="$(grep -nE '^[[:space:]]*exit 6' "$WRAPPER" | head -1 | cut -d: -f1)"
if [ -n "$NFAIL_LINE" ] && [ -n "$EXIT6_LINE" ] && [ "$EXIT6_LINE" -gt "$NFAIL_LINE" ]; then
    pass "exit 6 reachable AFTER the SWAP_NFAIL parse (parse L$NFAIL_LINE, exit6 L$EXIT6_LINE)"
else
    fail "exit 6 not reachable after the SWAP_NFAIL parse (parse L${NFAIL_LINE:-none}, exit6 L${EXIT6_LINE:-none})"
fi

# The old dead N_FAIL-based exit-6 block (rc==0 path) must be gone — its presence
# is the  dead-code regression.
grep -q 'N_FAIL=' "$WRAPPER" \
    && fail "dead N_FAIL exit-6 block still present (g-115-2751 regression)" \
    || pass "dead N_FAIL exit-6 block removed"

echo ""
if [ "$FAILS" -eq 0 ]; then
    echo "ALL PASS (test_seed_swap_exit_codes.sh)"; exit 0
else
    echo "$FAILS FAILURE(S) (test_seed_swap_exit_codes.sh)" >&2; exit 1
fi
