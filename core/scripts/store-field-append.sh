#!/usr/bin/env bash
# store-field-append — append text to ONE governed-store TEXT field, idempotently.
#
#   store-field-append.sh --store guardrails|reasoning-bank|pipeline <id> <field> <marker> [<text>]
#                         [--value-file <path> | --value-stdin] [--anchor <text>]
#
# The store-side sibling of goal-field-append.sh (gap-106, ).
# guardrails-update-field.sh and reasoning-bank-update-field.sh take a WHOLE-FIELD
# write, so amending a record is a read-modify-write with no guard of its own —
# hand-rolled four times in one session (guard-1710/guard-2598 under ;
# guard-2908/guard-991 under ). Without the marker a retry
# DOUBLE-APPENDS; without --anchor the append lands on a record another agent has
# since rewritten. Both are invisible at write time.
#
# THIS IS DELIBERATELY A SCRIPT, NOT AN --append FLAG on the two update-field
# wrappers. Those refuse unknown leading-dash args with exit 2 today, but the
# pre-strict versions slid the next token into the VALUE slot and clobbered
# guard-1615 (times_active 677, 1400+ chars -> an 87-char path) at rc=0
# (). guard-2525 says "never pass --append" for exactly that class. A
# distinct script NAME cannot be swallowed by a passthrough arm.
#
# Rationale, refusals, the --anchor drift guard, and the verification contract:
# store-field-append.py. The four pure helpers are IMPORTED from
# goal-field-append.py rather than re-typed, so the safety invariants cannot fork.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"

# --store and --anchor are extracted BEFORE the strict parse, the same shape
# goal-field-append.sh uses for --source (argv_strict_parse refuses any flag it
# does not know). Everything else stays strict.
STORE_VAL=""
ANCHOR_VAL=""
HAVE_ANCHOR=0
declare -a REST=()
while [ $# -gt 0 ]; do
    case "$1" in
        --store)      STORE_VAL="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --store=*)    STORE_VAL="${1#--store=}"; shift;;
        --anchor)     ANCHOR_VAL="${2-}"; HAVE_ANCHOR=1; shift $(( $# >= 2 ? 2 : 1 ));;
        --anchor=*)   ANCHOR_VAL="${1#--anchor=}"; HAVE_ANCHOR=1; shift;;
        *)            REST+=("$1"); shift;;
    esac
done

source "$CORE_ROOT/scripts/_argv_strict.sh"
argv_strict_parse "store-field-append.sh" 4 "${REST[@]+"${REST[@]}"}"

RECORD_ID="${ARGV_POS[0]-}"
FIELD="${ARGV_POS[1]-}"
MARKER="${ARGV_POS[2]-}"

# ONE literal, referenced by the usage line, the refusal text, AND the
# membership test below — the idiom pipeline-update-field.sh adopted at
# fresh-eyes F-002, where two strings that had to agree were asserted to be one
# and simply were not. This was THREE copies until pipeline joined the map on
# 2026-09-15 (), and the .py's `choices=tuple(STORES)` is a fourth
# that derives itself from the map. A store present in the map but absent here
# is accepted by the .py and refused by this wrapper at exit 2 — a drift that
# nothing tested, and that reads as "the store is not supported" rather than as
# a wiring bug. test_store_field_append.py::test_shell_wrapper_store_list_matches_the_map
# now parses this line and fails when it disagrees with STORES.
_STORES="guardrails|reasoning-bank|pipeline"

usage() {
    echo "Usage: store-field-append.sh --store $_STORES <id> <field> <marker> [<text>]" >&2
    echo "       (or supply the text via --value-file <path> / --value-stdin)" >&2
    echo "       [--anchor <text>]  refuse unless <text> is present in the CURRENT value" >&2
    echo "       <marker> is a BARE token — this script wraps it as [appended:<marker>]." >&2
    echo "                Passing the wrapped form is REFUSED (exit 2): it would write a" >&2
    echo "                doubled sentinel and break the idempotency key." >&2
}

# --store is required and is checked HERE rather than only in the .py, so a
# missing store costs no interpreter start and cannot be confused with a
# read/transport failure.
# The membership test is delimiter-wrapped rather than a `case "$STORE_VAL" in
# $_STORES)` alternation: bash does NOT treat a `|` that arrives via parameter
# expansion as a case alternation — measured 2026-09-15, all four of
# guardrails/reasoning-bank/pipeline/bogus fell through to the default arm, so
# that form would have refused EVERY store while looking correct.
if [ -z "$STORE_VAL" ]; then
    echo "ERROR: --store is required" >&2; usage; exit 2
fi
case "|$_STORES|" in
    *"|$STORE_VAL|"*) ;;
    *) echo "ERROR: unknown --store '$STORE_VAL' (expected $_STORES)" >&2; exit 2 ;;
esac

if [ -z "$RECORD_ID" ] || [ -z "$FIELD" ] || [ -z "$MARKER" ]; then
    usage
    exit 2
fi

TEXT="$(argv_strict_resolve_value "store-field-append.sh" "${ARGV_POS[3]-}")"

if [ "$HAVE_ANCHOR" = "1" ]; then
    exec python3 "$CORE_ROOT/scripts/store-field-append.py" \
        --store "$STORE_VAL" --anchor "$ANCHOR_VAL" "$RECORD_ID" "$FIELD" "$MARKER" "$TEXT"
else
    exec python3 "$CORE_ROOT/scripts/store-field-append.py" \
        --store "$STORE_VAL" "$RECORD_ID" "$FIELD" "$MARKER" "$TEXT"
fi
