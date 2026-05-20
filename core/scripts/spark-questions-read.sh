#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Read spark-question records — daemon-aware wrapper.
#
# Migrated for Phase B PR 4. Daemon path: rt_call /v1/spark-questions/read.
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

declare -a FLAG_KEYS=()
declare -a PASSTHROUGH=()
REC_ID=""

# Value-arg pattern: "${2-}" + safe shift; see retrieve.sh for rationale.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --id)
            REC_ID="${2-}"; PASSTHROUGH+=(--id "${2-}"); shift $(( $# >= 2 ? 2 : 1 ));;
        --active)     FLAG_KEYS+=(active);     PASSTHROUGH+=("$1"); shift;;
        --candidates) FLAG_KEYS+=(candidates); PASSTHROUGH+=("$1"); shift;;
        --all)        FLAG_KEYS+=(all);        PASSTHROUGH+=("$1"); shift;;
        --summary)    FLAG_KEYS+=(summary);    PASSTHROUGH+=("$1"); shift;;
        *)
            PASSTHROUGH+=("$1"); shift;;
    esac
done

source "$CORE_ROOT/scripts/_runtime.sh"

QUERY=""
[ -n "$REC_ID" ] && QUERY="id=$(rt_url_encode "$REC_ID")"
for key in "${FLAG_KEYS[@]+"${FLAG_KEYS[@]}"}"; do
    [ -n "$QUERY" ] && QUERY+="&"
    QUERY+="${key}=1"
done

if [ -z "$QUERY" ]; then
    echo "Error: at least one filter is required." >&2
    exit 1
fi
rc=0
rt_call GET /v1/spark-questions/read --query "$QUERY" || rc=$?

case $rc in
    0) exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            rt_call GET /v1/spark-questions/read --query "$QUERY" || rc=$?
            if [ "$rc" = "0" ]; then exit 0; fi
        fi
        rt_no_daemon_error "spark-questions-read.sh";;
    *)
        exit $rc;;
esac
