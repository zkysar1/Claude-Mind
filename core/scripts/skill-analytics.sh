#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Skill analytics and reporting — daemon-aware wrapper. Dispatches
# subcommands to their respective daemon GET routes:
#   reuse-report     -> GET /v1/skill-analytics/reuse-report
#   co-invocation    -> GET /v1/skill-analytics/co-invocation
#   coverage         -> GET /v1/skill-analytics/coverage
#   recommendations  -> GET /v1/skill-analytics/recommendations
#   trend            -> GET /v1/skill-analytics/trend?window=N
#   usage-report     -> GET /v1/skill-analytics/usage-report?window=N
#
# All endpoints return byte-compat JSON (indent=2, ensure_ascii=False),
# so the response body is emitted verbatim (no translation).
#
# Usage: bash core/scripts/skill-analytics.sh <subcommand> [--window N]
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

CMD="${1:-reuse-report}"
shift 2>/dev/null || true

# --- Parse per-subcommand args -----------------------------------------------
WINDOW=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --window) WINDOW="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        *) shift;;
    esac
done

# --- Map subcommand to route -------------------------------------------------
case "$CMD" in
    reuse-report)     ROUTE="/v1/skill-analytics/reuse-report";;
    co-invocation)    ROUTE="/v1/skill-analytics/co-invocation";;
    coverage)         ROUTE="/v1/skill-analytics/coverage";;
    recommendations)  ROUTE="/v1/skill-analytics/recommendations";;
    trend)            ROUTE="/v1/skill-analytics/trend";;
    usage-report)     ROUTE="/v1/skill-analytics/usage-report";;
    *)
        echo "Error: unknown subcommand '$CMD'. Expected one of: reuse-report, co-invocation, coverage, recommendations, trend, usage-report" >&2
        exit 1;;
esac

# --- Daemon path --------------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY=""
_append_q() { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="$1"; }

# trend and usage-report accept --window
if { [ "$CMD" = "trend" ] || [ "$CMD" = "usage-report" ]; } && [ -n "$WINDOW" ]; then
    _append_q "window=$(rt_url_encode "$WINDOW")"
fi

rc=0
rt_call GET "$ROUTE" --query "$QUERY" || rc=$?

case $rc in
    0) exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            rt_call GET "$ROUTE" --query "$QUERY" || rc=$?
            if [ "$rc" = "0" ]; then exit 0; fi
        fi
        rt_no_daemon_error "skill-analytics.sh";;
    *) exit $rc;;
esac
