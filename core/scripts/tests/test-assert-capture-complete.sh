#!/usr/bin/env bash
# test-assert-capture-complete — pins every exit code of assert-capture-complete.sh.
#
# HERMETIC BY CONSTRUCTION. Every fixture lives in a mktemp -d that is removed on
# EXIT; this test reads and writes nothing under agents/, world/ or meta/. That is
# deliberate rather than incidental — a sibling shell test in this directory seeds
# the LIVE working-memory file and restores it from a pre-run copy, which silently
# clobbers any loop write inside its window and makes the test's own result depend
# on how busy the agent is. A test for a silent-failure detector must not itself
# have a silent failure mode.
#
# Every failing case below was verified to actually FAIL before this file was
# committed (guard-3534: a test is only protection if the gate can fail — prove it
# with a forced-failure control first).
set -uo pipefail

SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/assert-capture-complete.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0

check() {  # check <label> <expected-rc> <actual-rc>
    if [[ "$2" == "$3" ]]; then
        echo "  PASS $1 (rc=$3)"; PASS=$((PASS+1))
    else
        echo "  FAIL $1: expected rc=$2, got rc=$3"; FAIL=$((FAIL+1))
    fi
}

echo "=== assert-capture-complete.sh ==="
[[ -x "$SCRIPT" || -f "$SCRIPT" ]] || { echo "FATAL: $SCRIPT not found"; exit 1; }

# --- rc=0: a complete capture -------------------------------------------------
printf 'line one\nline two\nTERMINAL\n' > "$TMP/ok.txt"
bash "$SCRIPT" "$TMP/ok.txt" --quiet; check "complete capture" 0 $?
bash "$SCRIPT" "$TMP/ok.txt" --expect-terminal 'TERMINAL' --quiet; check "terminal line matched" 0 $?

# Trailing blank lines must not defeat the terminal check — a producer whose last
# write is a newline is complete, and rejecting it would make the assertion fire
# on healthy runs, which is how a check gets disabled.
printf 'a\nTERMINAL\n\n\n' > "$TMP/ok-trailing.txt"
bash "$SCRIPT" "$TMP/ok-trailing.txt" --expect-terminal 'TERMINAL' --quiet; check "trailing blank lines tolerated" 0 $?

# --- rc=1: file missing -------------------------------------------------------
bash "$SCRIPT" "$TMP/never-created.txt" --quiet 2>/dev/null; check "missing file" 1 $?

# --- rc=2: the silent-empty class (rb-5684) -----------------------------------
: > "$TMP/empty.txt"
bash "$SCRIPT" "$TMP/empty.txt" --quiet 2>/dev/null; check "zero-byte capture" 2 $?

printf 'tiny\n' > "$TMP/short.txt"
bash "$SCRIPT" "$TMP/short.txt" --min-bytes 1000 --quiet 2>/dev/null; check "under --min-bytes" 2 $?

# --- rc=3: partial write, terminal line absent () -------------------
# The exact live shape: a clean prefix, a hard stop, NO NUL bytes, rc=0 from the
# producer. Byte count and line count both look plausible.
printf 'header\n[health-ledger] ok\n' > "$TMP/partial.txt"
bash "$SCRIPT" "$TMP/partial.txt" --expect-terminal 'ITERATION COMPLETE' --quiet 2>/dev/null
check "partial write missing terminal line" 3 $?

# The same file passes when no terminal line is demanded — proving rc=3 comes
# from the assertion and not from anything else about the fixture.
bash "$SCRIPT" "$TMP/partial.txt" --quiet; check "same file OK without --expect-terminal" 0 $?

# --- rc=4: NUL bytes (the log-corruption signature) ---------------------------
printf 'good line\n' > "$TMP/nul.txt"
printf '\0\0\0' >> "$TMP/nul.txt"
bash "$SCRIPT" "$TMP/nul.txt" --quiet 2>/dev/null; check "NUL bytes rejected" 4 $?
bash "$SCRIPT" "$TMP/nul.txt" --allow-nul --quiet 2>/dev/null; check "--allow-nul opts out" 0 $?

