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
  A RELATIVE path resolves against --workdir (where the suite actually runs),
  NOT against the cwd this script was invoked from. Globs are allowed.
  ALSO populates `red_tests` in the verdict: which testcases went red under
  sabotage, each with its type (failure=assertion / error=exception) and
  message. A RED run alone does not say WHICH assertion fired — a precondition
  blowing up and the behavioural detector firing look identical from the exit
  status. Without --junit-xml, `red_tests` is null (NOT measured), never [].
  `type` is the REPORTING TOOL'S classification, not a property of the test:
  Gradle separates failure from error, while pytest files an uncaught
  exception as a failure too (verified against real output from both). Read
  `type` as a hint and the message as the evidence; do not infer "an
  assertion fired rather than something threw" from `type` alone.
  CAVEAT — the XML is not checked for FRESHNESS. Nothing here proves it was
  written by THIS run, so when the test command never executes (a compile
  break leaves the previous run's report in place) `red_tests` reports THAT
  run's failures as if they were this one's. Weigh it accordingly: a named,
  specific list reads as more trustworthy than the tests>0 boolean it sits
  beside, and here it is exactly as stale. Tracked in g-115-3499, which owns
  the freshness fix; this field inherits the hazard rather than adding it.

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
[[ -d "$WORKDIR" ]] || { echo "ERROR: workdir not found: $WORKDIR" >&2; exit 2; }
# --test-cmd runs inside --workdir, so the repo-relative form that matches the test command
# is the natural thing to type for --target too — but it used to resolve against the CALLER's
# cwd. That half failed LOUDLY ("target file not found") and was self-correcting, unlike the
# --junit-xml half below, so it was never the dangerous one; leaving the two flags on
# different bases is nonetheless a trap of its own. Strictly ADDITIVE: only redirect when the
# path does NOT resolve as given, so every invocation that works today keeps working.
if [[ "$TARGET" != /* && ! -f "$TARGET" && -f "$WORKDIR/$TARGET" ]]; then
  TARGET="$WORKDIR/$TARGET"
fi
[[ -f "$TARGET" ]] || { echo "ERROR: target file not found: $TARGET" >&2; exit 2; }
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
  # Every field is a STRING except red_tests, which is raw JSON parsed below —
  # callers get a real list, not a string that has to be double-decoded.
  TARGET="$TARGET" VERDICT="$1" REASON="$2" BG="$BASELINE_GREEN" \
  SA="$SAB_APPLIED" SR="$SAB_RED" RG="$RESTORE_GREEN" TR="$TEST_RAN" RS="$RESTORE_STATUS" \
  RT="${RED_TESTS:-null}" RC="${RED_COUNT:-null}" RPE="${RED_PARSE_ERRORS:-null}" \
  $PY -c '
import os, json
try:
    red = json.loads(os.environ.get("RT") or "null")
except (ValueError, TypeError):
    red = None          # unparseable is NOT the same as none-found — stay null
try:
    red_count = json.loads(os.environ.get("RC") or "null")
except (ValueError, TypeError):
    red_count = None
try:
    parse_errors = json.loads(os.environ.get("RPE") or "null")
except (ValueError, TypeError):
    parse_errors = None
print(json.dumps({
  "verdict": os.environ["VERDICT"], "reason": os.environ["REASON"],
  "target": os.environ["TARGET"], "baseline_green": os.environ["BG"],
  "sabotage_applied": os.environ["SA"], "sabotage_red": os.environ["SR"],
  "restore_green": os.environ["RG"], "test_ran": os.environ["TR"],
  "restore_status": os.environ["RS"],
  # null => not measured (no --junit-xml, or the parse failed).
  # []   => measured, and the RED run reported no failing testcase at all —
  #         a real signal, not an absence: the run went red WITHOUT any test
  #         failing (compile/build artifact), which is exactly what guard-1220
  #         and guard-1631 warn a bare RED cannot distinguish.
  "red_tests": red, "red_count": red_count, "red_parse_errors": parse_errors,
}))'
}

BASELINE_GREEN="skipped"; SAB_APPLIED="false"; SAB_RED="n/a"; RESTORE_GREEN="n/a"; TEST_RAN="unchecked"
# null (the JSON literal), not [] — see the emit() comment. Absent-vs-empty is
# load-bearing here: [] would claim a measurement that never happened.
RED_TESTS="null"; RED_COUNT="null"; RED_PARSE_ERRORS="null"
RED_TESTS_CAP=20

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
  # Resolve a RELATIVE --junit-xml against --workdir, not against the script's cwd.
  # run_test() executes the suite in a `cd "$WORKDIR"` subshell, so the result XML lands
  # under WORKDIR — but this check used to glob from wherever the script was invoked. With
  # --workdir set (the normal shape for a product repo) a relative path therefore matched
  # nothing, total stayed 0, and a SOUND proof was reported as
  # FAIL "tests=0 ... the test was never executed". That false-FAIL is the inverse of the
  # false-PASS family this flag exists to catch, and it is worse than a missing check:
  # it tells the operator their regression test is vacuous when it demonstrably is not,
  # which pushes them back to hand-rolling the proof (gap-019). Measured 2026-07-27
  # (): same proof, same repo, same sabotage — relative path FAIL tests=0 vs
  # absolute path PASS, while the RED-run XML read tests=6 failures=1 with the target
  # assertion firing verbatim.
  # The `tests="N"` regex total below is UNCHANGED and remains the sole driver of
  # test_ran (guard-1220). The red-test extraction is a SEPARATE, strictly additive
  # ElementTree pass on the same files: if it throws on a malformed file it skips
  # that file and red_tests is short — it can never flip test_ran, so a parse bug
  # here cannot manufacture the false-FAIL this flag was fixed for in .
  if JUNIT_JSON="$(TARGET="$JUNIT" WD="$WORKDIR" CAP="$RED_TESTS_CAP" $PY -c '
import os, sys, glob, re, json
import xml.etree.ElementTree as ET
_t = os.environ["TARGET"]
# Try the path AS GIVEN first (preserves every invocation that already works), then the
# --workdir-joined form. Strictly additive: a relative path that resolved before still
# resolves; one that silently matched nothing now finds the XML where the suite wrote it.
_cands = [_t] if os.path.isabs(_t) else [_t, os.path.join(os.environ.get("WD") or ".", _t)]
paths = []
for _c in _cands:
    paths = glob.glob(_c) or ([_c] if os.path.exists(_c) else [])
    if paths:
        break
total = 0
red = []
parse_errors = 0
for p in paths:
    try:
        txt = open(p, encoding="utf-8", errors="replace").read()
    except OSError:
        continue
    for m in re.finditer(r"tests=\"(\d+)\"", txt):
        total += int(m.group(1))
    # --- additive: which testcases went red, and why ---
    try:
        root = ET.fromstring(txt)
    except ET.ParseError:
        # Counted, never swallowed. An unparseable file must not silently
        # become "no tests failed" — that is the same absent-vs-empty
        # conflation this whole field exists to eliminate, and skipping
        # quietly here would commit it. Surfaced as red_parse_errors, and
        # below it downgrades an otherwise-empty red set back to null.
        parse_errors += 1
        continue
    # A file may be a single <testsuite> or a <testsuites> wrapper; iterate
    # testcases wherever they live rather than assuming the shape.
    for tc in root.iter("testcase"):
        for kind in ("failure", "error"):
            node = tc.find(kind)
            if node is None:
                continue
            cls = tc.get("classname") or ""
            nm = tc.get("name") or ""
            msg = (node.get("message") or (node.text or "")).strip().replace("\n", " ")
            red.append({
                "name": (cls + "." + nm) if cls and nm else (nm or cls),
                # failure = an assertion fired; error = the mutant threw/crashed.
                # guard-1631: a crash proves nothing about the assertion having teeth.
                "type": kind,
                "message": msg[:300],
            })
            break   # one entry per testcase, even if it somehow carries both
cap = int(os.environ.get("CAP") or 20)
# Emit the TRUE count alongside a capped list so truncation is visible rather
# than silently reading as "that was all of them" (guard-1715).
# An EMPTY red set with parse errors is NOT "nothing failed" — it is "could not
# tell", so it degrades to null (not measured). A NON-empty red set is kept even
# with parse errors: it is genuinely partial, and red_parse_errors says so.
out = {"red": red[:cap], "red_count": len(red), "parse_errors": parse_errors}
if not red and parse_errors:
    out["red"] = None
    out["red_count"] = None
print(json.dumps(out))
sys.exit(0 if total > 0 else 8)
' 2>/dev/null)"; then TEST_RAN="true"; else TEST_RAN="false"; fi
  # Populate red_tests from whatever the parse produced — INDEPENDENT of test_ran,
  # because a tests=0 run whose XML still names failing cases is exactly the
  # diagnostic an operator needs to see.
  if [[ -n "${JUNIT_JSON:-}" ]]; then
    RED_TESTS="$(RTJ="$JUNIT_JSON" $PY -c 'import os,json;print(json.dumps(json.loads(os.environ["RTJ"])["red"]))' 2>/dev/null || echo "null")"
    RED_COUNT="$(RTJ="$JUNIT_JSON" $PY -c 'import os,json;print(json.dumps(json.loads(os.environ["RTJ"])["red_count"]))' 2>/dev/null || echo "null")"
    RED_PARSE_ERRORS="$(RTJ="$JUNIT_JSON" $PY -c 'import os,json;print(json.dumps(json.loads(os.environ["RTJ"])["parse_errors"]))' 2>/dev/null || echo "null")"
  fi
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
