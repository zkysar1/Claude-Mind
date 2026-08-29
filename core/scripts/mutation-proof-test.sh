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
  The verdict's `attribution` field names WHICH of those it is —
  measured | unmeasured (flag not given) | parse-failed — so a null is never
  read as a clean attribution, and a PASS carries a CAVEAT saying so.
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

HOW BROAD WAS THE SABOTAGE — `sabotage_sites` + `sabotage_sites_basis`.
  --sabotage-old replaces ALL occurrences. When the token appears more than
  once, the mutation lands at the site under test AND everywhere else, so a
  predicate anchored to that site and one that merely greps the whole file BOTH
  go red — and a PASS cannot tell them apart. Measured: a deliberately vacuous
  whole-file predicate was certified PASS by exactly this. The verdict stays
  PASS (the proof is real, just for a smaller proposition) and the reason
  carries a CAVEAT whenever sites > 1. basis says WHAT was counted:
  "occurrences" (--sabotage-old) | "changed-lines" (--sabotage-sed, the only
  mode-independent measure) | "unmeasured" (sabotage never applied; sites null).
  To prove ANCHORING, narrow to one site: --sabotage-sed '0,/re/s//new/'.
  Mirror of guard-1629, which covers sabotage landing at the WRONG site.

--target MUST NOT be this script. bash re-reads a running script by byte offset,
  so self-mutation corrupts the running instance; the damage is length-dependent
  (small edits look fine, large ones syntax-error), so it is refused outright.
  Mutating any OTHER script live in the current process tree has the same hazard
  and cannot be detected from here — drive those from a separate harness.

For an N-mutation matrix over a SET of cases (partition proof, per-mutant
negative controls, unproven-case reporting), see the companion
core/scripts/mutation-partition-proof.sh, which delegates each mutant here.

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
# REFUSE self-mutation. bash reads a script incrementally BY BYTE OFFSET, so
# editing this file while it executes makes the running instance resume at a
# stale offset. The damage is LENGTH-DEPENDENT, which is what makes it worth a
# hard refusal rather than a caveat: a +-2-byte replacement lands inside the
# same token and appears to work, while a 24-byte deletion resumes mid-statement
# and dies with a syntax error pointing at an unrelated line. Measured
# 2026-07-31 () doing exactly this: 3 of 4 self-mutants returned a
# clean verdict and the 4th exited 2 with no JSON at all. A silent-then-erratic
# failure inside an N-mutation matrix is precisely the "matrix that corrupts the
# tree is worse than no matrix" hazard. This catches the SELF case, which is the
# knowable one; mutating any OTHER script that is live in the current process
# tree has the same hazard and cannot be detected from here.
_abs() { readlink -f "$1" 2>/dev/null || echo "$1"; }
if [[ "$(_abs "$TARGET")" == "$(_abs "${BASH_SOURCE[0]}")" ]]; then
  echo "ERROR: --target is this script itself. bash re-reads a running script by byte offset, so self-mutation corrupts the running instance (length-dependent: small edits look fine, large ones syntax-error). Copy the script to a scratch path and mutate the copy, or drive the proof from a separate harness." >&2
  exit 2
fi
if [[ $((have_old + have_sed)) -ne 1 ]]; then
  echo "ERROR: provide EXACTLY ONE sabotage form (--sabotage-old/--sabotage-new OR --sabotage-sed)" >&2; exit 2
fi
[[ $have_old -eq 1 && -z "$SAB_OLD" ]] && { echo "ERROR: --sabotage-old must be non-empty" >&2; exit 2; }

if command -v python3 >/dev/null 2>&1; then PY="python3"; else PY="py -3"; fi

