#!/usr/bin/env bash
# mutation-proof-test.sh (gap-019, guard-1220, rb-4004) — prove a regression
# test actually catches its bug via the TWO-WAY mutation proof:
#   baseline GREEN -> apply sabotage -> RED -> restore (GUARANTEED) -> GREEN.
#
# The gap's key failure mode is "a missed restore silently ships sabotage
# code", so restore fires from an EXIT/INT/TERM trap AND is byte-verified
# against the backup; a restore mismatch is the highest-severity exit (3).
# Two other failure modes this catches: a VACUOUS test (passes even under
# sabotage — the  encounter) and a BROKEN test (fails on real code).
#
# Domain-free framework helper: --target / --test-cmd / --sabotage are generic
# parameters; the script hard-codes no product, service, or test-runner name.
set -uo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: mutation-proof-test.sh --target <file> --test-cmd '<cmd>'
         ( --sabotage-old '<str>' --sabotage-new '<str>' | --sabotage-sed '<sed-script>' )
         [--workdir <dir>] [--junit-xml <path>] [--skip-baseline]

Proves a regression test is not vacuous by mutating the code under test:
  1. baseline: run the exact test on real code            -> expect PASS (GREEN)
  2. sabotage: apply a targeted mutation to --target
  3. red:      re-run the test                            -> expect FAIL (RED)
  4. restore:  put --target back (GUARANTEED via trap)    -> byte-verified
  5. green:    re-run the test                            -> expect PASS (GREEN)

--test-cmd should target the SPECIFIC test (not the whole suite): it runs 3x.
--sabotage-old replaces ALL occurrences of a distinctive string; pick one that
  reverts the fix / breaks the mechanism the test guards.
--junit-xml <path>: after the RED step, assert the result XML shows tests>0
  (a build can report SUCCESS while the test was never executed — guard-1220).

Exit: 0 PASS (test is mutation-proof) | 1 FAIL (vacuous/broken/no-op mutation)
      2 usage/operational error       | 3 RESTORE FAILED (manual recovery needed)
Emits a single-line JSON verdict on stdout.
EOF
}

TARGET=""; TEST_CMD=""; SAB_OLD=""; SAB_NEW=""; SAB_SED=""
WORKDIR="."; JUNIT=""; SKIP_BASELINE=0
have_old=0; have_sed=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --target) TARGET="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
    --test-cmd) TEST_CMD="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
    --sabotage-old) SAB_OLD="${2-}"; have_old=1; shift $(( $# >= 2 ? 2 : 1 ));;
    --sabotage-new) SAB_NEW="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
    --sabotage-sed) SAB_SED="${2-}"; have_sed=1; shift $(( $# >= 2 ? 2 : 1 ));;
    --workdir) WORKDIR="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
    --junit-xml) JUNIT="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
    --skip-baseline) SKIP_BASELINE=1; shift;;
    -h|--help) usage; exit 2;;
    *) echo "ERROR: unknown arg: $1" >&2; usage; exit 2;;
  esac
done

[[ -n "$TARGET" && -n "$TEST_CMD" ]] || { echo "ERROR: --target and --test-cmd are required" >&2; usage; exit 2; }
[[ -f "$TARGET" ]] || { echo "ERROR: target file not found: $TARGET" >&2; exit 2; }
[[ -d "$WORKDIR" ]] || { echo "ERROR: workdir not found: $WORKDIR" >&2; exit 2; }
if [[ $((have_old + have_sed)) -ne 1 ]]; then
  echo "ERROR: provide EXACTLY ONE sabotage form (--sabotage-old/--sabotage-new OR --sabotage-sed)" >&2; exit 2
fi
[[ $have_old -eq 1 && -z "$SAB_OLD" ]] && { echo "ERROR: --sabotage-old must be non-empty" >&2; exit 2; }

if command -v python3 >/dev/null 2>&1; then PY="python3"; else PY="py -3"; fi

BACKUP="${TARGET}.mutation-backup.$$"
cp -p "$TARGET" "$BACKUP" || { echo "ERROR: could not back up target" >&2; exit 2; }

RESTORE_STATUS="pending"
restore() {
  # Guaranteed, idempotent, byte-verified restore. Fires from the trap on ANY
  # exit path (normal, error, INT, TERM) so sabotage can never survive the run.
  [[ -f "$BACKUP" ]] || return 0
  cp -p "$BACKUP" "$TARGET" 2>/dev/null || true
  if cmp -s "$BACKUP" "$TARGET"; then
    rm -f "$BACKUP"; RESTORE_STATUS="ok"
  else
    RESTORE_STATUS="FAILED"
    echo "CRITICAL: restore verification FAILED — '$TARGET' does not match backup '$BACKUP'. SABOTAGE MAY BE LIVE. Manually restore from the backup file." >&2
  fi
}
trap restore EXIT INT TERM

