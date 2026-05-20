#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Read team state — daemon-aware wrapper.
# Usage: bash core/scripts/team-state-read.sh [--field <path>] [--json]
#
# Migrated for Phase B PR 4. Daemon path: rt_call /v1/team-state/read.
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

declare -a PASSTHROUGH=()
FIELD=""
AS_JSON=0

# Value-arg pattern: "${2-}" + safe shift; see retrieve.sh for rationale.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --field) FIELD="${2-}"; PASSTHROUGH+=(--field "${2-}"); shift $(( $# >= 2 ? 2 : 1 ));;
        --json)  AS_JSON=1; PASSTHROUGH+=("$1"); shift;;
        *)
            PASSTHROUGH+=("$1"); shift;;
    esac
done

source "$CORE_ROOT/scripts/_runtime.sh"

QUERY=""
[ -n "$FIELD" ] && QUERY="field=$(rt_url_encode "$FIELD")"
[ "$AS_JSON" = "1" ] && { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="json=1"; }

rc=0
if [ -n "$QUERY" ]; then
    rt_call GET /v1/team-state/read --query "$QUERY" || rc=$?
else
    rt_call GET /v1/team-state/read || rc=$?
fi

case $rc in
    0) exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            if [ -n "$QUERY" ]; then
                rt_call GET /v1/team-state/read --query "$QUERY" || rc=$?
            else
                rt_call GET /v1/team-state/read || rc=$?
            fi
            if [ "$rc" = "0" ]; then exit 0; fi
        fi
        rt_no_daemon_error "team-state-read.sh";;
    *)
        exit $rc;;
esac
