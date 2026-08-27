#!/usr/bin/env bash
# test_guardrails_update_field_argv.sh — argv-strictness pins for the positional
# store wrappers that share core/scripts/_argv_strict.sh.
#
# THIS FILE IS A MERGE of two independent fixes for the same defect, written the
# same day on two boxes:  (inline strict parser + a deep single-wrapper
# suite) and  (shared helper + a broad four-wrapper suite). They
# collided as an add/add conflict. Neither side was discardable — each pinned an
# axis the other missed — so this is their union, not a winner.
#
# THE BUG BOTH SIDES PIN. The old parser was `-*) shift;;` — it silently
# DISCARDED any leading-dash argument and silently ignored a 4th positional. So
#     guardrails-update-field.sh <id> rule --value-file <path>
# dropped the (then-nonexistent) flag and wrote the literal PATH STRING as the
# rule, rc=0, no error on any channel. Measured 2026-08-01: guard-1615
# (times_active=677, 1,400+ chars) was replaced by 87 characters, and survived
# only because a mandated read-back caught it while the original text was still
# in the author's context. Trusting rc=0, the only recovery would have been a
# .history snapshot that nothing would have told anyone to look for.
#
# Because the need behind that invocation was real — multi-KB rule bodies are
# genuinely awkward as argv — --value-file / --value-stdin now make the tempting
# shape actually work, instead of silently corrupting. Refusing the shape without
# supplying the capability would only have moved the pressure somewhere worse.
#
# THREE ASSERTION AXES, each load-bearing — do not "simplify" any of them away:
#
#  1. rc == 2 SPECIFICALLY, never merely non-zero. rc 2 is emitted ONLY by the
#     argv parser, before _runtime.sh is even sourced. "Non-zero" is also
#     satisfied by a DOWNSTREAM daemon failure, so it cannot distinguish "the
#     parser refused" from "the parser accepted and the write happened to fail"
#     — and under the original bug the write could just as easily SUCCEED and
#     clobber. Mutation-proved on this very code: with the guard reverted to a
#     silent `shift`, the unknown-flag path exits 1 — non-zero, and therefore
#     invisible to the weaker assertion.
#
#  2. The record is BYTE-IDENTICAL after a refused invocation. A script can exit
#     non-zero and still have written; only comparing the stored value proves it
#     did not. Exit codes alone would not have caught the original bug's shape.
#
#  3. Valid shapes are ACCEPTED (rc != 2). Without this axis a parser that
#     refused EVERYTHING would pass axes 1 and 2 perfectly. This is the axis the
#      side lacked, and it is the one that keeps the guard honest.
#
# Part 1 runs all three axes deeply against guardrails-update-field.sh — the
# store where the incident actually happened and where a known-good reader
# exists. Part 2 runs axes 1 and 3 across all four wrappers: they now share ONE
# parser, so re-proving byte-identity per store would re-measure the same code,
# while the rc pins DO catch per-wrapper wiring drift (a missed source, a wrong
# arity). Depth where the evidence is, breadth where the wiring is.
#
# Nothing here mutates a live record: every negative case exits inside the
# parser before the daemon call, and every positive case uses a deliberately
# nonexistent id, so an accepted parse still cannot land a write.

set -uo pipefail

# THREE levels up, not two: this file lives at core/scripts/tests/, so ../..
# lands on core/ and every path below becomes core/core/scripts/... . That
# mistake does not fail loudly on its own — it makes the subject unreadable,
# which would trip a graceful skip and report "0 passed, 0 failed". Assert the
# resolution instead of trusting it.
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "$0")/../../.." && pwd)}"
S="$PROJECT_ROOT/core/scripts/guardrails-update-field.sh"
R="$PROJECT_ROOT/core/scripts/guardrails-read.sh"
HELPER="$PROJECT_ROOT/core/scripts/_argv_strict.sh"
for _p in "$S" "$R" "$HELPER"; do
  [ -f "$_p" ] || { echo "FATAL: resolved a path that does not exist: $_p (PROJECT_ROOT=$PROJECT_ROOT)" >&2; exit 1; }
