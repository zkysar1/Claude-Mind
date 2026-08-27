#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# meta-backpressure.sh — Backpressure gate for meta-strategy changes.
# Daemon path: rt_call to /v1/meta/backpressure/* (7 subcommands).
# The endpoint returns byte-compat-to-CLI stdout (JSON), so the response
# body is emitted verbatim (no translation needed).
#
# Usage:
#   meta-backpressure.sh monitor --change-id <id> --file <f> --field <dp> --old <v> --new <v> --baseline <imp_k>
#   meta-backpressure.sh check --learning-value <v>
#   meta-backpressure.sh graduate --change-id <id>
#   meta-backpressure.sh status
#   meta-backpressure.sh cooldown-check [--window <N>]
#   meta-backpressure.sh evolution-monitor --monitor-kind <k> --revision-id <id> --file-path <p> --history-snapshot <s> --baseline-vector <json> [--agent <a>]
#   meta-backpressure.sh evolution-check
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse subcommand + args ------------------------------------------------
SUBCMD="${1-}"
[ -z "$SUBCMD" ] && { echo "Error: subcommand required (monitor|check|graduate|status|cooldown-check|evolution-monitor|evolution-check)" >&2; exit 1; }
shift

# Per-subcommand arg parsing
CHANGE_ID=""; FILE=""; FIELD=""; OLD=""; NEW=""; BASELINE=""
LEARNING_VALUE=""
WINDOW=""
MONITOR_KIND=""; REVISION_ID=""; FILE_PATH=""; AGENT=""; HISTORY_SNAPSHOT=""; BASELINE_VECTOR=""

# Value-arg pattern: "${2-}" + safe shift; see _runtime.sh / tree-read.sh.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --change-id)        CHANGE_ID="${2-}";        shift $(( $# >= 2 ? 2 : 1 ));;
        --file)             FILE="${2-}";             shift $(( $# >= 2 ? 2 : 1 ));;
        --field)            FIELD="${2-}";            shift $(( $# >= 2 ? 2 : 1 ));;
        --old)              OLD="${2-}";              shift $(( $# >= 2 ? 2 : 1 ));;
        --new)              NEW="${2-}";              shift $(( $# >= 2 ? 2 : 1 ));;
        --baseline)         BASELINE="${2-}";         shift $(( $# >= 2 ? 2 : 1 ));;
        --learning-value)   LEARNING_VALUE="${2-}";   shift $(( $# >= 2 ? 2 : 1 ));;
        --window)           WINDOW="${2-}";           shift $(( $# >= 2 ? 2 : 1 ));;
        --monitor-kind)     MONITOR_KIND="${2-}";     shift $(( $# >= 2 ? 2 : 1 ));;
        --revision-id)      REVISION_ID="${2-}";      shift $(( $# >= 2 ? 2 : 1 ));;
        --file-path)        FILE_PATH="${2-}";        shift $(( $# >= 2 ? 2 : 1 ));;
        --agent)            AGENT="${2-}";            shift $(( $# >= 2 ? 2 : 1 ));;
        --history-snapshot) HISTORY_SNAPSHOT="${2-}";  shift $(( $# >= 2 ? 2 : 1 ));;
        --baseline-vector)  BASELINE_VECTOR="${2-}";  shift $(( $# >= 2 ? 2 : 1 ));;
        *) shift;;
    esac
done

# --- Daemon path ------------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

_do_call() {
    local rc=0
    local METHOD="$1"
    local ROUTE="$2"
    shift 2

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
            rt_no_daemon_error "meta-backpressure.sh";;
        *) exit $rc;;
    esac
}

case "$SUBCMD" in
    monitor)
        BODY=$($(rt_python_launcher) -c "
import json, sys
print(json.dumps({
    'change_id': sys.argv[1],
    'file': sys.argv[2],
    'field': sys.argv[3],
    'old': sys.argv[4],
    'new': sys.argv[5],
    'baseline': sys.argv[6],
}))
" "$CHANGE_ID" "$FILE" "$FIELD" "$OLD" "$NEW" "$BASELINE")
        _do_call POST /v1/meta/backpressure/monitor --body-string "$BODY"
        ;;
    check)
        BODY=$($(rt_python_launcher) -c "
import json, sys
print(json.dumps({'learning_value': sys.argv[1]}))
" "$LEARNING_VALUE")
        _do_call POST /v1/meta/backpressure/check --body-string "$BODY"
        ;;
    graduate)
        BODY=$($(rt_python_launcher) -c "
import json, sys
print(json.dumps({'change_id': sys.argv[1]}))
" "$CHANGE_ID")
        _do_call POST /v1/meta/backpressure/graduate --body-string "$BODY"
        ;;
    status)
        _do_call GET /v1/meta/backpressure/status --query ""
        ;;
    cooldown-check)
        QUERY=""
        [ -n "$WINDOW" ] && QUERY="window=$(rt_url_encode "$WINDOW")"
        _do_call GET /v1/meta/backpressure/cooldown-check --query "$QUERY"
        ;;
    evolution-monitor)
        BODY=$($(rt_python_launcher) -c "
import json, sys
d = {
    'monitor_kind': sys.argv[1],
    'revision_id': sys.argv[2],
    'file_path': sys.argv[3],
    'history_snapshot': sys.argv[4],
    'baseline_vector': sys.argv[5],
}
if sys.argv[6]:
    d['agent'] = sys.argv[6]
print(json.dumps(d))
" "$MONITOR_KIND" "$REVISION_ID" "$FILE_PATH" "$HISTORY_SNAPSHOT" "$BASELINE_VECTOR" "$AGENT")
        _do_call POST /v1/meta/backpressure/evolution-monitor --body-string "$BODY"
        ;;
    evolution-check)
        _do_call POST /v1/meta/backpressure/evolution-check --body-string "{}"
        ;;
    *)
        echo "Error: unknown subcommand '$SUBCMD'. Expected: monitor|check|graduate|status|cooldown-check|evolution-monitor|evolution-check" >&2
        exit 1
        ;;
esac