# : RESIDUE PRE-CHECK, and it is the root-cause fix rather than the
# detection one. `cmp` compares the target to the BACKUP, so it can only prove
# "the file is what it was when this run started" — never "the file is clean".
# When the target ALREADY contains the injected string at entry, the backup
# captures that residue, restore faithfully writes it back, cmp matches, and the
# run emits verdict PASS whose reason literally reads "restore left no sabotage
# behind" while the sabotage is live. MEASURED 2026-08-16 (alpha, hostname cc-08,
# uname -r 6.8.0-137-generic): deterministic, one run, on a two-site fixture with
# one site pre-sabotaged. That is STRICTLY WORSE than the  incident it
# was filed from, which at least returned FAIL.
#
# The property that makes this worth a hard refuse and not a warning: residue is
# SELF-PERPETUATING. Once any run leaves the string behind — an interrupted run,
# a concurrent run, a genuine restore failure — every subsequent run adopts it as
# baseline and certifies it clean, forever, with the evidence deleted each time.
# Refusing at entry breaks that chain at the only point where the file is still
# known-good.
#
# Only the --sabotage-old/--sabotage-new form can be checked: the --sabotage-sed
# form injects an expression whose output text is not knowable here. That case is
# reported as `residue_check: unavailable` rather than silently passing, because
# an unchecked lane that renders identically to a checked one is how this defect
# stayed invisible (guard-1760).
RESIDUE_CHECK="unavailable"
if [[ -n "${SAB_NEW:-}" ]]; then
  RESIDUE_CHECK="clean"
  if grep -qF -- "$SAB_NEW" "$TARGET" 2>/dev/null; then
    echo "ERROR: --target already contains the --sabotage-new string before this run started, so a backup taken now would capture RESIDUE and every verdict below would be about the wrong baseline. This is what a previous interrupted/concurrent run leaves behind; it is self-perpetuating and each run deletes the evidence. Remove the residue from '$TARGET' and re-run. Offending string: $SAB_NEW" >&2
    exit 2
  fi
fi

# : THE BACKUP LIVES OUTSIDE THE TARGET'S WORKING TREE.
# It used to be written as a SIBLING of the target ("${TARGET}.mutation-backup.$$"),
# which put it inside whatever repo the target belongs to. Combined with the
#  retention decision below — the backup deliberately SURVIVES every
# non-PASS exit — that made stray copies of production source accumulate in
# product working trees: measured 2026-08-21, five files after a 6-mutation
# partition run, seventeen after an 8-mutation Java run. The next canonical step
# for product work is product-pr-flow.sh, whose staging step is `git add -A`, so
# those copies would be committed into a PR and merged.
#
# ADJACENCY WAS NEVER THE POINT — recoverability was, and the two are separable.
# Retention semantics are UNCHANGED (see restore() and the PASS path); only the
# location moves, which removes the staging hazard at its root rather than asking
# every product repo to gitignore a framework-owned filename. The absolute path is
# interpolated into every CRITICAL/retention message, so a human recovering from a
# red run is told exactly where to look.
#
# The one property given up is tmp reaping (typically ~10 days). The backup exists
# so a reader can recover immediately after a red verdict; a recovery that has not
# happened in ten days is not happening, and until this change the file's real
# lifetime was "forever, inside your repo", which is the defect.
BACKUP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/mutation-proof-backup-XXXXXX")" \
  || { echo "ERROR: could not create a backup directory under ${TMPDIR:-/tmp}" >&2; exit 2; }
BACKUP="$BACKUP_DIR/$(basename "$TARGET").mutation-backup.$$"
cp -p "$TARGET" "$BACKUP" || { echo "ERROR: could not back up target" >&2; exit 2; }

