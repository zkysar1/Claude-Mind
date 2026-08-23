#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# utilization-stats — daemon-aware wrapper. Read-only utilization reporting
# for guardrails, reasoning bank, and rules.
#
# Daemon paths (all GET, verbatim body — no translation):
#   guardrails report       -> GET /v1/utilization/guardrails/report
#   guardrails candidates   -> GET /v1/utilization/guardrails/candidates?limit=N
#   rb report               -> GET /v1/utilization/rb/report
#   rb candidates           -> GET /v1/utilization/rb/candidates?limit=N
#   rules audit             -> GET /v1/utilization/rules/audit
#
# Usage:
#   bash core/scripts/utilization-stats.sh guardrails report
#   bash core/scripts/utilization-stats.sh guardrails candidates --limit 15
#   bash core/scripts/utilization-stats.sh rb report
#   bash core/scripts/utilization-stats.sh rb candidates --limit 15
#   bash core/scripts/utilization-stats.sh rules audit
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse positional subcommands + flags ------------------------------------
KIND=""
ACTION=""
LIMIT=""

# First two positional args are KIND and ACTION.
POSITIONAL=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --limit) LIMIT="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        -*)      shift;;
        *)
            if [ "$POSITIONAL" = "0" ]; then
                KIND="$1"; POSITIONAL=1
            elif [ "$POSITIONAL" = "1" ]; then
                ACTION="$1"; POSITIONAL=2
            fi
            shift;;
    esac
done

if [ -z "$KIND" ] || [ -z "$ACTION" ]; then
    echo "Usage: utilization-stats.sh {guardrails|rb|rules} {report|candidates|audit} [--limit N]" >&2
    exit 1
fi

# --- Build route + query -----------------------------------------------------
ROUTE=""
QUERY=""

case "$KIND" in
    guardrails)
        case "$ACTION" in
            report)     ROUTE="/v1/utilization/guardrails/report";;
            candidates) ROUTE="/v1/utilization/guardrails/candidates";;
            *) echo "Error: unknown action '$ACTION' for guardrails (expected report|candidates)" >&2; exit 1;;
        esac
        ;;
    rb)
        case "$ACTION" in
            report)     ROUTE="/v1/utilization/rb/report";;
            candidates) ROUTE="/v1/utilization/rb/candidates";;
            *) echo "Error: unknown action '$ACTION' for rb (expected report|candidates)" >&2; exit 1;;
        esac
        ;;
    rules)
        case "$ACTION" in
            audit) ROUTE="/v1/utilization/rules/audit";;
            *) echo "Error: unknown action '$ACTION' for rules (expected audit)" >&2; exit 1;;
        esac
        ;;
    *) echo "Error: unknown kind '$KIND' (expected guardrails|rb|rules)" >&2; exit 1;;
esac

# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

if [ -n "$LIMIT" ]; then
    QUERY="limit=$(rt_url_encode "$LIMIT")"
fi

# --- Daemon call --------------------------------------------------------------
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
        rt_no_daemon_error "utilization-stats.sh";;
    *) exit $rc;;
esac
