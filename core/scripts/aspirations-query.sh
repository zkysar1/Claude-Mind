#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Targeted goal query — searches both world and agent queues, returns matching goals.
# Lightweight alternative to loading the full aspirations-compact.json into context.
#
# Migrated for Phase B PR 6. Daemon path: rt_call /v1/aspirations/query.
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

declare -a PASSTHROUGH=()
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
            PASSTHROUGH+=("$1" "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --title-contains)
            TITLE_CONTAINS="${2-}"
            PASSTHROUGH+=("$1" "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --goal-field)
            GOAL_FIELD_NAME="${2-}"
            GOAL_FIELD_VALUE="${3-}"
            PASSTHROUGH+=("$1" "${2-}" "${3-}")
            shift $(( $# >= 3 ? 3 : ($# >= 2 ? 2 : 1) ));;
        --full)
            # Boolean flag (no value): full-record read mode ().
            # Translated to the full=true query param after the filter check below,
            # so --full alone (no filter) still hits the "filter required" error.
            FULL=1; shift;;
        *)
            PASSTHROUGH+=("$1"); shift;;
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
