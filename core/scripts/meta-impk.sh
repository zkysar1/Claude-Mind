#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# meta-impk.sh — Compute or snapshot improvement velocity (imp@k).
# Daemon path:
#   compute  -> GET  /v1/meta/impk/compute
#   snapshot -> POST /v1/meta/impk/snapshot
# Both endpoints return CLI-byte-identical stdout (verbatim).
#
# Usage:
#   meta-impk.sh compute --window <k> --metric <name>
#   meta-impk.sh snapshot --goal-id <id> --learning-value <v> [--category <c>] [--active-changes <csv>]
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# Normalize --goal/--goal-id flag aliases → --goal-id (rewrites $@). SSOT for
# the dual-accept goal-id contract; verify-learning enforces that this wrapper
# sources the normalizer (12-wrapper coverage grep). Runs before the subcommand
# parse — leaves the compute/snapshot positional in place, only translates the
# goal flag. The snapshot parser below therefore only needs to know --goal-id.
GOAL_NORMALIZE_TARGET=--goal-id source "$CORE_ROOT/scripts/_goal-arg-normalize.sh"

# --- Parse subcommand --------------------------------------------------------
SUBCMD="${1-}"
if [ -z "$SUBCMD" ]; then
    echo "Error: subcommand required (compute|snapshot)." >&2
    exit 1
fi
shift

case "$SUBCMD" in
    compute)
        WINDOW=""; METRIC=""
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --window) WINDOW="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
                --metric) METRIC="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
                *) shift;;
            esac
        done

        # shellcheck disable=SC1091
        source "$CORE_ROOT/scripts/_runtime.sh"

        QUERY=""
        _append_q() { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="$1"; }
        [ -n "$WINDOW" ] && _append_q "window=$(rt_url_encode "$WINDOW")"
        [ -n "$METRIC" ] && _append_q "metric=$(rt_url_encode "$METRIC")"

        rc=0
        rt_call GET /v1/meta/impk/compute --query "$QUERY" || rc=$?
        case $rc in
            0) exit 0;;
            2) exit 1;;
            3)
                # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
                if rt_try_autospawn; then
                    rc=0
                    rt_call GET /v1/meta/impk/compute --query "$QUERY" || rc=$?
                    if [ "$rc" = "0" ]; then exit 0; fi
                fi
                rt_no_daemon_error "meta-impk.sh";;
            *) exit $rc;;
        esac
        ;;
    snapshot)
        GOAL_ID=""; LEARNING_VALUE=""; CATEGORY=""; ACTIVE_CHANGES=""
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --goal-id) GOAL_ID="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;  # --goal aliased by normalizer above
                --learning-value) LEARNING_VALUE="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
                --category)       CATEGORY="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
                --active-changes) ACTIVE_CHANGES="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
                *) shift;;
            esac
        done

        # shellcheck disable=SC1091
        source "$CORE_ROOT/scripts/_runtime.sh"

        QUERY=""
        _append_q() { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="$1"; }
        [ -n "$GOAL_ID" ]        && _append_q "goal_id=$(rt_url_encode "$GOAL_ID")"
        [ -n "$LEARNING_VALUE" ] && _append_q "learning_value=$(rt_url_encode "$LEARNING_VALUE")"
        [ -n "$CATEGORY" ]       && _append_q "category=$(rt_url_encode "$CATEGORY")"
        [ -n "$ACTIVE_CHANGES" ] && _append_q "active_changes=$(rt_url_encode "$ACTIVE_CHANGES")"

        rc=0
        rt_call POST /v1/meta/impk/snapshot --query "$QUERY" || rc=$?
        case $rc in
            0) exit 0;;
            2) exit 1;;
            3)
                # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
                if rt_try_autospawn; then
                    rc=0
                    rt_call POST /v1/meta/impk/snapshot --query "$QUERY" || rc=$?
                    if [ "$rc" = "0" ]; then exit 0; fi
                fi
                rt_no_daemon_error "meta-impk.sh";;
            *) exit $rc;;
        esac
        ;;
    *)
        echo "Error: unknown subcommand '$SUBCMD' (expected compute|snapshot)." >&2
        exit 1
        ;;
esac
