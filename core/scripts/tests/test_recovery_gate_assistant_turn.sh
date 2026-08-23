#!/usr/bin/env bash
# domain-leak-exempt: framework recovery infra; stub names are core script literals
#
# WIRING proof for the assistant-turn liveness veto in recovery-gate Path D
# (g-115-6253). test_assistant_turn_freshness.py proves the PROBE; this proves
# the GATE — that the shipped `_check_wedged_loop` actually consults it and
# branches on its rc. guard-1943: a green suite certifies the FUNCTION, never
# the WIRING, and those are two different things here — the probe could be
# perfect while the gate is dead code, or absent, or placed after the recovery.
#
# HOW THIS TESTS THE REAL THING. Same harness shape as
# test_recovery_gate_runner_age.sh: recovery-gate.sh has no main guard, so
# sourcing it whole would run Paths A/B/C/D against the LIVE agent. Instead the
# harness awk-extracts the shipped `_check_wedged_loop` text and sources just
# that, stubbing its six dependencies plus agent_dir and _perform_recovery. The
# assertions therefore run the REAL branch ordering from the shipped file, not a
# re-implementation of the predicate in the test (guard-920).
#
# Every scenario ages runner-token past the wedge threshold so the UPSTREAM
# runner-age gate cannot be what suppresses — otherwise scenario 1 would pass
# for the wrong reason and prove nothing about this gate at all.
#
# Scenarios:
#   1. probe rc=0 (recent assistant turn) -> NO recovery. The fix itself.
#   2. probe rc=1 (no recent turn)        -> recovery FIRES. The gate must not
#      over-suppress; a veto that always vetoes is a deletion of Path D.
#   3. probe rc=2 (present-but-unreadable)-> NO recovery. guard-487
#      fail-closed-as-suppressed, matching phase-wedge-check's rc=2 posture.
#   4. probe rc=1 with verdict=no_transcript -> recovery FIRES, asserted through
#      the REAL function. ABSENCE IS NOT EVIDENCE OF LIVENESS: measured cc-02
#      2026-08-15, only the box-RESIDENT agent has a transcript, so treating
#      absent as suppression would disable Path D everywhere else — a deletion
#      of Path D rather than the narrowing this change intends. The python suite
#      pins the probe's half of this; only this scenario pins the gate's.
#   5. probe emits NOTHING and exits 1 -> NO recovery. Empty stdout can only
#      mean the probe died BEFORE its own emit (module-level ImportError, a
#      syntax error, python3 off the hook's PATH), and Python exits 1 for all
#      of those — landing on the "proceed to recovery" branch. Found by the
#      g-115-6253 fresh-eyes review AFTER the other 92 tests were green, because
#      none of them exercises module import. Pair with scenario 2, which is the
#      guard-2175 success-path control for the same clause.
#   6. probe emits NON-JSON text on an unknown rc -> NO recovery. The Store-stub
#      shape ("Python was not found...", rc=49). Scenario 5 caught only the
#      SILENT death; this is the LOUD one, and it survived that fix — measured
#      cc-02 2026-08-15, RECOVERED=1 against a live agent.
#   7. probe emits TRUNCATED JSON on rc=1 -> NO recovery. `{"verdict": ` starts
#      like a verdict and is not one; this is what the `}` anchor in the gate's
#      pattern exists to reject, so it is the scenario that would go red if
#      someone "simplified" that anchor away.
#   8. MUTATION PROOF: neuter the gate's CONDITION (`if <cond>` -> `if false`)
#      and confirm scenario 1 flips to RECOVERED — i.e. these assertions can
#      actually go red. Anchored on the CONDITION LINE by its stable prefix
#      (`if [[ "$turn_rc"`), never on the full condition text and never on a
#      comment or message. Two reasons: neutering leaves every string in the
#      body intact, so a prose-anchored check would still "pass" against a
#      switched-off gate; and a full-text anchor goes stale the moment the
#      predicate is refined — which it did, in the very next change (rounds 1
#      and 2 both rewrote this condition). A stale anchor fails LOUDLY here
#      ("expected exactly 1 neutered site, got 0"), never silently.
#
# Run: bash core/scripts/tests/test_recovery_gate_assistant_turn.sh
# Exit 0 = all pass, 1 = any failure.

