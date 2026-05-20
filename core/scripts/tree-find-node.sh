#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Find tree nodes matching a text query — daemon-aware wrapper.
#
# Migrated for Phase B. Hot path: rt_call /v1/tree/find-node.
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve -------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
TEXT=""
TOP=""
LEAF=0
declare -a PASSTHROUGH=()

# Value-arg pattern: "${2-}" + safe shift handle the case where the user
# passes a flag with no following value. See retrieve.sh for the rationale.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --text|--find)
            TEXT="${2-}"
            # Fallback path uses --find; preserve that name for argparse.
            PASSTHROUGH+=(--find "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --top)
            TOP="${2-}"
            PASSTHROUGH+=(--top "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --leaf-only)
            LEAF=1
            PASSTHROUGH+=("$1")
            shift;;
        *)
            # Unknown flag passes through to the fallback so argparse can
            # surface a canonical error.
            PASSTHROUGH+=("$1")
            shift;;
    esac
done

# --- Daemon path ----------------------------------------------------------
source "$CORE_ROOT/scripts/_runtime.sh"

if [ -z "$TEXT" ]; then
    echo "Error: --text or --find argument is required." >&2
    exit 1
else
    QUERY="text=$(rt_url_encode "$TEXT")"
    [ -n "$TOP" ] && QUERY+="&top=${TOP}"
    [ "$LEAF" = "1" ] && QUERY+="&leaf_only=1"

    rc=0
    rt_call GET /v1/tree/find-node --query "$QUERY" || rc=$?
fi

case $rc in
    0) exit 0;;
    2)
        # Daemon answered with HTTP 4xx/5xx; body already on stderr.
        exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            rt_call GET /v1/tree/find-node --query "$QUERY" || rc=$?
            if [ "$rc" = "0" ]; then exit 0; fi
        fi
        rt_no_daemon_error "tree-find-node.sh";;
    *)
        exit $rc;;
esac
