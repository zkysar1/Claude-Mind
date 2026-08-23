#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Targeted goal query — searches both world and agent queues, returns matching goals.
# Lightweight alternative to loading the full aspirations-compact.json into context.
#
# UNION-ONLY BY DESIGN — THERE IS NO --source FLAG ().
# This wrapper ALWAYS returns the union of the world and agent queues. The
# endpoint builds `sources` from both stores unconditionally and 404s only if
# NEITHER exists (mind_api/src/endpoints/aspirations_query.py). To scope a
# result to one queue, filter CLIENT-SIDE on the per-row `source` key that every
# row already carries (guard-2588) — do not reach for a flag.
#   WHY THIS IS SPELLED OUT: `--source` was accepted and silently discarded for
#   as long as this wrapper existed, so `--source world` and `--source agent`
#   returned BYTE-IDENTICAL output (measured 2026-08-07: md5
#   2e0542d676104b01abe8b33f75affd7d, 15543388 bytes, both invocations). A
#   caller that ran both and SUMMED them double-counted every number — that
#   published 456 fleet closes against a true 228, and both halves looked
#   internally consistent. The `-*)` refusal below is what makes the mistake
#   loud instead of plausible (guard-2986).
#
# Migrated for Phase B PR 6. Daemon path: rt_call /v1/aspirations/query.
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# Shared unknown-flag refusal (). Sourced BEFORE _runtime.sh so the
# refusal is cheap and cannot be masked by a daemon failure.
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_argv_strict.sh"
# ONE literal, referenced by BOTH the --help arm and the refusal (
# fresh-eyes F-002) — two strings that must agree is the drift surface the
# refusal exists to remove.
_ACCEPTED_FLAGS="--goal-status <status> | --goal-field <name> <value> | --title-contains <substr> | --full"

GOAL_STATUS=""
GOAL_FIELD_NAME=""
GOAL_FIELD_VALUE=""
TITLE_CONTAINS=""
FULL=0