set -uo pipefail
SCRIPT_DIR_SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORE_SCRIPTS="$(cd "$SCRIPT_DIR_SELF/.." && pwd)"
GATE="$CORE_SCRIPTS/recovery-gate.sh"
TMP=$(mktemp -d)
trap "rm -rf '$TMP'" EXIT

failures=0

if ! date -u -d "5 minutes ago" +%Y >/dev/null 2>&1; then
    echo "SKIP: GNU date -d unavailable — cannot age fixtures"
    exit 0
fi

# ─── Harness: stub the function's dependencies, source the REAL function ────
SCRIPT_DIR="$TMP/bin"; mkdir -p "$SCRIPT_DIR"
ADIR="$TMP/agents/echo"; mkdir -p "$ADIR/session"

printf '#!/usr/bin/env bash\necho RUNNING\n' > "$SCRIPT_DIR/session-state-get.sh"
printf '#!/usr/bin/env bash\necho fresh\n'   > "$SCRIPT_DIR/heartbeat-stale.sh"
printf '#!/usr/bin/env bash\nexit 1\n'       > "$SCRIPT_DIR/session-signal-exists.sh"
printf '#!/usr/bin/env bash\nexit 1\n'       > "$SCRIPT_DIR/background-jobs.sh"
printf 'import json\nprint(json.dumps({"verdict":"wedged","age_minutes":71.0,"threshold_minutes":65.0}))\n' \
    > "$SCRIPT_DIR/phase-wedge-check.py"
