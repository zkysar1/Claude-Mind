#!/usr/bin/env bash
# domain-leak-exempt: framework recovery infra; stub names are core script literals
#
# Pins the g-357-51 hung-autocompact false-positive fix pair (2026-09-02):
#
#   FIX 2 — recovery-gate.sh `_check_hung_autocompact` roundtrip-completed
#   suppressor: an execution-diary write NEWER than the compact-in-flight
#   sentinel proves the autocompact roundtrip COMPLETED (the sentinel is
#   written only at PreCompact), so Path C must consume the sentinel and
#   suppress instead of demoting a live loop. Measured 2026-09-01T07:01:34
#   (staging deployment): a rate-limited loop resumed after compact, wrote
#   diary entries for ~an hour, then went quiet under provider backoff —
#   heartbeat stale, diary stale >15min, sentinel >60min — and Path C yanked
#   the LIVE loop to IDLE off a sentinel the loop had outlived by hours.
#
#   FIX 1 — session-save-id.sh source=compact consume block: a sentinel whose
#   CONTENT equals THIS SessionStart's SID is cleared REGARDLESS of age (we
#   ARE its resume event; rate-limit backoff stretches legitimate roundtrips
#   past any fixed window). The <10min age window survives only for
#   foreign/unreadable-SID sentinels.
#
# HOW THIS TESTS THE REAL THING. Same harness shape as
# test_recovery_gate_assistant_turn.sh: recovery-gate.sh has no main guard,
# so sourcing it whole would run Paths A/B/C/D against the LIVE agent. The
# harness awk-extracts the shipped `_check_hung_autocompact` text and sources
# just that, stubbing its dependencies plus agent_dir and _perform_recovery;
# the session-save-id.sh consume block is awk-extracted between its comment
# marker and its `unset` terminator and run with env fixtures. Assertions
# therefore run the REAL shipped branch ordering, not a re-implementation of
# the predicate in the test (guard-920).
#
# Scenarios:
#   A1. sentinel >60min, diary NEWER but stale (>15min) -> NO recovery AND
#       the sentinel is CONSUMED and the self-heal is audit-logged. The fix.
#   A2. sentinel >60min, diary OLDER than sentinel -> recovery FIRES. The
#       genuine-hang path must survive; a suppressor that always suppresses
#       is a deletion of Path C.
#   A3. sentinel >60min, NO diary at all -> recovery FIRES. Absence of a
#       diary is not evidence the roundtrip completed.
#   A4. diary FRESH (<15min) -> NO recovery via the PRE-EXISTING freshness
#       suppressor, and the sentinel is NOT consumed — pins that the new
#       block sits AFTER the freshness suppressor rather than shadowing it.
#   A5. MUTATION PROOF: neuter the new condition (`if [[ -f "$diary" ...` ->
#       `if false`) and confirm A1's fixture flips to RECOVERED=1 — i.e.
#       these assertions can actually go red. Anchored on the condition line
#       by its full stable text; a stale anchor fails LOUDLY ("expected
#       exactly 1 neutered site"), never silently.
#   B1. sentinel content == SID, aged 200min -> REMOVED. The FIX 1 case.
#   B2. sentinel content is a FOREIGN SID, aged 200min -> KEPT (Path C's
#       genuine-hang evidence must not be destroyed by an unrelated resume).
#   B3. sentinel content is a FOREIGN SID, aged 2min -> REMOVED (the
#       original just-resumed heuristic is intact).
#   B4. SID empty + sentinel content empty, aged 200min -> KEPT. Pins the
#       `[ -n "$SID" ]` guard: without it, empty-matches-empty would clear
#       genuine hang evidence on any unbound SessionStart.
#   B5. MUTATION PROOF: neuter the SID-match branch and confirm B1's fixture
#       stays PRESENT — the branch, not the age fallback, does the clearing.
#
# Run: bash core/scripts/tests/test_recovery_gate_stale_sentinel.sh
# Exit 0 = all pass, 1 = any failure.

set -uo pipefail
SCRIPT_DIR_SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORE_SCRIPTS="$(cd "$SCRIPT_DIR_SELF/.." && pwd)"
GATE="$CORE_SCRIPTS/recovery-gate.sh"
SAVEID="$CORE_SCRIPTS/session-save-id.sh"
TMP=$(mktemp -d)
trap "rm -rf '$TMP'" EXIT

