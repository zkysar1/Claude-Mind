#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Read recent changelog entries — daemon-aware wrapper. Daemon path:
# rt_call GET /v1/changelog/read. The endpoint prints byte-compat-to-CLI
# stdout, so the response body is emitted verbatim (no translation).
# Usage: bash core/scripts/changelog-read.sh [--since <dur>] [--agent <name>] [--last <N>] [--file <substr>] [--json]
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# Value-arg pattern: "${2-}" + safe shift; see _runtime.sh / tree-read.sh.
SINCE=""; AGENT_FILTER=""; LAST=""; FILE_FILTER=""; JSON=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --since) SINCE="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --agent) AGENT_FILTER="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --last)  LAST="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --file)  FILE_FILTER="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --json)  JSON=1; shift;;
        *) shift;;
    esac
done

# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY=""
_append_q() { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="$1"; }
[ -n "$SINCE" ]        && _append_q "since=$(rt_url_encode "$SINCE")"
[ -n "$AGENT_FILTER" ] && _append_q "agent=$(rt_url_encode "$AGENT_FILTER")"
[ -n "$LAST" ]         && _append_q "last=$(rt_url_encode "$LAST")"
[ -n "$FILE_FILTER" ]  && _append_q "file=$(rt_url_encode "$FILE_FILTER")"
[ "$JSON" = "1" ]      && _append_q "json=1"

rc=0
rt_call GET /v1/changelog/read --query "$QUERY" || rc=$?
case $rc in
    0) exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            rt_call GET /v1/changelog/read --query "$QUERY" || rc=$?
            if [ "$rc" = "0" ]; then exit 0; fi
        fi
        rt_no_daemon_error "changelog-read.sh";;
    *) exit $rc;;
esac