chmod +x "$SCRIPT_DIR"/*.sh

# Runner-token aged well past the 65min wedge threshold in EVERY scenario, so
# the upstream runner-age gate never short-circuits ahead of the gate under test.
echo "11111111-2222-3333-4444-555555555555" > "$ADIR/session/runner-token"
touch -d "200 minutes ago" "$ADIR/session/runner-token" 2>/dev/null

agent_dir() { echo "$ADIR"; }
RECOVERED=0
_perform_recovery() { RECOVERED=1; }

_extract_fn() {   # $1 = source file, $2 = dest ; optional $3 = sed mutation
    if [[ -n "${3:-}" ]]; then
        awk '/^_check_wedged_loop\(\) \{/,/^\}/' "$1" | sed "$3" > "$2"
    else
        awk '/^_check_wedged_loop\(\) \{/,/^\}/' "$1" > "$2"
    fi
    [[ -s "$2" ]]
}

if ! _extract_fn "$GATE" "$TMP/fn.sh"; then
    echo "FAIL: could not extract _check_wedged_loop from recovery-gate.sh (function renamed?)"
    exit 1
fi
# shellcheck disable=SC1090
source "$TMP/fn.sh"

_set_probe() {   # $1 = exit code, $2 = verdict string
    printf 'import json,sys\nprint(json.dumps({"verdict":"%s","suppress":%s}))\nsys.exit(%s)\n' \
        "$2" "$([[ "$1" == "0" ]] && echo True || echo False)" "$1" \
        > "$SCRIPT_DIR/assistant-turn-freshness.py"
}

_run_case() {   # $1 = probe rc, $2 = verdict
    _set_probe "$1" "$2"
    RECOVERED=0
    _check_wedged_loop echo >/dev/null 2>&1
    echo "$RECOVERED"
}

# ─── Scenario 1: a recent assistant turn must SUPPRESS ─────────────────────
if [[ "$(_run_case 0 recent_assistant_turn)" == "0" ]]; then
    echo "PASS: scenario 1 (probe rc=0, recent assistant turn) — NO recovery; the false-fire class is closed"
else
    echo "FAIL: scenario 1 — a live assistant turn did NOT suppress; the veto is not wired into Path D"
    failures=$((failures+1))
fi

# ─── Scenario 2: no recent turn must still allow the genuine wedge ─────────
if [[ "$(_run_case 1 no_recent_assistant_turn)" == "1" ]]; then
    echo "PASS: scenario 2 (probe rc=1, no recent turn) — recovery FIRES; genuine fleet-wedge path intact"
else
    echo "FAIL: scenario 2 — the veto suppressed a genuine wedge; Path D is deleted, not narrowed"
    failures=$((failures+1))
fi

# ─── Scenario 3: unreadable transcript fails CLOSED as suppressed ──────────
if [[ "$(_run_case 2 unreadable)" == "0" ]]; then
    echo "PASS: scenario 3 (probe rc=2, unreadable) — NO recovery; guard-487 fail-closed-as-suppressed"
else
    echo "FAIL: scenario 3 — an unreadable probe allowed recovery; suppression gate failed OPEN"
    failures=$((failures+1))
fi

# ─── Scenario 4: absence is NOT evidence of liveness ───────────────────────
if [[ "$(_run_case 1 no_transcript)" == "1" ]]; then
    echo "PASS: scenario 4 (probe rc=1, no_transcript) — recovery FIRES; absence is not liveness, Path D survives off-box"
else
    echo "FAIL: scenario 4 — a missing transcript suppressed recovery; Path D is dead on every box the runner does not live on"
    failures=$((failures+1))
fi

# ─── Scenario 5: probe died BEFORE its emit (empty stdout) must SUPPRESS ───
# The defect this pins shipped green past all 92 other tests, because every one
# of them either calls check() in-process or stubs the probe — so none exercises
# the MODULE IMPORT path, where _paths/_dt sit outside every try and main()'s
# belt-and-braces cannot reach them. Python exits 1 on an unhandled module-level
# exception, and rc=1 is this gate's "proceed to recovery" branch, so the single
# most likely way for the probe to break mapped to the one outcome that kills a
# healthy loop. Measured cc-02 2026-08-15: one broken import -> rc=1, 0 bytes of
# stdout, live agent recovered, traceback eaten by 2>/dev/null.
# The stub emits NOTHING and exits 1 — the exact production shape (guard-920),
# not a synthetic rc=2.
# Scenario 2 above is the SUCCESS-PATH test guard-2175 mandates for this change:
# it is rc=1 WITH non-empty JSON and must still FIRE, which is precisely the
# branch a careless `-z` clause would have broken. Read 2 and 5 as a pair.
printf 'import sys\nsys.exit(1)\n' > "$SCRIPT_DIR/assistant-turn-freshness.py"
RECOVERED=0
_check_wedged_loop echo >/dev/null 2>&1
if [[ "$RECOVERED" == "0" ]]; then
    echo "PASS: scenario 5 (probe emitted nothing, rc=1) — NO recovery; a died-before-emit probe is an error, not a no-turn verdict"
else
    echo "FAIL: scenario 5 — a probe that produced NO output still allowed recovery; a broken import silently reverts Path D to pre-fix behaviour"
    failures=$((failures+1))
fi

# ─── Scenario 6: NON-JSON output on an unknown rc must SUPPRESS ────────────
# Round 1 (scenario 5) split off the SILENT failure — the probe dies with 0
# bytes. This is the LOUD one, and it SURVIVED that fix: any breakage that still
# manages to PRINT something landed straight back on the proceed-to-recovery
# branch, because the discriminator was emptiness rather than "did the probe
# emit a verdict at all". Measured cc-02 2026-08-15 against the round-1 gate:
# RECOVERED=1, i.e. a live agent recovered.
# The canonical instance is the Microsoft Store python3 stub ("Python was not
# found...", rc=49). On this box recovery-gate.sh sources _paths.sh, which
# normally prevents it — which is exactly why it must be PINNED rather than
# trusted: that protection lives in a different file this gate does not control,
# and a gate must not depend on a neighbour staying correct (guard-1943).
printf 'import sys\nsys.stdout.write("Python was not found; run without arguments")\nsys.exit(49)\n' \
    > "$SCRIPT_DIR/assistant-turn-freshness.py"
RECOVERED=0
_check_wedged_loop echo >/dev/null 2>&1
if [[ "$RECOVERED" == "0" ]]; then
    echo "PASS: scenario 6 (non-JSON stdout, rc=49) — NO recovery; an unrecognised probe outcome is an error, not a no-turn verdict"
else
    echo "FAIL: scenario 6 — a probe printing non-JSON on an unknown rc still allowed recovery; the branch is fail-OPEN (guard-487)"
    failures=$((failures+1))
fi

# ─── Scenario 7: TRUNCATED JSON on rc=1 must SUPPRESS ──────────────────────
# `{"verdict": ` opens like a real verdict and carries the key, so a pattern
# anchored only on `{` plus `"verdict"` would admit it. The CLOSING `}` anchor is
# what rejects it, which makes this the scenario that goes red if that anchor is
# ever "simplified" away. Command substitution strips print()'s trailing newline,
# so a healthy payload genuinely ends in `}` — scenarios 2 and 4 are the controls
# proving this does not over-suppress the real verdicts.
printf 'import sys\nsys.stdout.write("{\\"verdict\\": ")\nsys.exit(1)\n' \
    > "$SCRIPT_DIR/assistant-turn-freshness.py"
RECOVERED=0
_check_wedged_loop echo >/dev/null 2>&1
if [[ "$RECOVERED" == "0" ]]; then
    echo "PASS: scenario 7 (truncated JSON, rc=1) — NO recovery; a partial payload is not a verdict"
else
    echo "FAIL: scenario 7 — a truncated payload was accepted as a verdict; the closing-brace anchor is gone or ineffective"
    failures=$((failures+1))
fi

# ─── Scenario 8: MUTATION PROOF on the shipped CONDITION ───────────────────
# Anchored on the condition LINE by its stable prefix, NOT on the full predicate
# text. The previous anchor spelled the whole condition out and went stale the
# moment the predicate was refined — which round 2 did immediately. It failed
# loudly (correct), but it would fail that way on every legitimate refinement,
# and an anchor that cries wolf on good changes gets "fixed" by deletion.
# `@` delimiter, NOT `|`: the condition CONTAINS `||`, which would terminate an
# s|...|...| expression mid-pattern. sed reports "unknown option to `s'" and
# _extract_fn returns non-zero, so a broken anchor fails LOUDLY rather than
# silently building a non-mutant and passing — the failure mode that would make
# scenarios 1-7 unfalsifiable.
MUT='s@^    if \[\[ "$turn_rc".*$@    if false; then@'
if _extract_fn "$GATE" "$TMP/fn_mutant.sh" "$MUT"; then
    neutered=$(grep -c 'if false; then' "$TMP/fn_mutant.sh")
    if [[ "$neutered" -eq 1 ]]; then
        mutant_result=$(
            source "$TMP/fn_mutant.sh"
            RECOVERED=0
            _perform_recovery() { RECOVERED=1; }
            _set_probe 0 recent_assistant_turn
            _check_wedged_loop echo >/dev/null 2>&1
            echo "$RECOVERED"
        )
        if [[ "$mutant_result" == "1" ]]; then
            echo "PASS: scenario 8 — mutation proof: neutering the veto condition (1 site) reproduces the false-fire (RECOVERED=1)"
        else
            echo "FAIL: scenario 8 — neutered veto still suppressed (RECOVERED=$mutant_result); scenarios 1-7 prove nothing"
            failures=$((failures+1))
        fi
    else
        echo "FAIL: scenario 8 — expected exactly 1 neutered site, got $neutered (anchor no longer matches the condition LINE; re-anchor MUT)"
        failures=$((failures+1))
    fi
else
    echo "FAIL: scenario 8 — could not build mutant"
    failures=$((failures+1))
fi

# ─── Summary ──
if [[ $failures -eq 0 ]]; then
    echo ""
    echo "All 8 scenarios passed (7 behavioral on the real function + 1 mutation proof)."
    exit 0
fi
echo ""
echo "$failures scenario(s) FAILED"
exit 1