RESTORE_STATUS="pending"
restore() {
  # Guaranteed, idempotent, byte-verified restore. Fires from the trap on ANY
  # exit path (normal, error, INT, TERM) so sabotage can never survive the run.
  [[ -f "$BACKUP" ]] || return 0
  cp -p "$BACKUP" "$TARGET" 2>/dev/null || true
  if cmp -s "$BACKUP" "$TARGET"; then
    # : cmp proves target==backup, NOT target-is-clean. Re-check the
    # injected string directly so a matching-but-dirty pair cannot report "ok".
    # With the pre-check above this should be unreachable; it is kept because the
    # two guards fail independently (the pre-check cannot see residue introduced
    # DURING the run by a concurrent instance, which is measured-real: two
    # overlapping runs on one target produced a false `sabotage_red:false`
    # "vacuous test" verdict and a false baseline-red "broken test" verdict in
    # the same pair).
    if [[ "$RESIDUE_CHECK" != "unavailable" ]] && grep -qF -- "$SAB_NEW" "$TARGET" 2>/dev/null; then
      RESIDUE_CHECK="RESIDUE"
      RESTORE_STATUS="FAILED"
      echo "CRITICAL: restore wrote a backup that ITSELF contains the sabotage string — '$TARGET' matches its backup but is NOT clean. SABOTAGE IS LIVE. The backup at '$BACKUP' is retained deliberately; it is the residue, not the recovery." >&2
      return 0
    fi
    RESTORE_STATUS="ok"
  else
    RESTORE_STATUS="FAILED"
    echo "CRITICAL: restore verification FAILED — '$TARGET' does not match backup '$BACKUP'. SABOTAGE MAY BE LIVE. Manually restore from the backup file." >&2
  fi
  #  outcome 3: the backup is NO LONGER deleted here. It used to be
  # rm-ed the instant cmp matched — i.e. before the post-restore test had run —
  # so the one artifact that could prove what happened was destroyed at exactly
  # the moment the run was about to go red. It is now removed only on the
  # success path (just before the PASS emit), which means every non-PASS exit,
  # including a trap-driven one, leaves the residue recoverable.
}
trap restore EXIT INT TERM

# stdin is redirected from /dev/null so the test command can never consume the
# CALLER's stdin (). mutation-partition-proof.sh drives its loop with
# `while read ... done <<< "$PLAN_TSV"`, so inside the loop body fd 0 IS the
# remaining plan rows; a test_cmd that reads stdin ate them, the loop's next
# `read` found nothing, and the run ended early reporting verdict PASS over a
# TRUNCATED matrix (measured: a 2-mutation plan reported mutations:1 cases:1
# cases_unproven:[] and exit 0). That is the k/N-tally-conceals-unproven-cases
# failure this tool exists to detect, occurring inside the tool itself.
# Strictly additive: it cannot change any invocation that does not read stdin.
run_test() { ( cd "$WORKDIR" && bash -c "$TEST_CMD" ) </dev/null >/dev/null 2>&1; }

