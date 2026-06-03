#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Skill relation graph operations — daemon-aware wrapper.
#
# Subcommands:
#   read      — GET  /v1/skill-relations/read       [--skill X] [--type X] [--composable X] [--similar X]
#   add       — POST /v1/skill-relations/add         (JSON from stdin)
#   co-invoke — POST /v1/skill-relations/co-invoke   --goal X --skills X
#   discover  — GET  /v1/skill-relations/discover
#
# All endpoints return Response.text (byte-compat with CLI stdout); the
# rt_call body is emitted verbatim — no translation needed.
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

CMD="${1:-read}"
shift 2>/dev/null || true

# --- Subcommand: read --------------------------------------------------------
_do_read() {
    local SKILL="" TYPE="" COMPOSABLE="" SIMILAR=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --skill)      SKILL="${2-}";      shift $(( $# >= 2 ? 2 : 1 ));;
            --type)       TYPE="${2-}";       shift $(( $# >= 2 ? 2 : 1 ));;
            --composable) COMPOSABLE="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
            --similar)    SIMILAR="${2-}";    shift $(( $# >= 2 ? 2 : 1 ));;
            *) shift;;
        esac
    done

    # shellcheck disable=SC1091
    source "$CORE_ROOT/scripts/_runtime.sh"

    local QUERY=""
    _append_q() { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="$1"; }
    [ -n "$SKILL" ]      && _append_q "skill=$(rt_url_encode "$SKILL")"
    [ -n "$TYPE" ]       && _append_q "type=$(rt_url_encode "$TYPE")"
    [ -n "$COMPOSABLE" ] && _append_q "composable=$(rt_url_encode "$COMPOSABLE")"
    [ -n "$SIMILAR" ]    && _append_q "similar=$(rt_url_encode "$SIMILAR")"

    local rc=0
    rt_call GET /v1/skill-relations/read --query "$QUERY" || rc=$?
    case $rc in
        0) exit 0;;
        2) exit 1;;
        3)
            if rt_try_autospawn; then
                rc=0
                rt_call GET /v1/skill-relations/read --query "$QUERY" || rc=$?
                if [ "$rc" = "0" ]; then exit 0; fi
            fi
            rt_no_daemon_error "skill-relations.sh";;
        *) exit $rc;;
    esac
}

# --- Subcommand: add ---------------------------------------------------------
_do_add() {
    local BODY
    BODY="$(cat)"

    # shellcheck disable=SC1091
    source "$CORE_ROOT/scripts/_runtime.sh"

    local rc=0
    rt_call POST /v1/skill-relations/add --body-string "$BODY" || rc=$?
    case $rc in
        0) exit 0;;
        2) exit 1;;
        3)
            if rt_try_autospawn; then
                rc=0
                rt_call POST /v1/skill-relations/add --body-string "$BODY" || rc=$?
                if [ "$rc" = "0" ]; then exit 0; fi
            fi
            rt_no_daemon_error "skill-relations.sh";;
        *) exit $rc;;
    esac
}

# --- Subcommand: co-invoke ---------------------------------------------------
_do_co_invoke() {
    local GOAL="" SKILLS=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --goal)   GOAL="${2-}";   shift $(( $# >= 2 ? 2 : 1 ));;
            --skills) SKILLS="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
            *) shift;;
        esac
    done

    # shellcheck disable=SC1091
    source "$CORE_ROOT/scripts/_runtime.sh"

    local QUERY=""
    _append_q() { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="$1"; }
    [ -n "$GOAL" ]   && _append_q "goal=$(rt_url_encode "$GOAL")"
    [ -n "$SKILLS" ] && _append_q "skills=$(rt_url_encode "$SKILLS")"

    local rc=0
    rt_call POST /v1/skill-relations/co-invoke --query "$QUERY" || rc=$?
    case $rc in
        0) exit 0;;
        2) exit 1;;
        3)
            if rt_try_autospawn; then
                rc=0
                rt_call POST /v1/skill-relations/co-invoke --query "$QUERY" || rc=$?
                if [ "$rc" = "0" ]; then exit 0; fi
            fi
            rt_no_daemon_error "skill-relations.sh";;
        *) exit $rc;;
    esac
}

# --- Subcommand: discover ----------------------------------------------------
_do_discover() {
    # shellcheck disable=SC1091
    source "$CORE_ROOT/scripts/_runtime.sh"

    local rc=0
    rt_call GET /v1/skill-relations/discover || rc=$?
    case $rc in
        0) exit 0;;
        2) exit 1;;
        3)
            if rt_try_autospawn; then
                rc=0
                rt_call GET /v1/skill-relations/discover || rc=$?
                if [ "$rc" = "0" ]; then exit 0; fi
            fi
            rt_no_daemon_error "skill-relations.sh";;
        *) exit $rc;;
    esac
}

# --- Dispatch ----------------------------------------------------------------
case "$CMD" in
    read)      _do_read "$@";;
    add)       _do_add "$@";;
    co-invoke) _do_co_invoke "$@";;
    discover)  _do_discover "$@";;
    *)
        echo "Error: unknown subcommand '$CMD'. Use: read, add, co-invoke, discover" >&2
        exit 1;;
esac
