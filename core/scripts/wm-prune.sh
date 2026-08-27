#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# wm-prune.sh — mid-session working-memory pruning. Daemon path:
# rt_call POST /v1/wm/prune. The endpoint returns {ok, dry_run, report};
# CLI printed only the report dict, so _translate extracts .report.
#
# Cadence-tracker protection: the daemon endpoint (wm_write.py) lifts
# CADENCE_TRACKER_PATTERNS verbatim from wm.py — matching slots are NEVER
# evicted. See  (2026-04-21).
#
# Usage: bash core/scripts/wm-prune.sh [--dry-run]
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
DRY_RUN=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift;;
        *) shift;;
    esac
done

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY=""
_append_q() { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="$1"; }
[ "$DRY_RUN" = "1" ] && _append_q "dry_run=1"

_translate() {
    # CLI printed json.dumps(report) — extract .report from daemon JSON response.
    # shellcheck disable=SC2086
    printf '%s' "$1" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
print(json.dumps(resp.get('report', {}), ensure_ascii=False, default=str))
"
}

rc=0
RESPONSE="$(rt_call POST /v1/wm/prune --query "$QUERY")" || rc=$?

case $rc in
    0) _translate "$RESPONSE"; exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/wm/prune --query "$QUERY")" || rc=$?
            if [ "$rc" = "0" ]; then _translate "$RESPONSE"; exit 0; fi
        fi
        rt_no_daemon_error "wm-prune.sh";;
    *) exit $rc;;
esac