emit() {
  # $1 verdict, $2 reason. Single-line JSON on stdout.
  # Every field is a STRING except red_tests, which is raw JSON parsed below —
  # callers get a real list, not a string that has to be double-decoded.
  TARGET="$TARGET" VERDICT="$1" REASON="$2" BG="$BASELINE_GREEN" \
  SA="$SAB_APPLIED" SR="$SAB_RED" RG="$RESTORE_GREEN" TR="$TEST_RAN" RS="$RESTORE_STATUS" \
  RT="${RED_TESTS:-null}" RC="${RED_COUNT:-null}" RPE="${RED_PARSE_ERRORS:-null}" \
  ATTR="${ATTRIBUTION:-unmeasured}" \
  SS="${SAB_SITES:-null}" SSB="${SAB_SITES_BASIS:-unmeasured}" \
  RCK="${RESIDUE_CHECK:-unavailable}" \
  MSC="${MUTANT_SYNTAX_CHECKED:-unchecked}" \
  BKP="${BACKUP:-}" BKPR="$( [[ -n "${BACKUP:-}" && -e "${BACKUP:-}" ]] && echo true || echo false )" \
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
try:
    sites = json.loads(os.environ.get("SS") or "null")
except (ValueError, TypeError):
    sites = None
print(json.dumps({
  "verdict": os.environ["VERDICT"], "reason": os.environ["REASON"],
  "target": os.environ["TARGET"], "baseline_green": os.environ["BG"],
  "sabotage_applied": os.environ["SA"], "sabotage_red": os.environ["SR"],
  "restore_green": os.environ["RG"], "test_ran": os.environ["TR"],
  "restore_status": os.environ["RS"],
  # . restore_status answers "does the target match its BACKUP", which
  # is NOT "is the target clean" — a backup taken over pre-existing residue
  # matches itself and reports ok. This field answers the second question
  # separately so no consumer has to infer one from the other:
  #   "clean"       the injected string is absent from the target after restore
  #   "RESIDUE"     it is PRESENT — sabotage is live regardless of restore_status
  #   "unavailable" NOT CHECKED (--sabotage-sed injects unknowable text). This is
  #                 an absence of measurement, never a pass; a consumer that
  #                 treats it as clean has reintroduced the original defect.
  "residue_check": os.environ["RCK"],
  # . residue_check above is about SABOTAGE TEXT IN THE TARGET. It says
  # nothing about the backup FILE, and never did — a goal filed against this tool
  # read the two as one thing and reported "residue_check: clean while N backup
  # files remained" as a false assertion. It was not: both readings were correct
  # about different objects. The gap was that the JSON described the on-disk
  # backup NOWHERE, so a caller running an N-mutation matrix had no machine-
  # readable signal that N files had been left behind — only free text inside
  # `reason`. These two fields close that, and backup_retained is MEASURED with a
  # real stat at emit time, never inferred from the verdict:
  #   true   the backup is on disk at backup_path (every non-PASS exit, by design)
  #   false  it was removed (the clean-PASS path, the one state with no evidence
  #          value) — or, on a pre-backup refusal, never taken at all
  "backup_path": os.environ["BKP"] or None,
  "backup_retained": os.environ["BKPR"],
  # null => not measured (no --junit-xml, or the parse failed).
  # []   => measured, and the RED run reported no failing testcase at all —
  #         a real signal, not an absence: the run went red WITHOUT any test
  #         failing (compile/build artifact), which is exactly what guard-1220
  #         and guard-1631 warn a bare RED cannot distinguish.
  "red_tests": red, "red_count": red_count, "red_parse_errors": parse_errors,
  # WHICH of the two null-producing situations above actually occurred. A bare
  # null cannot say, and the difference decides what to do next:
  #   "measured"     --junit-xml was given and the extraction ran. red_count is a
  #                  real number (0 included, per the [] note above).
  #   "unmeasured"   --junit-xml was NOT given, so attribution was never asked
  #                  for. NOT a clean result: the kill may have been made by an
  #                  unrelated test, or by a precondition blowing up rather than
  #                  the behavioural assertion, and this run cannot tell. Re-run
  #                  with --junit-xml to find out. Measured 2026-08-28: the flag
  #                  is passed at ~4% of in-repo call sites, so this is the
  #                  DEFAULT state of the tool, not an edge case.
  #   "parse-failed" --junit-xml WAS given and yielded nothing usable — a tool or
  #                  XML fault worth chasing, not an un-run check.
  # Deliberately a separate field rather than a default-on --junit-xml: that flag
  # names where the script LOOKS for XML the test command already wrote, it does
  # not tell the runner where to WRITE. Defaulting it to a path nothing writes
  # would read tests=0 and hard-FAIL every currently-passing caller (the
  #  false-FAIL class, which the Step-3b comment calls worse than a
  # missing check).
  "attribution": os.environ["ATTR"],
  # HOW BROAD the sabotage was. A mutation that lands at the site under test
  # AND at N-1 others makes the RED uninformative: a predicate anchored to the
  # site and one that merely greps the whole file both go red, so a PASS cannot
  # tell them apart. guard-1629 covers the mirror case (sabotage lands at the
  # WRONG site -> false accusation); this covers sabotage landing at the right
  # site PLUS others -> false certification. basis names WHAT was counted:
  # "occurrences" (--sabotage-old, exact) | "changed-lines" (--sabotage-sed,
  # the only mode-independent measure) | "unmeasured" (sabotage never applied).
  "sabotage_sites": sites, "sabotage_sites_basis": os.environ["SSB"],
  # Was the MUTANT proven to still parse before the suite judged it? ()
  # "true" python|shell checked and valid | "unchecked" no validator for this
  # extension. NEVER omitted: "we did not look" and "we looked and it was fine"
  # must not render identically -- the same absent-vs-empty posture red_tests and
  # sabotage_sites_basis already take. An invalid mutant never reaches emit() with
  # a PASS: it exits 1 at Step 2b.
  "mutant_syntax_checked": os.environ["MSC"],
}))'
}