# --- rc=64: usage -------------------------------------------------------------
bash "$SCRIPT" --quiet 2>/dev/null; check "no file argument" 64 $?
bash "$SCRIPT" "$TMP/ok.txt" --bogus-flag 2>/dev/null; check "unknown flag refused" 64 $?
bash "$SCRIPT" "$TMP/ok.txt" --min-bytes notanumber 2>/dev/null; check "non-numeric --min-bytes refused" 64 $?

# --- precedence: emptiness is reported before a terminal-line miss ------------
# An empty file also lacks any terminal line. rc=2 is the more actionable
# diagnosis (the capture never landed), so it must win over rc=3.
: > "$TMP/empty2.txt"
bash "$SCRIPT" "$TMP/empty2.txt" --expect-terminal 'ANYTHING' --quiet 2>/dev/null
check "empty outranks terminal-absent" 2 $?

# --- the diagnosis reaches stderr, not just the exit code --------------------
ERR="$(bash "$SCRIPT" "$TMP/empty.txt" --quiet 2>&1 >/dev/null)"
if grep -q 'EMPTY' <<<"$ERR"; then
    echo "  PASS empty diagnosis names the class"; PASS=$((PASS+1))
else
    echo "  FAIL empty diagnosis missing from stderr: $ERR"; FAIL=$((FAIL+1))
fi
ERR="$(bash "$SCRIPT" "$TMP/empty.txt" --quiet 2>&1 >/dev/null)"
if grep -q '0 bytes' <<<"$ERR"; then
    echo "  PASS diagnosis carries the byte count"; PASS=$((PASS+1))
else
    echo "  FAIL byte count absent from diagnosis: $ERR"; FAIL=$((FAIL+1))
fi

# --- a valueless --expect-terminal must be LOUD, never a skipped check -------
# Found by fresh-eyes review of this script (). `shift $(( $# >= 2 ? 2
# : 1 ))` is guard-1224's prescribed form and correctly avoids the infinite loop,
# but it guards only the SHIFT: `${2:-}` left EXPECT="", and the `[[ -n "$EXPECT"
# ]]` test read that as "no terminal check requested", so this returned rc=0 OK
# having asserted nothing. Asymmetric too — the sibling --min-bytes already
# exited 64 on its own missing value. That is guard-3893 (a flag ACCEPTED but not
# WIRED) landing inside the one script whose header refuses to fail open.
bash "$SCRIPT" "$TMP/ok.txt" --expect-terminal >/dev/null 2>&1
check "valueless --expect-terminal is a usage error" 64 $?

EMPTY_PATTERN=""
bash "$SCRIPT" "$TMP/ok.txt" --expect-terminal "$EMPTY_PATTERN" >/dev/null 2>&1
check "--expect-terminal from an empty variable is a usage error" 64 $?

# The complement, and the reason the fix keys on EXPECT_SET rather than on
# emptiness alone: OMITTING the flag is a legitimate no-terminal-check run and
# must stay rc=0. Without this pin the fix above could be "tightened" into
# refusing every call that does not pass a pattern.
bash "$SCRIPT" "$TMP/ok.txt" >/dev/null 2>&1
check "omitting --expect-terminal entirely is still valid" 0 $?

# --- a non-file target is a BAD ARGUMENT, not an empty capture ---------------
# -e admits directories. `wc -c < <dir>` fails, BYTES lands empty, bash reads
# empty as 0, and the run reported "EMPTY: 0 bytes with no error from the
# producer" — blaming the producer for the caller's wrong path argument.
bash "$SCRIPT" "$TMP" >/dev/null 2>&1
check "directory target is refused as a usage error" 64 $?

ERR="$(bash "$SCRIPT" "$TMP" 2>&1 >/dev/null)"
if grep -q 'NOT A REGULAR FILE' <<<"$ERR"; then
    echo "  PASS directory diagnosis blames the argument, not the producer"; PASS=$((PASS+1))
else
    echo "  FAIL directory misdiagnosed: $ERR"; FAIL=$((FAIL+1))
fi

echo
echo "$PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
