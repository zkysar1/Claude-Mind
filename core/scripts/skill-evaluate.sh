#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Skill quality evaluation (five dimensions) — daemon-aware wrapper.
#
# Subcommands:
#   read              GET  /v1/skill-evaluate/read
#   report            GET  /v1/skill-evaluate/report
#   underperforming   GET  /v1/skill-evaluate/underperforming
#   score             POST /v1/skill-evaluate/score
#
# Usage:
#   bash core/scripts/skill-evaluate.sh read [--skill <name>] [--all] [--summary]
#   bash core/scripts/skill-evaluate.sh report
#   bash core/scripts/skill-evaluate.sh underperforming [--threshold <float>]
#   bash core/scripts/skill-evaluate.sh score --skill <name> --goal <id> \
#       --safety good|average|poor --completeness good|average|poor \
#       --executability good|average|poor --maintainability good|average|poor \
#       --cost-awareness good|average|poor
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

CMD="${1:-read}"
shift 2>/dev/null || true

# --- Parse args per subcommand ------------------------------------------------
SKILL=""; GOAL=""; THRESHOLD=""
SAFETY=""; COMPLETENESS=""; EXECUTABILITY=""; MAINTAINABILITY=""; COST_AWARENESS=""
ALL=0; SUMMARY=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skill)            SKILL="${2-}";            shift $(( $# >= 2 ? 2 : 1 ));;
        --goal)             GOAL="${2-}";             shift $(( $# >= 2 ? 2 : 1 ));;
        --threshold)        THRESHOLD="${2-}";        shift $(( $# >= 2 ? 2 : 1 ));;
        --safety)           SAFETY="${2-}";           shift $(( $# >= 2 ? 2 : 1 ));;
        --completeness)     COMPLETENESS="${2-}";     shift $(( $# >= 2 ? 2 : 1 ));;
        --executability)    EXECUTABILITY="${2-}";    shift $(( $# >= 2 ? 2 : 1 ));;
        --maintainability)  MAINTAINABILITY="${2-}";  shift $(( $# >= 2 ? 2 : 1 ));;
        --cost-awareness)   COST_AWARENESS="${2-}";   shift $(( $# >= 2 ? 2 : 1 ));;
        --all)              ALL=1; shift;;
        --summary)          SUMMARY=1; shift;;
        *) shift;;
    esac
done

# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

# --- Route per subcommand -----------------------------------------------------
case "$CMD" in
    read)
        QUERY=""
        _append_q() { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="$1"; }
        [ -n "$SKILL" ]    && _append_q "skill=$(rt_url_encode "$SKILL")"
        [ "$ALL" = "1" ]   && _append_q "all=1"
        [ "$SUMMARY" = "1" ] && _append_q "summary=1"

        rc=0
        rt_call GET /v1/skill-evaluate/read --query "$QUERY" || rc=$?
        case $rc in
            0) exit 0;;
            2) exit 1;;
            3)
                if rt_try_autospawn; then
                    rc=0
                    rt_call GET /v1/skill-evaluate/read --query "$QUERY" || rc=$?
                    if [ "$rc" = "0" ]; then exit 0; fi
                fi
                rt_no_daemon_error "skill-evaluate.sh";;
            *) exit $rc;;
        esac
        ;;
    report)
        rc=0
        rt_call GET /v1/skill-evaluate/report || rc=$?
        case $rc in
            0) exit 0;;
            2) exit 1;;
            3)
                if rt_try_autospawn; then
                    rc=0
                    rt_call GET /v1/skill-evaluate/report || rc=$?
                    if [ "$rc" = "0" ]; then exit 0; fi
                fi
                rt_no_daemon_error "skill-evaluate.sh";;
            *) exit $rc;;
        esac
        ;;
    underperforming)
        QUERY=""
        [ -n "$THRESHOLD" ] && QUERY="threshold=$(rt_url_encode "$THRESHOLD")"

        rc=0
        rt_call GET /v1/skill-evaluate/underperforming --query "$QUERY" || rc=$?
        case $rc in
            0) exit 0;;
            2) exit 1;;
            3)
                if rt_try_autospawn; then
                    rc=0
                    rt_call GET /v1/skill-evaluate/underperforming --query "$QUERY" || rc=$?
                    if [ "$rc" = "0" ]; then exit 0; fi
                fi
                rt_no_daemon_error "skill-evaluate.sh";;
            *) exit $rc;;
        esac
        ;;
    score)
        # Build JSON body from CLI args. The endpoint accepts cost_awareness
        # (underscore) and cost-awareness (hyphen); send underscore form.
        BODY="$($(rt_python_launcher) -c "
import json, sys
body = {}
if sys.argv[1]: body['skill'] = sys.argv[1]
if sys.argv[2]: body['goal'] = sys.argv[2]
if sys.argv[3]: body['safety'] = sys.argv[3]
if sys.argv[4]: body['completeness'] = sys.argv[4]
if sys.argv[5]: body['executability'] = sys.argv[5]
if sys.argv[6]: body['maintainability'] = sys.argv[6]
if sys.argv[7]: body['cost_awareness'] = sys.argv[7]
print(json.dumps(body))
" "$SKILL" "$GOAL" "$SAFETY" "$COMPLETENESS" "$EXECUTABILITY" "$MAINTAINABILITY" "$COST_AWARENESS")"

        rc=0
        rt_call POST /v1/skill-evaluate/score --body-string "$BODY" || rc=$?
        case $rc in
            0) exit 0;;
            2) exit 1;;
            3)
                if rt_try_autospawn; then
                    rc=0
                    rt_call POST /v1/skill-evaluate/score --body-string "$BODY" || rc=$?
                    if [ "$rc" = "0" ]; then exit 0; fi
                fi
                rt_no_daemon_error "skill-evaluate.sh";;
            *) exit $rc;;
        esac
        ;;
    *)
        echo "Error: unknown subcommand '$CMD'. Use: read | report | underperforming | score" >&2
        exit 1
        ;;
esac
