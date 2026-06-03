#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# meta-dead-ends.sh — Dead end registry for meta-strategy approaches.
# Daemon routes:
#   POST /v1/meta/dead-ends/add        (stdin JSON body)
#   GET  /v1/meta/dead-ends/check      (?file=&field=&value=)
#   GET  /v1/meta/dead-ends/read       (?active=&category=)
#   POST /v1/meta/dead-ends/increment  (?id=)
#   POST /v1/meta/dead-ends/review     (?id=)
#
# Usage:
#   echo '<json>' | meta-dead-ends.sh add
#   meta-dead-ends.sh check --file <f> --field <dp> --value <v>
#   meta-dead-ends.sh read [--active] [--category <cat>]
#   meta-dead-ends.sh increment <id>
#   meta-dead-ends.sh review <id>
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse subcommand + args ------------------------------------------------
CMD=""
FILE_ARG=""; FIELD_ARG=""; VALUE_ARG=""
ACTIVE=0; CATEGORY=""
REC_ID=""
BODY=""

if [[ $# -gt 0 ]]; then
    CMD="$1"; shift
fi

case "$CMD" in
    add)
        # Stdin JSON body — capture BEFORE sourcing _runtime.sh.
        BODY="$(cat)"
        ;;
    check)
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --file)  FILE_ARG="${2-}";  shift $(( $# >= 2 ? 2 : 1 ));;
                --field) FIELD_ARG="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
                --value) VALUE_ARG="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
                *) shift;;
            esac
        done
        ;;
    read)
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --active)   ACTIVE=1; shift;;
                --category) CATEGORY="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
                *) shift;;
            esac
        done
        ;;
    increment)
        REC_ID="${1-}"; shift $(( $# >= 1 ? 1 : 0 ))
        ;;
    review)
        REC_ID="${1-}"; shift $(( $# >= 1 ? 1 : 0 ))
        ;;
    *)
        echo "Usage: meta-dead-ends.sh {add|check|read|increment|review} [args]" >&2
        exit 1
        ;;
esac

# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

# --- Build query string per subcommand --------------------------------------
QUERY=""
_append_q() { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="$1"; }

case "$CMD" in
    check)
        [ -n "$FILE_ARG" ]  && _append_q "file=$(rt_url_encode "$FILE_ARG")"
        [ -n "$FIELD_ARG" ] && _append_q "field=$(rt_url_encode "$FIELD_ARG")"
        [ -n "$VALUE_ARG" ] && _append_q "value=$(rt_url_encode "$VALUE_ARG")"
        ;;
    read)
        [ "$ACTIVE" = "1" ] && _append_q "active=1"
        [ -n "$CATEGORY" ]  && _append_q "category=$(rt_url_encode "$CATEGORY")"
        ;;
    increment)
        [ -n "$REC_ID" ] && _append_q "id=$(rt_url_encode "$REC_ID")"
        ;;
    review)
        [ -n "$REC_ID" ] && _append_q "id=$(rt_url_encode "$REC_ID")"
        ;;
esac

# --- Dispatch per subcommand ------------------------------------------------

_call_add() {
    rc=0
    rt_call POST /v1/meta/dead-ends/add --body-string "$BODY" || rc=$?
    case $rc in
        0) exit 0;;
        2) exit 1;;
        3)
            if rt_try_autospawn; then
                rc=0
                rt_call POST /v1/meta/dead-ends/add --body-string "$BODY" || rc=$?
                if [ "$rc" = "0" ]; then exit 0; fi
            fi
            rt_no_daemon_error "meta-dead-ends.sh";;
        *) exit $rc;;
    esac
}

_call_get() {
    local route="$1"
    rc=0
    rt_call GET "$route" --query "$QUERY" || rc=$?
    case $rc in
        0) exit 0;;
        2) exit 1;;
        3)
            if rt_try_autospawn; then
                rc=0
                rt_call GET "$route" --query "$QUERY" || rc=$?
                if [ "$rc" = "0" ]; then exit 0; fi
            fi
            rt_no_daemon_error "meta-dead-ends.sh";;
        *) exit $rc;;
    esac
}

_call_post_query() {
    local route="$1"
    rc=0
    rt_call POST "$route" --query "$QUERY" || rc=$?
    case $rc in
        0) exit 0;;
        2) exit 1;;
        3)
            if rt_try_autospawn; then
                rc=0
                rt_call POST "$route" --query "$QUERY" || rc=$?
                if [ "$rc" = "0" ]; then exit 0; fi
            fi
            rt_no_daemon_error "meta-dead-ends.sh";;
        *) exit $rc;;
    esac
}

case "$CMD" in
    add)       _call_add;;
    check)     _call_get  /v1/meta/dead-ends/check;;
    read)      _call_get  /v1/meta/dead-ends/read;;
    increment) _call_post_query /v1/meta/dead-ends/increment;;
    review)    _call_post_query /v1/meta/dead-ends/review;;
esac