BASELINE_GREEN="skipped"; SAB_APPLIED="false"; SAB_RED="n/a"; RESTORE_GREEN="n/a"; TEST_RAN="unchecked"
MUTANT_SYNTAX_CHECKED="unchecked"
# null (the JSON literal), not [] — see the emit() comment. Absent-vs-empty is
# load-bearing here: [] would claim a measurement that never happened.
RED_TESTS="null"; RED_COUNT="null"; RED_PARSE_ERRORS="null"
# Which of the THREE attribution states the red_tests/red_count pair is in.
# They are both `null` in two DIFFERENT situations and a bare null cannot say
# which, so this names it — the same posture residue_check, sabotage_sites_basis
# and mutant_syntax_checked already take (guard-963: never let "verified nothing"
# render as a clean result). Default is the ~96%-of-call-sites case.
ATTRIBUTION="unmeasured"
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
  # Emit the occurrence count on stdout so the caller can see HOW BROAD the
  # sabotage was (captured into SAB_SITES — it must never reach the script's
  # own stdout, which carries the single-line JSON verdict).
  if ! SAB_SITES="$(TARGET="$TARGET" SAB_OLD="$SAB_OLD" SAB_NEW="$SAB_NEW" $PY -c '
import os, sys
t = os.environ["TARGET"]; old = os.environ["SAB_OLD"]; new = os.environ["SAB_NEW"]
s = open(t, encoding="utf-8").read()
n = s.count(old)
if n == 0:
    sys.exit(7)
open(t, "w", encoding="utf-8").write(s.replace(old, new))
print(n)
')"; then
    emit "FAIL" "sabotage-old string not found in target — no mutation applied, nothing proven"
    exit 1
  fi
  SAB_SITES_BASIS="occurrences"
else
  sed -i "$SAB_SED" "$TARGET" || { emit "FAIL" "sabotage-sed command failed"; exit 1; }
  # --sabotage-sed runs an arbitrary sed program, so an occurrence count is not
  # knowable; changed LINES of the original is the mode-independent breadth
  # measure. Named separately (never let one measure masquerade as another —
  # same posture as red_tests' null-vs-[]): occurrences counts two hits on one
  # line as 2, changed-lines counts them as 1, and understating breadth is the
  # unsafe direction.
  SAB_SITES="$(A="$BACKUP" B="$TARGET" $PY -c '
import os, difflib
a = open(os.environ["A"], encoding="utf-8").read().splitlines()
b = open(os.environ["B"], encoding="utf-8").read().splitlines()
print(sum(1 for l in difflib.unified_diff(a, b, n=0)
          if l.startswith("-") and not l.startswith("---")))
' 2>/dev/null || echo null)"
  # Apply this block's own invariant to its own failure path: if the breadth
  # probe itself failed, sites is null, and naming a basis for a measurement
  # that never happened is exactly the masquerade the comment above forbids.
  if [[ "$SAB_SITES" =~ ^[0-9]+$ ]]; then
    SAB_SITES_BASIS="changed-lines"
  else
    SAB_SITES_BASIS="unmeasured"
  fi
fi
if cmp -s "$BACKUP" "$TARGET"; then
  emit "FAIL" "sabotage produced NO change (no-op mutation) — a passing test would be a false proof"
  exit 1
fi
SAB_APPLIED="true"

