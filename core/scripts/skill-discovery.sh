#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Skill discoverability measurement — daemon-aware wrapper.
# Multi-subcommand: report | flagged | score
#
# Daemon paths:
#   GET /v1/skill-discovery/report
#   GET /v1/skill-discovery/flagged?action_required_only=1
#   GET /v1/skill-discovery/score?skill=<name>
#
# All endpoints return byte-compat JSON (indent=2, ensure_ascii=False),
# so the response body is emitted verbatim (no translation).
#
# Usage:
#   bash core/scripts/skill-discovery.sh [report]
#   bash core/scripts/skill-discovery.sh flagged [--action-required-only]
#   bash core/scripts/skill-discovery.sh score <skill-name>
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse subcommand + args ----------------------------------------------
CMD="${1:-report}"
shift 2>/dev/null || true

ACTION_REQUIRED_ONLY=0
SKILL=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --action-required-only) ACTION_REQUIRED_ONLY=1; shift;;
        *) SKILL="${SKILL:-$1}"; shift;;
    esac
done

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

_do_call() {
    local route="$1"
    local query="$2"
    local rc=0
    rt_call GET "$route" --query "$query" || rc=$?
    case $rc in
        0) exit 0;;
        2) exit 1;;
        3)
            # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
            if rt_try_autospawn; then
                rc=0
                rt_call GET "$route" --query "$query" || rc=$?
                if [ "$rc" = "0" ]; then exit 0; fi
            fi
            rt_no_daemon_error "skill-discovery.sh";;
        *) exit $rc;;
    esac
}

case "$CMD" in
    report)
        _do_call /v1/skill-discovery/report ""
        ;;
    flagged)
        QUERY=""
        if [ "$ACTION_REQUIRED_ONLY" = "1" ]; then
            QUERY="action_required_only=1"
        fi
        _do_call /v1/skill-discovery/flagged "$QUERY"
        ;;
    score)
        if [ -z "$SKILL" ]; then
            echo "Error: score subcommand requires a skill name." >&2
            exit 1
        fi
        QUERY="skill=$(rt_url_encode "$SKILL")"
        _do_call /v1/skill-discovery/score "$QUERY"
        ;;
    *)
        echo "Error: unknown subcommand '$CMD'. Use: report | flagged | score" >&2
        exit 1
        ;;
esac