done

PASS=0; FAIL=0
ok(){ PASS=$((PASS+1)); echo "  PASS  $1"; }
no(){ FAIL=$((FAIL+1)); echo "  FAIL  $1"; echo "        got: $2"; }
chk(){ [ "$2" = "$3" ] && ok "$1" || no "$1" "$2 (want $3)"; }
chk_ne(){ [ "$2" != "$3" ] && ok "$1" || no "$1" "$2 (want NOT $3)"; }

ok "shared helper present: _argv_strict.sh"

# stdin is redirected from /dev/null on every invocation that does not
# deliberately pipe: an accepted `--value-stdin` parse reaches `cat`, which
# would otherwise inherit the terminal and hang the suite forever.
rc_of(){ bash "$S" "$@" >/dev/null 2>&1 </dev/null; echo $?; }

# ── Subject discovery ───────────────────────────────────────────────────────
# DISCOVERED rather than hardcoded: the pinned local backend (guard-955) is a
# different snapshot from the live store, so any fixed id makes this suite skip
# on one backend or the other.
SUBJECT="$(bash "$R" --active 2>/dev/null | py -3 -c '
import sys,json
try: d=json.load(sys.stdin)
except Exception: raise SystemExit
rows = d if isinstance(d,list) else d.get("guardrails",[])
print(rows[0].get("id","") if rows else "")
' 2>/dev/null)"

