#!/usr/bin/env bash
# assert-capture-complete — fail LOUDLY when a redirected capture is empty,
# truncated, or missing its producer's terminal line.
#
# WHY THIS EXISTS. A capture that lands empty or short with rc=0 is the worst
# shape a failure can take: it is indistinguishable from a legitimate result, so
# it produces a CONFIDENT WRONG ANSWER downstream rather than an error. Four
# measured instances across two producers and three boxes:
#   - rb-5684: the fleet's cloud-CLI wrapper returns 0 bytes with rc=0 whenever
#     stdout is a regular FILE (full output via pipe or $()), output-format
#     independent, reproduced on two boxes. A 0-byte side made a config diff
#     report total divergence, which read as a dramatic finding.
#   - : a long-running framework writer's redirect stopped mid-stream
#     with no NUL bytes and no error, silently eating the loop's own terminal
#     imperative; separately a ~2.8 MB stdout landed as a zero-byte file.
# rb-5684 states the general rule and did not build the helper: "when a capture
# can be empty without erroring, assert non-empty at the CAPTURE SITE rather
# than trusting the consumer to notice." This is that assertion.
#
# WHY AT THE CAPTURE SITE AND NOT IN THE CONSUMER. The consumer usually cannot
# tell the two apart — `wc -l`, `grep -c` and `|| echo 0` all render a broken
# capture as a confident zero (guard-2298). Only the caller knows what the
# producer was supposed to emit, so only the caller can assert it.
#
# THIS DELIBERATELY DOES NOT FAIL OPEN. Every other advisory in this tree exits
# 0 on error so a broken check cannot stop real work. An assertion whose whole
# job is refusing a silent-empty must not itself go silent: a fail-open
# completeness check would pass on exactly the runs it exists to catch.
#
# Usage:
#   assert-capture-complete.sh <file> [--expect-terminal <regex>]
#                                     [--min-bytes <n>] [--allow-nul] [--quiet]
#
# Exit codes (distinct on purpose — the three failures need different fixes):
#   0  capture looks complete
#   1  file MISSING            (the redirect never created it)
#   2  file EMPTY or under --min-bytes  (the silent-empty class)
#   3  terminal line ABSENT    (partial write — producer ran past what landed)
#   4  NUL bytes present       (the log-corruption signature; see
#                               .claude/rules/run-full-suite-after-deep-code.md)
#   64 usage error
set -uo pipefail

usage() {
    cat >&2 <<'EOF'
usage: assert-capture-complete.sh <file> [options]
  --expect-terminal <regex>  last non-blank line MUST match this (extended regex)
  --min-bytes <n>            fail when the file is smaller than n bytes (default 1)
  --allow-nul                do not treat NUL bytes as corruption
  --quiet                    print nothing on success
Exit: 0 ok | 1 missing | 2 empty/short | 3 terminal-absent | 4 NUL | 64 usage
EOF
}

FILE=""; EXPECT=""; EXPECT_SET=0; MIN_BYTES=1; ALLOW_NUL=0; QUIET=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --expect-terminal) EXPECT="${2:-}"; EXPECT_SET=1; shift $(( $# >= 2 ? 2 : 1 ));;
        --min-bytes)       MIN_BYTES="${2:-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --allow-nul)       ALLOW_NUL=1; shift;;
        --quiet)           QUIET=1; shift;;
        -h|--help)         usage; exit 64;;
        --*)               echo "assert-capture-complete: unknown option '$1'" >&2; usage; exit 64;;
        *)                 if [[ -z "$FILE" ]]; then FILE="$1"; else
                               echo "assert-capture-complete: unexpected argument '$1'" >&2; exit 64
                           fi; shift;;
    esac
done

[[ -n "$FILE" ]] || { echo "assert-capture-complete: <file> is required" >&2; usage; exit 64; }
[[ "$MIN_BYTES" =~ ^[0-9]+$ ]] || { echo "assert-capture-complete: --min-bytes must be an integer, got '$MIN_BYTES'" >&2; exit 64; }

