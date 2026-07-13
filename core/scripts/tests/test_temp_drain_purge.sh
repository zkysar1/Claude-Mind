#!/usr/bin/env bash
# test_temp_drain_purge.sh — regression test for 6 (agent-hang fix).
# Unit-tests the assert_safe_temp_dir guard in temp-drain-purge.sh: hostile
# inputs (empty agent_dir/project_root/temp_dir, non-absolute path, /temp,
# outside-project-root, wrong basename) MUST be REFUSED (rc 1); only a real
# "$PROJECT_ROOT/.../temp" passes (rc 0). This guarantees the agent-hang class
# — an unguarded rm on a possibly-empty variable path triggering the Claude
# Code dangerous-rm dialog — cannot recur through this canonical purge helper.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="$SCRIPT_DIR/../temp-drain-purge.sh"

if [ ! -f "$HELPER" ]; then
  echo "FAIL: helper not found at $HELPER"; exit 1
fi

# Source the helper — main() does NOT run (guarded on BASH_SOURCE==0), so the
# guard function is callable in isolation with hostile inputs.
# shellcheck disable=SC1090
source "$HELPER"

PR="/opt/example/root"     # synthetic project root
AD="$PR/agents/alpha"      # synthetic agent dir
GOOD="$AD/temp"            # the one safe shape

fails=0
# check <description> <expected_rc> <temp_dir> <project_root> <agent_dir>
check() {
  local desc="$1" exp="$2" td="$3" pr="$4" ad="$5" rc
  assert_safe_temp_dir "$td" "$pr" "$ad" 2>/dev/null; rc=$?
  if [ "$rc" -eq "$exp" ]; then
    echo "  [PASS] $desc (rc=$rc)"
  else
    echo "  [FAIL] $desc — expected rc=$exp, got rc=$rc"
    fails=$((fails+1))
  fi
}

echo "assert_safe_temp_dir guard cases:"
check "empty agent_dir REFUSED"           1 "$GOOD" "$PR" ""
check "empty project_root REFUSED"        1 "$GOOD" ""    "$AD"
check "empty temp_dir REFUSED"            1 ""      "$PR"  "$AD"
check "non-absolute temp_dir REFUSED"     1 "relative/temp" "$PR" "$AD"
check "/temp (empty-AGENT_DIR shape) REFUSED" 1 "/temp" "$PR" "$AD"
check "outside-project-root REFUSED"      1 "/other/agents/alpha/temp" "$PR" "$AD"
check "wrong basename (scratch) REFUSED"  1 "$AD/scratch" "$PR" "$AD"
check "valid temp dir PASSES"             0 "$GOOD" "$PR" "$AD"

echo "executed dry-run smoke:"
out="$(bash "$HELPER" --dry-run 2>/dev/null)"; rc=$?
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q '"temp_dir"'; then
  echo "  [PASS] dry-run rc=0 + JSON with temp_dir"
else
  echo "  [FAIL] dry-run rc=$rc out=$out"; fails=$((fails+1))
fi

if [ "$fails" -gt 0 ]; then echo ""; echo "$fails failure(s)"; exit 1; fi
echo ""
echo "All temp-drain-purge guard cases verified."
exit 0
