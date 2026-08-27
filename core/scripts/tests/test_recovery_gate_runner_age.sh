#!/usr/bin/env bash
# test_recovery_gate_runner_age.sh — 
#
# Pins recovery-gate.sh Path D's RUNNER-AGE gate: Path D must not fire when the
# CURRENT runner is younger than the wedge threshold, because a diary marker
# older than the current runner is INHERITED and cannot be evidence about it.
#
# Incident being pinned (2026-08-05T11:24:38, alpha, cc-04): a session 4m55s old
# was auto-recovered RUNNING->IDLE mid-boot. /start neither rolls execution-
# diary.jsonl nor stamps entries with a sid, so the last marker — a phase_start
# from the PREVIOUS run, aged 70.9min against the 65min threshold — was read as
# evidence about the brand-new runner. Recurrence: same cause string for alpha at
# 2026-07-20T07:18:06.
#
# HOW THIS TESTS THE REAL THING. recovery-gate.sh has no main guard, so sourcing
# it whole would run Paths A/B/C/D against the LIVE agent. Instead the harness
# extracts the shipped `_check_wedged_loop` function text with awk and sources
# just that, stubbing its six dependencies (session-state-get, heartbeat-stale,
# phase-wedge-check, session-signal-exists, background-jobs,
# assistant-turn-freshness) plus agent_dir and
# _perform_recovery. So the assertions run the REAL branch ordering and the REAL
# runner-age condition from the shipped file — not a re-implementation of the
# predicate in the test, which is the failure mode that lets a pin pass while
# production is broken (guard-920).
#
# PRODUCTION SHAPE: the stubbed detector returns verdict=wedged rc=0 with
# threshold_minutes=65 — i.e. every scenario presents a diary the detector
# genuinely calls WEDGED. That is correct detector behaviour (it is a pure diary
# detector by contract and cannot see runners), so the runner-age gate is the
# ONLY thing standing between that verdict and a false recovery. Without this
# framing the scenarios would be vacuous.
#
# Scenarios:
#   1. NEGATIVE (the bug): wedged diary + runner 5min old   -> NO recovery
#   2. POSITIVE (must not regress): same diary + runner 200min -> recovery FIRES
#   3. Missing runner-token -> recovery FIRES (an undeterminable runner age must
#      not silently disable the genuine-wedge path — the costlier failure)
#   4. MUTATION PROOF: neuter the gate's CONDITION (`if <cond>` -> `if false`)
#      and confirm scenario 1 flips to RECOVERED — i.e. these assertions can
#      actually go red, and they reproduce the pre-fix behaviour exactly.
#      Anchored on the CONDITION, never on a comment or message: neutering
#      leaves every string in the body intact, so a prose-anchored check would
#      still "pass" against a switched-off gate.
#
# Run: bash core/scripts/tests/test_recovery_gate_runner_age.sh
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
# Stands in for the pure diary detector: WEDGED, past a 65min threshold.
printf 'import json\nprint(json.dumps({"verdict":"wedged","age_minutes":71.0,"threshold_minutes":65.0}))\n' \
    > "$SCRIPT_DIR/phase-wedge-check.py"
# Assistant-turn liveness veto (): NO recent turn -> rc 1 -> does not
# suppress, so these scenarios exercise the runner-age gate rather than this one.
# WITHOUT this stub the real probe runs, resolves the LIVE bound agent's
# transcript, finds this very session's assistant turn, and suppresses every
# scenario — which is what happened when the gate first landed. Scenario 4's
# mutation proof is what caught it: it reported "neutered gate still suppressed
# ... scenarios 1-3 prove nothing" rather than letting three vacuous PASSes
# through.
printf 'import json,sys\nprint(json.dumps({"verdict":"no_recent_assistant_turn","suppress":False}))\nsys.exit(1)\n' \
    > "$SCRIPT_DIR/assistant-turn-freshness.py"
chmod +x "$SCRIPT_DIR"/*.sh

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

_run_case() {   # $1 = token age in minutes, or "absent"
    RECOVERED=0
    rm -f "$ADIR/session/runner-token"
    if [[ "$1" != "absent" ]]; then
        echo "11111111-2222-3333-4444-555555555555" > "$ADIR/session/runner-token"
        touch -d "$1 minutes ago" "$ADIR/session/runner-token" 2>/dev/null
    fi
    _check_wedged_loop echo >/dev/null 2>&1
    echo "$RECOVERED"
}

# ─── Scenario 1: NEGATIVE — a fresh runner must NOT be recovered ────────────
if [[ "$(_run_case 5)" == "0" ]]; then
    echo "PASS: scenario 1 (runner 5min, detector says wedged) — NO recovery; the false-recovery incident cannot recur"
else
    echo "FAIL: scenario 1 — a 5min-old runner WAS recovered; g-328-45 has regressed"
    failures=$((failures+1))
fi

# ─── Scenario 2: POSITIVE — the genuine 2026-07-04 wedge must still fire ────
if [[ "$(_run_case 200)" == "1" ]]; then
    echo "PASS: scenario 2 (runner 200min, detector says wedged) — recovery FIRES; genuine fleet-wedge path intact"
else
    echo "FAIL: scenario 2 — a 200min-old wedged runner was NOT recovered; genuine wedge recovery is DEAD"
    failures=$((failures+1))
fi

# ─── Scenario 3: missing token — must not suppress ──────────────────────────
if [[ "$(_run_case absent)" == "1" ]]; then
    echo "PASS: scenario 3 (runner-token absent) — recovery FIRES; undeterminable age does not disable the wedge path"
else
    echo "FAIL: scenario 3 — a missing runner-token suppressed recovery"
    failures=$((failures+1))
fi

# ─── Scenario 4: MUTATION PROOF on the shipped CONDITION ────────────────────
MUT='s|if \[\[ -n "$wedge_thresh" && -f "$rtok" \]\]; then|if false; then|'
if _extract_fn "$GATE" "$TMP/fn_mutant.sh" "$MUT"; then
    neutered=$(grep -c 'if false; then' "$TMP/fn_mutant.sh")
    if [[ "$neutered" -eq 1 ]]; then
        # Re-source the mutant in a subshell so the live function is untouched.
        mutant_result=$(
            source "$TMP/fn_mutant.sh"
            RECOVERED=0
            _perform_recovery() { RECOVERED=1; }
            rm -f "$ADIR/session/runner-token"
            echo "tok" > "$ADIR/session/runner-token"
            touch -d "5 minutes ago" "$ADIR/session/runner-token" 2>/dev/null
            _check_wedged_loop echo >/dev/null 2>&1
            echo "$RECOVERED"
        )
        if [[ "$mutant_result" == "1" ]]; then
            echo "PASS: scenario 4 — mutation proof: neutering the gate condition (1 site) reproduces the bug (RECOVERED=1)"
        else
            echo "FAIL: scenario 4 — neutered gate still suppressed (RECOVERED=$mutant_result); scenarios 1-3 prove nothing"
            failures=$((failures+1))
        fi
    else
        echo "FAIL: scenario 4 — expected exactly 1 neutered site, got $neutered (condition text drifted; re-anchor MUT)"
        failures=$((failures+1))
    fi
else
    echo "FAIL: scenario 4 — could not build mutant"
    failures=$((failures+1))
fi

# ─── Summary ──
if [[ $failures -eq 0 ]]; then
    echo ""
    echo "All 4 scenarios passed (3 behavioral on the real function + 1 mutation proof)."
    exit 0
else
    echo ""
    echo "$failures scenario(s) FAILED"
    exit 1
fi