# Value-arg pattern: "${2-}" + safe shift; see retrieve.sh for rationale.
# --goal-field takes TWO values, so it gets a three-tier shift guard (same
# as --child-path in tree-read.sh).
while [[ $# -gt 0 ]]; do
    case "$1" in
        --goal-status)
            GOAL_STATUS="${2-}"
            argv_strict_refuse_flaglike_value "$(basename "$0")" --goal-status \
                "$GOAL_STATUS" "$_ACCEPTED_FLAGS"
            shift $(( $# >= 2 ? 2 : 1 ));;
        --title-contains)
            TITLE_CONTAINS="${2-}"
            argv_strict_refuse_flaglike_value "$(basename "$0")" --title-contains \
                "$TITLE_CONTAINS" "$_ACCEPTED_FLAGS"
            shift $(( $# >= 2 ? 2 : 1 ));;
        --goal-field)
            GOAL_FIELD_NAME="${2-}"
            GOAL_FIELD_VALUE="${3-}"
            # BOTH slots, not just the value: `--goal-field --full pending` eats the
            # flag as the NAME and is just as silent as eating it as the value.
            argv_strict_refuse_flaglike_value "$(basename "$0")" '--goal-field <name>' \
                "$GOAL_FIELD_NAME" "$_ACCEPTED_FLAGS"
            argv_strict_refuse_flaglike_value "$(basename "$0")" '--goal-field <value>' \
                "$GOAL_FIELD_VALUE" "$_ACCEPTED_FLAGS"
            shift $(( $# >= 3 ? 3 : ($# >= 2 ? 2 : 1) ));;
        --full)
            # Boolean flag (no value): full-record read mode ().
            # Translated to the full=true query param after the filter check below,
            # so --full alone (no filter) still hits the "filter required" error.
            FULL=1; shift;;
        -h|--help)
            # BEFORE the -*) arm: --help is a `-*` token, and refusing it with
            # exit 2 would be a regression the refusal introduced rather than a
            # defect it fixed (). Help exits 0.
            # The `form` arg deliberately does NOT restate the flag list: this
            # wrapper has no positionals, so a spelled-out form would be a second
            # copy of $_ACCEPTED_FLAGS that must agree with it — the exact drift
            # surface the single-literal rule above exists to remove (found by
            # fresh-eyes on this goal's own diff).
            # 4th arg (): the default projection is invisible from the
            # flag list, and guessing it wrong is a MEASURED failure — a caller
            # filtered on created_at, which the projection does not emit, and
            # read the resulting nothing as "no goals". Naming the six keys here
            # is the cheap half of that fix; the loud half is the endpoint's
            # unknown_goal_field refusal.
            #
            # The `extra` slot is the right home for this and a leading printf
            # block is not, even though argv_strict_help exits 0 as its last act:
            # the helper has ALWAYS taken a 4th arg and prints it after the flag
            # list, which is where a reader looks for notes.
            argv_strict_help "$(basename "$0")" "<at least one filter> [--full]" \
                "$_ACCEPTED_FLAGS" \
"  Default projection emits SIX keys: goal_id, asp_id, source, title, status, category.
  Anything else (created_at, priority, defer_reason, claimed_by, ...) requires --full.
  --goal-field matches the RAW record, so it filters on fields the projection does
  not show; the identifier there is \`id\`, and \`goal_id\` is accepted as an alias.
  A name no record carries is REFUSED, not answered with an empty array.";;
        -*)
            # REFUSE (). Every unrecognized flag used to land in a
            # write-only PASSTHROUGH array, so the query silently answered a
            # BROADER question than the caller asked and still exited 0. Measured
            # costs of that silence: `--source` returned the union either way
            # (2x inflation in a published fleet count, guard-2986); `--asp-id`
            # returned 1539 rows where 44 matched — 35x wrong, in the direction
            # that manufactured a crisis; `--goal-title-contains` returned 942
            # rows identical to no filter at all, 157x over-broad (guard-694).
            # An over-broad answer never looks like a failure, which is why this
            # has to fail loudly rather than be documented.
            argv_strict_refuse_unknown "$(basename "$0")" "$1" "$_ACCEPTED_FLAGS";;
        *)
            # KNOWN RESIDUAL, deliberately not fixed here ( -> ).
            # This wrapper takes ZERO positionals, and a stray one is still
            # swallowed. MEASURED on this box rather than assumed:
            #   `aspirations-query.sh SOMEPOSITIONAL`            -> rc=1, loud
            #     (the filter-required check below catches it), so a positional
            #     ALONE cannot produce a wrong answer.
            #   `aspirations-query.sh --goal-status blocked EXTRA` -> rc=0, and
            #     byte-identical to the same call without EXTRA. Silently ignored.
            # That second case is the  class and `_argv_strict.sh`
            # already carries the remedy (argv_strict_refuse_extra_positional,
            # maxpos 0). It is NOT adopted here because guard-1562 requires
            # enumerating what would NEWLY fire, and the textual scan for
            # positional-passing call sites returned obvious noise (prose words,
            # `2` from `2>/dev/null`) — an unmeasured blast radius is not a
            # licence to ship a refusal into a loop-hot read path.
            shift;;
    esac
done

source "$CORE_ROOT/scripts/_runtime.sh"

QUERY=""
if [ -n "$GOAL_STATUS" ]; then
    QUERY="goal_status=$(rt_url_encode "$GOAL_STATUS")"
fi
if [ -n "$GOAL_FIELD_NAME" ]; then
    [ -n "$QUERY" ] && QUERY+="&"
    QUERY+="goal_field_name=$(rt_url_encode "$GOAL_FIELD_NAME")"
    QUERY+="&goal_field_value=$(rt_url_encode "$GOAL_FIELD_VALUE")"
fi
if [ -n "$TITLE_CONTAINS" ]; then
    [ -n "$QUERY" ] && QUERY+="&"
    QUERY+="title_contains=$(rt_url_encode "$TITLE_CONTAINS")"
fi

if [ -z "$QUERY" ]; then
    echo "Error: at least one filter is required (--goal-status, --goal-field, or --title-contains)." >&2
    exit 1
else
    # --full appends full=true ONLY when a filter is present (QUERY non-empty),
    # so --full alone falls through to the filter-required error above ().
    if [ "$FULL" = "1" ]; then QUERY+="&full=true"; fi
    rc=0
    rt_call GET /v1/aspirations/query --query "$QUERY" || rc=$?
fi

case $rc in
    0) exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            rt_call GET /v1/aspirations/query --query "$QUERY" || rc=$?
            if [ "$rc" = "0" ]; then exit 0; fi
        fi
        rt_no_daemon_error "aspirations-query.sh";;
    *)
        exit $rc;;
esac
