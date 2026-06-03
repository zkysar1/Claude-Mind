#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# meta-experiment.sh — A/B experiment lifecycle management (daemon-aware wrapper).
#
# Daemon paths:
#   POST /v1/meta/experiment/create   {strategy,field,baseline,variant}
#   GET  /v1/meta/experiment/status    ?id=<exp-id>
#   POST /v1/meta/experiment/resolve   {id}
#   GET  /v1/meta/experiment/list      ?completed=1
#
# All four endpoints return Response.text with byte-compat-to-CLI JSON,
# so the response body is emitted verbatim (no translation).
#
# Usage:
#   meta-experiment.sh create --strategy <file> --field <dotpath> --baseline <v> --variant <v>
#   meta-experiment.sh status [--id <exp-id>]
#   meta-experiment.sh resolve --id <exp-id>
#   meta-experiment.sh list [--active|--completed]
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse subcommand + args ------------------------------------------------
CMD="${1-}"
if [ -z "$CMD" ]; then
    echo "Usage: meta-experiment.sh {create|status|resolve|list} [options]" >&2
    exit 1
fi
shift

# Per-subcommand arg parsing
STRATEGY=""; FIELD=""; BASELINE=""; VARIANT=""
EXP_ID=""
COMPLETED=0

case "$CMD" in
    create)
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --strategy)  STRATEGY="${2-}";  shift $(( $# >= 2 ? 2 : 1 ));;
                --field)     FIELD="${2-}";     shift $(( $# >= 2 ? 2 : 1 ));;
                --baseline)  BASELINE="${2-}";  shift $(( $# >= 2 ? 2 : 1 ));;
                --variant)   VARIANT="${2-}";   shift $(( $# >= 2 ? 2 : 1 ));;
                *) shift;;
            esac
        done
        ;;
    status)
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --id) EXP_ID="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
                *) shift;;
            esac
        done
        ;;
    resolve)
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --id) EXP_ID="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
                *) shift;;
            esac
        done
        ;;
    list)
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --completed) COMPLETED=1; shift;;
                --active)    COMPLETED=0; shift;;
                *) shift;;
            esac
        done
        ;;
    *)
        echo "Unknown subcommand: $CMD" >&2
        echo "Usage: meta-experiment.sh {create|status|resolve|list} [options]" >&2
        exit 1
        ;;
esac

# --- Daemon path ------------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

_do_call() {
    case "$CMD" in
        create)
            local BODY
            BODY=$($(rt_python_launcher) -c "
import json, sys
print(json.dumps({
    'strategy': sys.argv[1],
    'field': sys.argv[2],
    'baseline': sys.argv[3],
    'variant': sys.argv[4]
}))
" "$STRATEGY" "$FIELD" "$BASELINE" "$VARIANT")
            rt_call POST /v1/meta/experiment/create --body-string "$BODY"
            ;;
        status)
            local QUERY=""
            [ -n "$EXP_ID" ] && QUERY="id=$(rt_url_encode "$EXP_ID")"
            rt_call GET /v1/meta/experiment/status --query "$QUERY"
            ;;
        resolve)
            local BODY
            BODY=$($(rt_python_launcher) -c "
import json, sys
print(json.dumps({'id': sys.argv[1]}))
" "$EXP_ID")
            rt_call POST /v1/meta/experiment/resolve --body-string "$BODY"
            ;;
        list)
            local QUERY=""
            [ "$COMPLETED" = "1" ] && QUERY="completed=1"
            rt_call GET /v1/meta/experiment/list --query "$QUERY"
            ;;
    esac
}

rc=0
_do_call || rc=$?

case $rc in
    0) exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            _do_call || rc=$?
            if [ "$rc" = "0" ]; then exit 0; fi
        fi
        rt_no_daemon_error "meta-experiment.sh";;
    *) exit $rc;;
esac