rule_of(){ bash "$R" --id "$1" 2>/dev/null | py -3 -c '
import sys,json
try: d=json.load(sys.stdin)
except Exception: print("<<unreadable>>"); raise SystemExit
r = d if isinstance(d,dict) else (d[0] if d else {})
print(r.get("rule","<<absent>>"))
'; }

BEFORE=""
[ -n "$SUBJECT" ] && BEFORE="$(rule_of "$SUBJECT")"

echo "== part 1: guardrails-update-field.sh — refusal, non-mutation, acceptance =="

if [ -z "$SUBJECT" ] || [ -z "$BEFORE" ] || [ "$BEFORE" = "<<unreadable>>" ] || [ "$BEFORE" = "<<absent>>" ]; then
  # FAIL, do not SKIP. A permanently-skipping test is indistinguishable from a
  # passing one, and this suite guards a silent-corruption path — precisely the
  # class where a false all-clear is most expensive. (The  side argued
  # exactly this in its own header and then shipped `exit 0` anyway; taking the
  # loud branch honors its argument over the code that contradicted it.)
  no "discover a readable subject guardrail" \
     "SUBJECT='${SUBJECT:-}' BEFORE='${BEFORE:-}' — daemon down or store empty; axis 2 is untestable, failing rather than reporting a hollow pass"
else
  ok "discovered live subject: $SUBJECT"

  TMPV="$(mktemp)"; printf 'a replacement rule body\n' > "$TMPV"

  # 1. The exact invocation from the incident. Must refuse AND not write.
  chk "unknown flag rejected by parser (rc=2)"         "$(rc_of "$SUBJECT" rule --value-file-typo "$TMPV")" "2"
  chk "unknown flag left rule BYTE-IDENTICAL"          "$(rule_of "$SUBJECT")" "$BEFORE"

  # 2. A 4th positional was the other silent-drop path.
  chk "4th positional rejected by parser (rc=2)"       "$(rc_of "$SUBJECT" rule "newvalue" "extra-arg")" "2"
  chk "4th positional left rule BYTE-IDENTICAL"        "$(rule_of "$SUBJECT")" "$BEFORE"

  # 3. Ambiguity is refused rather than resolved by precedence.
  chk "two value sources rejected by parser (rc=2)"    "$(rc_of "$SUBJECT" rule "positional" --value-file "$TMPV")" "2"
  chk "two value sources left rule BYTE-IDENTICAL"     "$(rule_of "$SUBJECT")" "$BEFORE"

  # 4. Flag-shaped mistakes that must not reach a write.
  chk "--value-file with no path rejected (rc=2)"      "$(rc_of "$SUBJECT" rule --value-file)" "2"
  chk "--value-file with missing file rejected (rc=2)" "$(rc_of "$SUBJECT" rule --value-file "$TMPV.nope")" "2"
  chk "flag-shape errors left rule BYTE-IDENTICAL"     "$(rule_of "$SUBJECT")" "$BEFORE"

  echo "== usage paths =="
  bash "$S" --help >/dev/null 2>&1 </dev/null
  chk "--help exits 0" "$?" "0"
  bash "$S" "$SUBJECT" >/dev/null 2>&1 </dev/null
  chk "missing value exits 1 (unchanged legacy contract)" "$?" "1"

  echo "== positive: valid shapes are ACCEPTED by the parser =="
  # rc 2 is reserved for argv rejection, so "not 2" is exactly the claim under
  # test: the parser accepted the shape. A nonexistent id keeps any write from
  # landing, so these stay side-effect-free while still exercising the parse.
  BOGUS="guard-zzz-nonexistent-argv-test"
  bash "$S" "$BOGUS" rule "a value" >/dev/null 2>&1 </dev/null
  chk_ne "3-positional shape is accepted by the parser" "$?" "2"
  bash "$S" "$BOGUS" rule --value-file "$TMPV" >/dev/null 2>&1 </dev/null
  chk_ne "--value-file shape is accepted by the parser" "$?" "2"
  printf 'from stdin\n' | bash "$S" "$BOGUS" rule --value-stdin >/dev/null 2>&1
  chk_ne "--value-stdin shape is accepted by the parser" "$?" "2"

  rm -f "$TMPV"

  # Final guard: nothing in part 1 may have altered the subject.
  chk "subject rule unchanged across part 1" "$(rule_of "$SUBJECT")" "$BEFORE"
fi

echo "== part 2: every wrapper sharing _argv_strict.sh — refusal + acceptance =="

# Six of the fifteen scripts carrying the old parser MUTATE records, so the
# shared guard has to hold at every call site, not just the one that broke.
PORTED=(
    guardrails-update-field
    reasoning-bank-update-field
    pattern-signatures-update-field
    spark-questions-update-field
)

for name in "${PORTED[@]}"; do
    SUT="$PROJECT_ROOT/core/scripts/$name.sh"
    echo "-- $name"
    if [ ! -f "$SUT" ]; then no "$name: subject exists" "not found: $SUT"; continue; fi
    bash -n "$SUT" 2>/dev/null && ok "$name: syntax parses" || no "$name: syntax parses" "bash -n failed"

    grep -q '_argv_strict.sh' "$SUT" \
        && ok "$name: sources the shared parser" \
        || no "$name: sources the shared parser" "no _argv_strict.sh reference — drifted back to a local copy?"

    r_of(){ bash "$SUT" "$@" >/dev/null 2>&1 </dev/null; echo $?; }
    BOG="zzz-nonexistent-argv-test"

    # Axis 1 — refusals. These exit inside the parser, before any store access,
    # so a nonexistent id is sufficient and the cases stay hermetic.
    chk "$name: unknown option refused, not discarded"  "$(r_of "$BOG" rule --bogus)" "2"
    chk "$name: unknown option BEFORE the positionals"  "$(r_of --bogus "$BOG" rule value)" "2"
    chk "$name: 4th positional refused, not discarded"  "$(r_of "$BOG" rule value EXTRA)" "2"
    chk "$name: --value-file with no path argument"     "$(r_of "$BOG" rule --value-file)" "2"
    chk "$name: --value-file naming a missing file"     "$(r_of "$BOG" rule --value-file /tmp/ayoai-argv-absent-zzz.txt)" "2"
    chk "$name: two value sources at once"              "$(r_of "$BOG" rule value --value-stdin)" "2"
    chk "$name: --help exits clean"                     "$(r_of --help)" "0"

    # Axis 3 — acceptance. Without these, a wrapper wired to refuse everything
    # would pass every line above.
    chk_ne "$name: 3-positional shape accepted"         "$(r_of "$BOG" rule "a value")" "2"
done

echo
echo "argv-strictness tests: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