failures=0

if ! date -u -d "5 minutes ago" +%Y >/dev/null 2>&1; then
    echo "SKIP: GNU date -d unavailable — cannot age fixtures"
    exit 0
fi

# ─── Part A harness: stub deps, source the REAL _check_hung_autocompact ────
SCRIPT_DIR="$TMP/bin"; mkdir -p "$SCRIPT_DIR"
ADIR="$TMP/agents/echo"; mkdir -p "$ADIR/session"

printf '#!/usr/bin/env bash\necho RUNNING\n' > "$SCRIPT_DIR/session-state-get.sh"
printf '#!/usr/bin/env bash\necho stale\n'   > "$SCRIPT_DIR/heartbeat-stale.sh"
printf '#!/usr/bin/env bash\nexit 1\n'       > "$SCRIPT_DIR/session-signal-exists.sh"
chmod +x "$SCRIPT_DIR"/*.sh

agent_dir() { echo "$ADIR"; }
RECOVERED=0
_perform_recovery() { RECOVERED=1; }

_extract_fn() {   # $1 = source file, $2 = dest ; optional $3 = sed mutation
    # Path C's suppressor writes its audit row through the shared
    # `_recovery_log_entry` verdict-record writer (g-357-51), so that helper
    # rides along with the function under test — the real writer, not a stub,
    # so the row SHAPE (action/path/evidence) is what is asserted below.
    # End each range at a line that is EXACTLY `}` — the helper's inline python
    # closes its dict on a line beginning `}))'`, which a bare `/^\}/` would
    # take as the function's end and hand `source` an unterminated body.
    if [[ -n "${3:-}" ]]; then
        { awk '/^_check_hung_autocompact\(\) \{/,/^\}$/' "$1"; awk '/^_recovery_log_entry\(\) \{/,/^\}$/' "$1"; } | sed "$3" > "$2"
    else
        { awk '/^_check_hung_autocompact\(\) \{/,/^\}$/' "$1"; awk '/^_recovery_log_entry\(\) \{/,/^\}$/' "$1"; } > "$2"
    fi
    [[ -s "$2" ]] && grep -q '^_recovery_log_entry() {' "$2"
}

if ! _extract_fn "$GATE" "$TMP/fn.sh"; then
    echo "FAIL: could not extract _check_hung_autocompact from recovery-gate.sh (function renamed?)"
    exit 1
fi
# shellcheck disable=SC1090
source "$TMP/fn.sh"

CIF="$ADIR/session/compact-in-flight"
DIARY="$ADIR/session/execution-diary.jsonl"
RLOG="$ADIR/session/recovery-log.jsonl"

_setup_a() {   # $1 = sentinel age (minutes), $2 = diary age (minutes) or "none"
    rm -f "$CIF" "$DIARY" "$RLOG"
    printf 'some-sid\n' > "$CIF"
    touch -d "$1 minutes ago" "$CIF"
    if [[ "$2" != "none" ]]; then
        printf '{"phase":"phase-4-execute","event":"phase_end"}\n' > "$DIARY"
        touch -d "$2 minutes ago" "$DIARY"
    fi
}

# ─── A1: diary newer than sentinel (both stale) — the fix itself ───────────
_setup_a 200 30
RECOVERED=0
_check_hung_autocompact echo >/dev/null 2>&1
if [[ "$RECOVERED" == "0" && ! -f "$CIF" ]]; then
    echo "PASS: A1 (sentinel 200min, diary 30min) — NO recovery, sentinel CONSUMED; the live-loop yank class is closed"
else
    echo "FAIL: A1 — RECOVERED=$RECOVERED sentinel_present=$([[ -f "$CIF" ]] && echo yes || echo no); a completed roundtrip still reads as a hung autocompact"
    failures=$((failures+1))
fi
if [[ -f "$RLOG" ]] && python3 - "$RLOG" <<'PY'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
ok = any(r.get("action") == "suppressed" and r.get("path") == "C"
         and isinstance(r.get("evidence"), dict) and r["evidence"].get("sentinel_consumed") is True
         and r["evidence"].get("compact_in_flight_sid") == "some-sid"
         for r in rows)
# and NEVER a row recovery_yank.py would read as a yank (action=recover or missing)
yank = any(r.get("action", "recover") == "recover" for r in rows)
sys.exit(0 if ok and not yank else 1)
PY
then
    echo "PASS: A1-audit — suppression logged as a verdict row (action=suppressed, path=C, evidence.sentinel_consumed) and NOT as a yank"
else
    echo "FAIL: A1-audit — sentinel consumed with no well-shaped audit row; the yank-vs-cleanup forensics trail is blind here: $(cat "$RLOG" 2>/dev/null | head -c 400)"
    failures=$((failures+1))
fi

# ─── A2: diary OLDER than sentinel — genuine hang must still recover ───────
_setup_a 200 300
RECOVERED=0
_check_hung_autocompact echo >/dev/null 2>&1
if [[ "$RECOVERED" == "1" ]]; then
    echo "PASS: A2 (sentinel 200min, diary 300min) — recovery FIRES; genuine-hang path intact"
else
    echo "FAIL: A2 — the suppressor swallowed a genuine hang; Path C is deleted, not narrowed"
    failures=$((failures+1))
fi

# ─── A3: no diary at all — absence is not completion evidence ──────────────
_setup_a 200 none
RECOVERED=0
_check_hung_autocompact echo >/dev/null 2>&1
if [[ "$RECOVERED" == "1" ]]; then
    echo "PASS: A3 (sentinel 200min, no diary) — recovery FIRES; a diary-less agent still recovers"
else
    echo "FAIL: A3 — a missing diary suppressed recovery; Path C is dead for agents that never wrote a diary"
    failures=$((failures+1))
fi

# ─── A4: fresh diary — pre-existing suppressor fires FIRST, keeps sentinel ─
_setup_a 200 5
RECOVERED=0
_check_hung_autocompact echo >/dev/null 2>&1
if [[ "$RECOVERED" == "0" && -f "$CIF" ]]; then
    echo "PASS: A4 (diary 5min fresh) — NO recovery via freshness suppressor, sentinel untouched; new block ordered AFTER it"
else
    echo "FAIL: A4 — RECOVERED=$RECOVERED sentinel_present=$([[ -f "$CIF" ]] && echo yes || echo no); the new block reordered or shadowed the freshness suppressor"
    failures=$((failures+1))
fi

# ─── A5: mutation proof — neuter the new condition, A1 must flip ───────────
MUT_A='s/^    if \[\[ -f "\$diary" && "\$diary" -nt "\$cif" \]\]; then$/    if false; then/'
if ! _extract_fn "$GATE" "$TMP/fn-mut.sh" "$MUT_A"; then
    echo "FAIL: A5 — could not extract function for mutation"
    failures=$((failures+1))
else
    NEUTERED=$(grep -c '^    if false; then$' "$TMP/fn-mut.sh" || true)
    if [[ "$NEUTERED" != "1" ]]; then
        echo "FAIL: A5 — expected exactly 1 neutered site, got $NEUTERED (condition line drifted; re-anchor the mutation)"
        failures=$((failures+1))
    else
        # shellcheck disable=SC1090
        source "$TMP/fn-mut.sh"
        _setup_a 200 30
        RECOVERED=0
        _check_hung_autocompact echo >/dev/null 2>&1
        if [[ "$RECOVERED" == "1" ]]; then
            echo "PASS: A5 (mutation) — neutered gate lets A1's fixture recover; the assertions can go red"
        else
            echo "FAIL: A5 — A1 passes even with the suppressor switched off; something else is suppressing and A1 proves nothing"
            failures=$((failures+1))
        fi
        # restore the unmutated function for any later use
        # shellcheck disable=SC1090
        source "$TMP/fn.sh"
    fi
fi

# ─── Part B harness: run the REAL session-save-id.sh consume block ─────────
_extract_block() {   # $1 = dest ; optional $2 = sed mutation
    if [[ -n "${2:-}" ]]; then
        awk '/# --- Clear compact-in-flight sentinels/,/^    unset _CIF _CIF_SID$/' "$SAVEID" | sed "$2" > "$1"
    else
        awk '/# --- Clear compact-in-flight sentinels/,/^    unset _CIF _CIF_SID$/' "$SAVEID" > "$1"
    fi
    [[ -s "$1" ]] && grep -q 'unset _CIF _CIF_SID' "$1"
}

if ! _extract_block "$TMP/block.sh"; then
    echo "FAIL: could not extract the compact-in-flight consume block from session-save-id.sh (markers renamed?)"
    exit 1
fi

BROOT="$TMP/broot"
BADIR="$BROOT/agents/echo/session"

_run_b() {   # $1 = SID env value, $2 = block file
    PROJECT_ROOT="$BROOT" AGENTS_PARENT_DIR="agents" SID="$1" bash "$2"
}

_setup_b() {   # $1 = sentinel content, $2 = age (minutes)
    rm -rf "$BROOT"; mkdir -p "$BADIR"
    printf '%s\n' "$1" > "$BADIR/compact-in-flight"
    touch -d "$2 minutes ago" "$BADIR/compact-in-flight"
}

# ─── B1: SID match, 200min old — cleared regardless of age ─────────────────
_setup_b "sid-mine" 200
_run_b "sid-mine" "$TMP/block.sh"
if [[ ! -f "$BADIR/compact-in-flight" ]]; then
    echo "PASS: B1 (own SID, 200min) — sentinel cleared regardless of age; slow rate-limited roundtrips no longer strand sentinels"
else
    echo "FAIL: B1 — an own-SID sentinel older than 10min survived its own resume event; Path C will later misread it as a hang"
    failures=$((failures+1))
fi

# ─── B2: foreign SID, 200min old — kept for Path C ─────────────────────────
_setup_b "sid-other" 200
_run_b "sid-mine" "$TMP/block.sh"
if [[ -f "$BADIR/compact-in-flight" ]]; then
    echo "PASS: B2 (foreign SID, 200min) — genuine-hang evidence preserved for recovery-gate Path C"
else
    echo "FAIL: B2 — an unrelated resume destroyed another agent's hung-compact evidence"
    failures=$((failures+1))
fi

# ─── B3: foreign SID, 2min old — just-resumed heuristic intact ─────────────
_setup_b "sid-other" 2
_run_b "sid-mine" "$TMP/block.sh"
if [[ ! -f "$BADIR/compact-in-flight" ]]; then
    echo "PASS: B3 (foreign SID, 2min) — the original <10min just-resumed clear is intact"
else
    echo "FAIL: B3 — the fresh-window heuristic regressed; just-completed foreign compacts leave sentinels behind"
    failures=$((failures+1))
fi

# ─── B4: empty SID + empty sentinel — the -n guard must refuse the match ───
_setup_b "" 200
_run_b "" "$TMP/block.sh"
if [[ -f "$BADIR/compact-in-flight" ]]; then
    echo "PASS: B4 (empty SID, empty sentinel, 200min) — no empty-matches-empty clear; hang evidence survives unbound SessionStarts"
else
    echo "FAIL: B4 — empty SID matched an empty/unreadable sentinel and cleared genuine hang evidence"
    failures=$((failures+1))
fi

# ─── B5: mutation proof — neuter the SID-match branch, B1 must flip ────────
MUT_B='s/^        if \[ -n "\$SID" \] && \[ "\$_CIF_SID" = "\$SID" \]; then$/        if false; then/'
if ! _extract_block "$TMP/block-mut.sh" "$MUT_B"; then
    echo "FAIL: B5 — could not extract block for mutation"
    failures=$((failures+1))
else
    NEUTERED_B=$(grep -c '^        if false; then$' "$TMP/block-mut.sh" || true)
    if [[ "$NEUTERED_B" != "1" ]]; then
        echo "FAIL: B5 — expected exactly 1 neutered site, got $NEUTERED_B (condition line drifted; re-anchor the mutation)"
        failures=$((failures+1))
    else
        _setup_b "sid-mine" 200
        _run_b "sid-mine" "$TMP/block-mut.sh"
        if [[ -f "$BADIR/compact-in-flight" ]]; then
            echo "PASS: B5 (mutation) — with the SID-match branch off, B1's sentinel survives; the branch, not the age fallback, does the clearing"
        else
            echo "FAIL: B5 — B1's sentinel cleared even with the SID-match branch neutered; B1 proves nothing about the new branch"
            failures=$((failures+1))
        fi
    fi
fi

echo ""
if [[ "$failures" -eq 0 ]]; then
    echo "ALL PASS: 11/11 assertions (A1 A1-audit A2 A3 A4 A5 B1 B2 B3 B4 B5)"
    exit 0
else
    echo "FAILURES: $failures"
    exit 1
fi
