#!/usr/bin/env bash
# goal-field-append — append text to ONE goal TEXT field, idempotently.
#
#   goal-field-append.sh [--source world|agent] <goal-id> <field> <marker> [<text>]
#                        [--value-file <path> | --value-stdin]
#
# The eight per-store append helpers this repo already has (wm-append,
# journal-append, evolution-log-append, decision-rules-append,
# health-ledger-append, meta-log-append, mind-append) all exist because their
# store's SET wrapper replaces rather than appends. Goal fields had no such
# helper, so every annotate-an-existing-record write was a hand-rolled
# read-modify-write (zeta rolled the same one four times in one session).
#
# THIS IS DELIBERATELY A SCRIPT, NOT AN --append FLAG on
# aspirations-update-goal.sh. That wrapper hand-rolls its parser and ends in a
# silent `-*) PASSTHROUGH+=("$1")` arm, so an unrecognized flag is dropped and
# the next token is promoted into the value slot — rc=0, full record echoed,
# field destroyed (guard-2460 / guard-1047 / guard-1488). guard-2525 says
# "never pass --append" for exactly that reason. A distinct script name cannot
# be swallowed by a PASSTHROUGH arm.
#
# Strict argv (guard-1047's measured discriminator: this wrapper DOES source
# _argv_strict.sh, so an unknown flag is refused with exit 2 rather than
# silently sliding a path into the value slot). --value-file / --value-stdin
# carry long text safely.
#
# Rationale, refusals, and the verification contract: goal-field-append.py.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"

# --source is extracted BEFORE the strict parse (argv_strict_parse refuses any
# flag it does not know). Everything else stays strict.
SOURCE_VAL="world"
declare -a REST=()
while [ $# -gt 0 ]; do
    case "$1" in
        --source)     SOURCE_VAL="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --source=*)   SOURCE_VAL="${1#--source=}"; shift;;
        *)            REST+=("$1"); shift;;
    esac
done

source "$CORE_ROOT/scripts/_argv_strict.sh"
argv_strict_parse "goal-field-append.sh" 4 "${REST[@]+"${REST[@]}"}"

GOAL_ID="${ARGV_POS[0]-}"
FIELD="${ARGV_POS[1]-}"
MARKER="${ARGV_POS[2]-}"

if [ -z "$GOAL_ID" ] || [ -z "$FIELD" ] || [ -z "$MARKER" ]; then
    echo "Usage: goal-field-append.sh [--source world|agent] <goal-id> <field> <marker> [<text>]" >&2
    echo "       (or supply the text via --value-file <path> / --value-stdin)" >&2
    exit 2
fi

TEXT="$(argv_strict_resolve_value "goal-field-append.sh" "${ARGV_POS[3]-}")"

exec python3 "$CORE_ROOT/scripts/goal-field-append.py" \
    --source "$SOURCE_VAL" "$GOAL_ID" "$FIELD" "$MARKER" "$TEXT"