# An EMPTY --expect-terminal is a USAGE ERROR, never a skipped check.
# The `shift $(( $# >= 2 ? 2 : 1 ))` above is guard-1224's prescribed form and it
# correctly prevents the infinite loop on a trailing valueless flag — but it only
# guards the SHIFT. `${2:-}` then leaves EXPECT="", and the `[[ -n "$EXPECT" ]]`
# test below reads that as "caller did not ask for a terminal check", so
# `... --expect-terminal` (value lost to a typo, or to a variable that expanded
# empty) returned rc=0 OK having asserted NOTHING.
#
# That is precisely the silent-pass class this script's header refuses to
# participate in, reproduced inside the script itself — and it was ASYMMETRIC:
# the sibling --min-bytes caught its own missing value one line above and exited
# 64, so one valueless flag was loud and the other silent. It is also guard-3893
# exactly: a flag the script ACCEPTS but does not WIRE parses cleanly, changes
# nothing, and leaves the caller believing they bought coverage they did not.
# guard-1224's own incident records the intended outcome for a trailing valueless
# flag as "exit 2 (clean usage error)", not a silent accept.
#
# Distinguish ABSENT from EMPTY: EXPECT_SET is 1 only when the flag appeared, so
# omitting --expect-terminal entirely stays a legitimate no-terminal-check run.
if (( EXPECT_SET == 1 )) && [[ -z "$EXPECT" ]]; then
    echo "assert-capture-complete: --expect-terminal requires a non-empty regex" >&2
    echo "  (an empty pattern would silently skip the terminal check — the exact" >&2
    echo "   silent-pass this script exists to refuse; guard-1224, guard-3893)" >&2
    exit 64
fi

if [[ ! -e "$FILE" ]]; then
    echo "[assert-capture] MISSING: $FILE was never created — the redirect did not run, or ran to a different path." >&2
    exit 1
fi

# -e admits directories, fifos and devices. `wc -c < <dir>` fails, BYTES lands
# empty, bash arithmetic reads empty as 0, and the run reports "EMPTY: 0 bytes
# with no error from the producer" — a confident MISDIAGNOSIS of a wrong-target
# mistake as the silent-empty class, pointing the reader at the producer instead
# of at their own path argument.
if [[ ! -f "$FILE" ]]; then
    echo "[assert-capture] NOT A REGULAR FILE: $FILE exists but is not a file (directory, fifo or device)." >&2
    echo "  This is a bad target argument, not a failed capture — check the path you passed." >&2
    exit 64
fi

BYTES=$(wc -c < "$FILE" | tr -d '[:space:]')

# Report bytes on EVERY failure. A verdict without the byte count is exactly the
# shape that makes a broken capture read as a real zero (guard-2298).
if (( BYTES < MIN_BYTES )); then
    if (( BYTES == 0 )); then
        echo "[assert-capture] EMPTY: $FILE is 0 bytes with no error from the producer." >&2
        echo "[assert-capture]   This is the silent-empty class (rb-5684). Capture via a pipe or \$( ) and" >&2
        echo "[assert-capture]   write the file from that, rather than redirecting the producer straight to it." >&2
    else
        echo "[assert-capture] SHORT: $FILE is $BYTES bytes, under the required $MIN_BYTES." >&2
    fi
    exit 2
fi

if (( ALLOW_NUL == 0 )); then
    NULS=$(tr -dc '\0' < "$FILE" | wc -c | tr -d '[:space:]')
    if (( NULS > 0 )); then
        echo "[assert-capture] NUL BYTES: $FILE carries $NULS NUL byte(s) in $BYTES bytes." >&2
        echo "[assert-capture]   That is log corruption, not a short run — re-capture to a different path." >&2
        exit 4
    fi
fi

if [[ -n "$EXPECT" ]]; then
    LAST=$(grep -v '^[[:space:]]*$' "$FILE" 2>/dev/null | tail -1)
    if ! printf '%s' "$LAST" | grep -Eq -- "$EXPECT"; then
        echo "[assert-capture] TERMINAL LINE ABSENT: $FILE is $BYTES bytes and ends cleanly, but its last" >&2
        echo "[assert-capture]   non-blank line does not match /$EXPECT/." >&2
        echo "[assert-capture]   last line: ${LAST:0:160}" >&2
        echo "[assert-capture]   A partial write with no NULs is indistinguishable from a short run, so it" >&2
        echo "[assert-capture]   fails as a plausible COMPLETE reading unless asserted (guard-1760 class)." >&2
        exit 3
    fi
fi

(( QUIET == 1 )) || echo "[assert-capture] OK: $FILE ($BYTES bytes${EXPECT:+, terminal line matched})"
exit 0