# --- Step 2b: the mutant must still PARSE (, guard-1631) ---
# A mutation that leaves the file syntactically invalid reddens the WHOLE suite
# before any assertion runs, and the verdict then credits a worthless test with
# discriminating power -- silent self-congratulation, the one failure mode here
# that looks like success. Measured (): deleting `merged = merged[:limit]`
# orphaned the `if limit > 0:` above it (IndentationError) and reddened all 8 merge
# tests; replacing that line with `pass` instead reddened exactly 1. BOTH runs
# reported 5/5 detected -- only the second run's attribution was real.
#
# THIRD CAUSE, NOT THE ONLY ONE. A broad uniform red has three distinct causes and
# they take opposite remedies, so this guard is deliberately narrow -- it fires ONLY
# on a mutant that does not parse:
#   * mutant does not parse            -> THIS guard: refuse, nothing was proven
#   * the TARGET's own internal guard raised -> guard-4384: faithful mutation, the
#     wide red is legitimate; build a COMPOUND mutation and read red_tests[].message
#   * the harness never ran the code   -> guard-2546: repair the harness
# Refusing on breadth alone would reject guard-4384's legitimate proofs outright.
#
# COMPILE IN MEMORY, NEVER py_compile (guard-3028). py_compile WRITES a .pyc, and
# CPython invalidates bytecode on mtime-seconds+size only -- so a stale mutant .pyc
# survives a correct restore and keeps executing the mutant against restored source,
# presenting as `diff` clean WHILE the test still fails. Writing no bytecode at all
# is strictly cheaper than remembering to clear it.
#
# An extension with no validator is reported as unchecked rather than assumed good:
# "we did not look" and "we looked and it was fine" must not render identically.
case "$TARGET" in
  *.py)
    # Record WHICH validator ran BEFORE running it. The refusal path emits and
    # exits without reaching any trailing assignment, so setting this after the
    # check made the one verdict where the check definitely fired report
    # "unchecked" -- the field contradicting its own reason string. Caught by
    # running the guard against the pre-change script rather than by the tests.
    MUTANT_SYNTAX_CHECKED="python"
    if ! MUTANT_ERR="$(TARGET="$TARGET" $PY -c '
import os, sys
t = os.environ["TARGET"]
src = open(t, encoding="utf-8").read()
try:
    compile(src, t, "exec")   # in memory: writes no .pyc (guard-3028)
except SyntaxError as e:
    sys.stderr.write("%s: %s (line %s)" % (type(e).__name__, e.msg, e.lineno))
    sys.exit(1)
' 2>&1 >/dev/null)"; then
      emit "FAIL" "mutant is not valid Python — $MUTANT_ERR. The mutation left the target unparseable, so the suite would redden before any assertion ran and the RED would prove nothing about the test. Refusing rather than reporting a red (guard-1631). Fix the sabotage so the mutant still parses (e.g. replace a line with a no-op such as \`pass\` instead of deleting it)."
      exit 1
    fi
    ;;
  *.sh|*.bash)
    MUTANT_SYNTAX_CHECKED="shell"
    if ! MUTANT_ERR="$(bash -n "$TARGET" 2>&1)"; then
      emit "FAIL" "mutant is not valid shell — $MUTANT_ERR. The mutation left the target unparseable, so the suite would redden before any assertion ran and the RED would prove nothing about the test. Refusing rather than reporting a red (guard-1631)."
      exit 1
    fi
    ;;
  *)
    MUTANT_SYNTAX_CHECKED="unchecked"
    ;;
esac

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
    ATTRIBUTION="measured"
    RED_TESTS="$(RTJ="$JUNIT_JSON" $PY -c 'import os,json;print(json.dumps(json.loads(os.environ["RTJ"])["red"]))' 2>/dev/null || echo "null")"
    RED_COUNT="$(RTJ="$JUNIT_JSON" $PY -c 'import os,json;print(json.dumps(json.loads(os.environ["RTJ"])["red_count"]))' 2>/dev/null || echo "null")"
    RED_PARSE_ERRORS="$(RTJ="$JUNIT_JSON" $PY -c 'import os,json;print(json.dumps(json.loads(os.environ["RTJ"])["parse_errors"]))' 2>/dev/null || echo "null")"
  else
    # --junit-xml WAS given and the extraction produced nothing usable. Distinct
    # from "unmeasured": the caller DID ask, so a null here is a tool/XML fault
    # worth chasing, not an un-run check.
    ATTRIBUTION="parse-failed"
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
  if [[ "$RESIDUE_CHECK" == "RESIDUE" ]]; then
    emit "RESTORE_FAILED" "restore reproduced the backup byte-for-byte but the BACKUP ITSELF contains the sabotage string, so $TARGET is NOT clean — sabotage is LIVE. Do not read this as a test problem. The backup at $BACKUP is retained; it is the residue, not the recovery."
  else
    emit "RESTORE_FAILED" "restore did not reproduce the backup byte-for-byte — sabotage may be live in $TARGET; recover manually"
  fi
  exit 3
