#!/usr/bin/env bash
# test-invisible-suites-agent-resolution.sh — pins the bound-agent resolution
# chain in run-invisible-suites.sh ().
#
# The defect being defended: an unbound run (no PreToolUse hook injection —
# backgrounded Bash, cron, CI, nested subshells) used to dispatch all suites
# with MIND_AGENT unset, fail 6 files env-shaped, and print them under the
# new-reds header — manufactured reds camouflaging genuine ones. The fix is a
# resolution chain (env → sole running-session-id → sole local-paths.conf) and
# a loud SKIP when unresolvable.
#
# Hermetic: every case drives `--resolve-only` (exits before enumeration) or
# the SKIP path (exits before dispatch) against scratch MIND_AGENTS_ROOT
# dirs, so no suite ever actually runs. This file itself is enumerated into
# the runner's shell half — it MUST stay cheap and non-recursive, which the
# exit-before-dispatch paths guarantee.

set -uo pipefail
export STORAGE_BACKEND=local  # guard-955 — belt-and-braces; no suite dispatches here

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUNNER="$SCRIPT_DIR/run-invisible-suites.sh"
[ -f "$RUNNER" ] || { echo "FAIL: runner not found at $RUNNER"; exit 1; }

SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

PASS=0
FAIL=0

check() {
  local label="$1" expected="$2" actual="$3"
  if [ "$actual" = "$expected" ]; then
    PASS=$((PASS + 1))
    echo "PASS: $label"
  else
    FAIL=$((FAIL + 1))
    echo "FAIL: $label"
    echo "  expected: $expected"
    echo "  actual:   $actual"
  fi
}

# ── 1. env tier wins: MIND_AGENT set → kept verbatim, resolution=env ────────
mkdir -p "$SCRATCH/case1"
out=$(env MIND_AGENT=testagent MIND_AGENTS_ROOT="$SCRATCH/case1" bash "$RUNNER" --resolve-only)
check "env tier wins" "agent=testagent resolution=env" "$out"

# ── 2. sole running-session-id → resident runner resolved ────────────────────
mkdir -p "$SCRATCH/case2/alpha/session"
touch "$SCRATCH/case2/alpha/session/running-session-id"
mkdir -p "$SCRATCH/case2/bravo"  # agent dir with NO rsid must not confuse the glob
out=$(env -u MIND_AGENT MIND_AGENTS_ROOT="$SCRATCH/case2" bash "$RUNNER" --resolve-only)
check "sole running-session-id" "agent=alpha resolution=running-session-id" "$out"

# ── 3. no rsid, sole conf → single-agent box resolved ────────────────────────
mkdir -p "$SCRATCH/case3/bravo"
touch "$SCRATCH/case3/bravo/local-paths.conf"
out=$(env -u MIND_AGENT MIND_AGENTS_ROOT="$SCRATCH/case3" bash "$RUNNER" --resolve-only)
check "sole local-paths.conf" "agent=bravo resolution=single-conf" "$out"

# ── 4. empty root → unresolved, no guess ─────────────────────────────────────
mkdir -p "$SCRATCH/case4"
out=$(env -u MIND_AGENT MIND_AGENTS_ROOT="$SCRATCH/case4" bash "$RUNNER" --resolve-only)
check "empty root unresolved" "agent= resolution=none" "$out"

# ── 5. TWO rsids, no conf → ambiguous, no guess ──────────────────────────────
mkdir -p "$SCRATCH/case5/alpha/session" "$SCRATCH/case5/bravo/session"
touch "$SCRATCH/case5/alpha/session/running-session-id" \
      "$SCRATCH/case5/bravo/session/running-session-id"
out=$(env -u MIND_AGENT MIND_AGENTS_ROOT="$SCRATCH/case5" bash "$RUNNER" --resolve-only)
check "ambiguous rsids unresolved" "agent= resolution=none" "$out"

# ── 6. ambiguous rsids + sole conf → conf tier still consulted ───────────────
# Two rsids can be a stale leftover from a cross-agent probe; the sole conf
# still names the box's RESIDENT agent, so the chain falls through to it.
mkdir -p "$SCRATCH/case6/alpha/session" "$SCRATCH/case6/bravo/session"
touch "$SCRATCH/case6/alpha/session/running-session-id" \
      "$SCRATCH/case6/bravo/session/running-session-id" \
      "$SCRATCH/case6/bravo/local-paths.conf"
out=$(env -u MIND_AGENT MIND_AGENTS_ROOT="$SCRATCH/case6" bash "$RUNNER" --resolve-only)
check "rsid-ambiguous falls to sole conf" "agent=bravo resolution=single-conf" "$out"

# ── 7. unresolvable FULL run → loud SKIP, exit 0, nothing dispatched ─────────
# This is the goal's VERIFY BY shape: "the invisible half either passes or
# SKIPS with a stated reason". Enumeration runs (cheap greps) but the skip
# fires before any dispatch, so this stays fast and non-recursive.
mkdir -p "$SCRATCH/case7"
full_out=$(env -u MIND_AGENT MIND_AGENTS_ROOT="$SCRATCH/case7" timeout 120 bash "$RUNNER" 2>&1)
full_rc=$?
if [ $full_rc -eq 0 ] \
   && grep -q "SKIPPED — no resolvable agent binding" <<<"$full_out" \
   && grep -q "Set MIND_AGENT=" <<<"$full_out" \
   && ! grep -q "new reds" <<<"$full_out" \
   && ! grep -qE '^(PASS|FAIL)\(?' <<<"$full_out"; then
  PASS=$((PASS + 1))
  echo "PASS: unresolvable full run skips loudly (rc=0, no dispatch)"
else
  FAIL=$((FAIL + 1))
  echo "FAIL: unresolvable full run (rc=$full_rc)"
  printf '%s\n' "$full_out" | tail -8 | sed 's/^/  | /'
fi

echo "────────────────────────────────────────"
echo "invisible-suites-agent-resolution: $PASS passed, $FAIL failed"
[ $FAIL -eq 0 ] || exit 1
exit 0