run_test() { ( cd "$WORKDIR" && bash -c "$TEST_CMD" ) >/dev/null 2>&1; }

emit() {
  # $1 verdict, $2 reason. Single-line JSON on stdout.
  TARGET="$TARGET" VERDICT="$1" REASON="$2" BG="$BASELINE_GREEN" \
  SA="$SAB_APPLIED" SR="$SAB_RED" RG="$RESTORE_GREEN" TR="$TEST_RAN" RS="$RESTORE_STATUS" \
  $PY -c '
import os, json
print(json.dumps({
  "verdict": os.environ["VERDICT"], "reason": os.environ["REASON"],
  "target": os.environ["TARGET"], "baseline_green": os.environ["BG"],
  "sabotage_applied": os.environ["SA"], "sabotage_red": os.environ["SR"],
  "restore_green": os.environ["RG"], "test_ran": os.environ["TR"],
  "restore_status": os.environ["RS"],
}))'
}

BASELINE_GREEN="skipped"; SAB_APPLIED="false"; SAB_RED="n/a"; RESTORE_GREEN="n/a"; TEST_RAN="unchecked"

# --- Step 1: baseline (real code must be GREEN) ---
if [[ $SKIP_BASELINE -eq 0 ]]; then
  if run_test; then BASELINE_GREEN="true"; else
    BASELINE_GREEN="false"
    emit "FAIL" "baseline RED: the test fails on unmodified (fixed) code — it is broken, not a valid regression guard"
    exit 1
  fi
fi

# --- Step 2: apply sabotage, verify the file actually changed ---
if [[ $have_old -eq 1 ]]; then
  if ! TARGET="$TARGET" SAB_OLD="$SAB_OLD" SAB_NEW="$SAB_NEW" $PY -c '
import os, sys
t = os.environ["TARGET"]; old = os.environ["SAB_OLD"]; new = os.environ["SAB_NEW"]
s = open(t, encoding="utf-8").read()
if s.count(old) == 0:
    sys.exit(7)
open(t, "w", encoding="utf-8").write(s.replace(old, new))
'; then
    emit "FAIL" "sabotage-old string not found in target — no mutation applied, nothing proven"
    exit 1
  fi
else
  sed -i "$SAB_SED" "$TARGET" || { emit "FAIL" "sabotage-sed command failed"; exit 1; }
fi
if cmp -s "$BACKUP" "$TARGET"; then
  emit "FAIL" "sabotage produced NO change (no-op mutation) — a passing test would be a false proof"
  exit 1
fi
SAB_APPLIED="true"

# --- Step 3: sabotaged code must be RED ---
if run_test; then
  SAB_RED="false"
  restore; trap - EXIT INT TERM   # restore now so restore_status is accurate in the emitted JSON
  emit "FAIL" "VACUOUS TEST: it PASSES against sabotaged code — it does not actually catch the regression it guards (gap-019)"
  exit 1
fi
SAB_RED="true"

# --- Step 3b: optional — confirm the test ACTUALLY RAN (guard-1220) ---
if [[ -n "$JUNIT" ]]; then
  if TARGET="$JUNIT" $PY -c '
import os, sys, glob, re
paths = glob.glob(os.environ["TARGET"]) or [os.environ["TARGET"]]
total = 0
for p in paths:
    try:
        txt = open(p, encoding="utf-8", errors="replace").read()
    except OSError:
        continue
    for m in re.finditer(r"tests=\"(\d+)\"", txt):
        total += int(m.group(1))
sys.exit(0 if total > 0 else 8)
' 2>/dev/null; then TEST_RAN="true"; else TEST_RAN="false"; fi
  if [[ "$TEST_RAN" == "false" ]]; then
    restore; trap - EXIT INT TERM   # restore now so restore_status is accurate in the emitted JSON
    emit "FAIL" "result XML shows tests=0 (junit-xml) — the test was never executed despite the run; RED may be a build/compile artifact, not the assertion firing (guard-1220)"
    exit 1
  fi
fi

# --- Step 4: restore (explicit; trap is the backstop) ---
restore
trap - EXIT INT TERM
if [[ "$RESTORE_STATUS" == "FAILED" ]]; then
  emit "RESTORE_FAILED" "restore did not reproduce the backup byte-for-byte — sabotage may be live in $TARGET; recover manually"
  exit 3
fi

# --- Step 5: restored code must be GREEN again ---
if run_test; then RESTORE_GREEN="true"; else
  RESTORE_GREEN="false"
  emit "FAIL" "post-restore RED: the test fails after restore even though the file matched the backup — the test is flaky or has external state; investigate before trusting it"
  exit 1
fi

emit "PASS" "mutation-proof: GREEN on real code -> RED under sabotage -> GREEN after restore; the test catches its regression and restore left no sabotage behind"
exit 0