fi

# --- Step 5: restored code must be GREEN again ---
if run_test; then RESTORE_GREEN="true"; else
  RESTORE_GREEN="false"
  #  outcome 5: this reason used to assert "the file matched the
  # backup — the test is flaky or has external state", which points the reader
  # at their own test and away from an unrestored file. That is a safety tool
  # whose failure mode is MISDIRECTION. It now states what was and was not
  # established, and names the two checks a reader should actually run.
  emit "FAIL" "post-restore RED: the test fails after restore. What is established: the file matches its backup${RESIDUE_CHECK:+ and residue_check=$RESIDUE_CHECK}. What is NOT established: that the file is CLEAN — a backup taken over pre-existing residue matches itself, and --sabotage-sed cannot be residue-checked at all. Before concluding the test is flaky, grep $TARGET for the injected string and, if the target is under world/ or meta/, verify the AUTHORITATIVE copy with backend-cat.sh (NOT world-cat.sh, which reads the local mirror). The backup at $BACKUP is retained for exactly this."
  exit 1
fi

PASS_REASON="mutation-proof: GREEN on real code -> RED under sabotage -> GREEN after restore; the test catches its regression and restore left no sabotage behind"
# A multi-site sabotage yields a RED that any predicate touching the token
# anywhere would also produce, so the PASS is weaker than it reads. Say so in
# the reason rather than downgrading the verdict: the proof IS evidence, just
# for a smaller proposition than "this test is anchored" (guard-1856).
if [[ "${SAB_SITES:-}" =~ ^[0-9]+$ ]] && (( SAB_SITES > 1 )); then
  PASS_REASON="${PASS_REASON}. CAVEAT: the sabotage changed ${SAB_SITES} sites (basis=${SAB_SITES_BASIS}), not one — this RED does NOT prove the test is anchored to the site under test, because a predicate that merely matches the token ANYWHERE in the file goes red too. Narrow the mutation to one site (e.g. --sabotage-sed '0,/re/s//new/') to prove anchoring."
fi
# The PASS above is silent about attribution unless we say so here: red_tests
# null renders identically whether nobody asked or nobody failed, and a reader
# with a PASS in hand has no prompt to look. Caveat, never downgrade — the proof
# IS real, it just does not establish WHICH test did the killing (guard-1856,
# the same posture the multi-site caveat above takes).
if [[ "$ATTRIBUTION" == "unmeasured" ]]; then
  PASS_REASON="${PASS_REASON}. CAVEAT: kill attribution was NOT measured (no --junit-xml), so red_tests/red_count are null because nothing was asked, not because nothing failed — this PASS does not establish WHICH test went red under the sabotage. An unrelated test can kill a mutant while the intended one stays green, and a precondition blowing up looks identical to the behavioural assertion firing. Re-run with --junit-xml '<result-xml-path-or-glob>' to attribute the kill."
elif [[ "$ATTRIBUTION" == "parse-failed" ]]; then
  PASS_REASON="${PASS_REASON}. CAVEAT: --junit-xml was given but produced no usable attribution (see red_parse_errors), so this PASS does not establish WHICH test went red."
fi
#  outcome 3: the ONLY place the backup is removed. Reaching here means
# restore matched, residue_check did not fire, and the post-restore test is green
# — the one state in which the backup has no evidentiary value. Every other exit
# path, including the trap, now leaves it on disk. `rm -rf` on the DIR rather
# than `rm -f` on the file since  moved the backup into a per-run
# mktemp dir — removing only the file would leave an empty dir behind on every
# passing run, which is the same accumulation defect in a quieter form.
rm -rf "$BACKUP_DIR"
emit "PASS" "$PASS_REASON"
exit 0
