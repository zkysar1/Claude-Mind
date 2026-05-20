#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# aspirations-clear-stale-claims — daemon-aware wrapper (PR 50).
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Parse --source / --dry-run flags
#   3. POST /v1/aspirations/clear-stale-claims with source & dry_run as query params
#   4. On 200, print "cleared N records" + per-goal IDs to stdout
#
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
SOURCE_VAL="world"
DRY_RUN_VAL="false"
declare -a PASSTHROUGH_SOURCE=()
declare -a PASSTHROUGH_DRY_RUN=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source)
            SOURCE_VAL="${2-}"
            PASSTHROUGH_SOURCE=(--source "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --dry-run)
            DRY_RUN_VAL="true"
            PASSTHROUGH_DRY_RUN=(--dry-run)
            shift;;
        *) shift;;
    esac
done

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY="source=${SOURCE_VAL}&dry_run=${DRY_RUN_VAL}"

rc=0
RESPONSE="$(rt_call POST /v1/aspirations/clear-stale-claims \
    --query "$QUERY")" || rc=$?

case $rc in
    0)
        # 200: parse response. Print "cleared N records" + per-goal IDs
        # (matches legacy CLI output shape).
        # shellcheck disable=SC2086
        printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
dry = resp.get('dry_run', False)
prefix = 'would clear' if dry else 'cleared'
cleared = resp.get('cleared_ids', [])
print(f'{prefix} {len(cleared)} records')
for gid in cleared:
    print(f'  {gid}')
"
        exit 0;;
    2)
        # Daemon answered 4xx/5xx; body already written to stderr by rt_curl.
        exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/aspirations/clear-stale-claims \
                --query "$QUERY")" || rc=$?
            if [ "$rc" = "0" ]; then
                # shellcheck disable=SC2086
                printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
dry = resp.get('dry_run', False)
prefix = 'would clear' if dry else 'cleared'
cleared = resp.get('cleared_ids', [])
print(f'{prefix} {len(cleared)} records')
for gid in cleared:
    print(f'  {gid}')
"
                exit 0
            fi
        fi
        rt_no_daemon_error "aspirations-clear-stale-claims.sh";;
    *)
        exit $rc;;
esac
