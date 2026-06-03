#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# meta-generations.sh — Strategy generation tracking (daemon-aware wrapper).
#
# Subcommands → daemon routes:
#   snapshot                          GET  /v1/meta/generation/snapshot
#   close [--metrics '<json>']        POST /v1/meta/generation/close
#   open                              POST /v1/meta/generation/open
#   update --learning-value <v>       POST /v1/meta/generation/update
#   status                            GET  /v1/meta/generation/status
#   history [--top N]                 GET  /v1/meta/generation/history
#
# All endpoints return Response.text with byte-compatible CLI output;
# the response body is emitted verbatim (no translation).
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
CMD="${1-}"
shift 2>/dev/null || true

METRICS=""; LEARNING_VALUE=""; TOP=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --metrics)        METRICS="${2-}";        shift $(( $# >= 2 ? 2 : 1 ));;
        --learning-value) LEARNING_VALUE="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --top)            TOP="${2-}";            shift $(( $# >= 2 ? 2 : 1 ));;
        *) shift;;
    esac
done

if [ -z "$CMD" ]; then
    echo "Usage: meta-generations.sh {snapshot|close|open|update|status|history}" >&2
    exit 1
fi

# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

# --- Dispatch subcommands -------------------------------------------------

_daemon_call() {
    local METHOD="$1" ROUTE="$2"
    shift 2
    local rc=0
    rt_call "$METHOD" "$ROUTE" "$@" || rc=$?
    case $rc in
        0) exit 0;;
        2) exit 1;;
        3)
            # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
            if rt_try_autospawn; then
                rc=0
                rt_call "$METHOD" "$ROUTE" "$@" || rc=$?
                if [ "$rc" = "0" ]; then exit 0; fi
            fi
            rt_no_daemon_error "meta-generations.sh";;
        *) exit $rc;;
    esac
}

case "$CMD" in
    snapshot)
        _daemon_call GET /v1/meta/generation/snapshot
        ;;
    close)
        BODY="{}"
        if [ -n "$METRICS" ]; then
            BODY="{\"metrics\":$METRICS}"
        fi
        _daemon_call POST /v1/meta/generation/close --body-string "$BODY"
        ;;
    open)
        _daemon_call POST /v1/meta/generation/open --body-string "{}"
        ;;
    update)
        if [ -z "$LEARNING_VALUE" ]; then
            echo "Error: --learning-value is required for update" >&2
            exit 1
        fi
        BODY="{\"learning_value\":$LEARNING_VALUE}"
        _daemon_call POST /v1/meta/generation/update --body-string "$BODY"
        ;;
    status)
        _daemon_call GET /v1/meta/generation/status
        ;;
    history)
        QUERY=""
        if [ -n "$TOP" ]; then
            QUERY="top=$(rt_url_encode "$TOP")"
        fi
        _daemon_call GET /v1/meta/generation/history --query "$QUERY"
        ;;
    *)
        echo "Error: unknown subcommand '$CMD'. Expected: snapshot|close|open|update|status|history" >&2
        exit 1
        ;;
esac
