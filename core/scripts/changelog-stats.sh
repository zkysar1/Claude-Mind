#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Show changelog statistics — daemon-aware wrapper. Daemon path:
# rt_call GET /v1/changelog/stats. The endpoint prints byte-compat-to-CLI
# stdout, so the response body is emitted verbatim (no translation).
# Usage: bash core/scripts/changelog-stats.sh [--since <duration>]
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# Value-arg pattern: "${2-}" + safe shift; see _runtime.sh / tree-read.sh.
SINCE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --since) SINCE="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        *) shift;;
    esac
done

# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY=""
[ -n "$SINCE" ] && QUERY="since=$(rt_url_encode "$SINCE")"

rc=0
rt_call GET /v1/changelog/stats --query "$QUERY" || rc=$?
case $rc in
    0) exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            rt_call GET /v1/changelog/stats --query "$QUERY" || rc=$?
            if [ "$rc" = "0" ]; then exit 0; fi
        fi
        rt_no_daemon_error "changelog-stats.sh";;
    *) exit $rc;;
esac
