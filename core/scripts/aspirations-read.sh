#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Layer 1 — CLIENT: agent framework. See core/BOUNDARY.md.
# Read aspirations — daemon-aware wrapper.
#
# Migrated for Phase 2. The hot path is:
#   1. Resolve PROJECT_ROOT from $0 (trivial; no _paths.sh source)
#   2. Source _runtime.sh (~1ms; no _paths.sh, no _platform.sh)
#   3. rt_call /v1/aspirations/read via curl
#
# When daemon is unreachable, auto-spawns and retries once; fails loud if
# still down.
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve -------------------------------------------
# core/scripts/aspirations-read.sh → PROJECT_ROOT is two `..`s up.
# _runtime.sh sees these via shell-source inheritance.
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
SOURCE_VAL="world"
ASP_ID=""
LIMIT=""
declare -a FLAG_KEYS=()
declare -a PASSTHROUGH=()
declare -a PASSTHROUGH_SOURCE=()

# Value-arg pattern: "${2-}" + safe shift handle the case where the user
# passes a flag with no following value (e.g., `aspirations-read.sh --source`).
# Without this, set -u trips "unbound variable" at $2 expansion; with it,
# the empty value flows through to argparse for a canonical error message.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --source)
            SOURCE_VAL="${2-}"
            PASSTHROUGH_SOURCE=(--source "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --active)            FLAG_KEYS+=(active);          PASSTHROUGH+=("$1"); shift;;
        --active-compact)    FLAG_KEYS+=(active_compact);  PASSTHROUGH+=("$1"); shift;;
        --summary)           FLAG_KEYS+=(summary);         PASSTHROUGH+=("$1"); shift;;
        --archive)           FLAG_KEYS+=(archive);         PASSTHROUGH+=("$1"); shift;;
        --stepping-stones)   FLAG_KEYS+=(stepping_stones); PASSTHROUGH+=("$1"); shift;;
        --meta)              FLAG_KEYS+=(meta);            PASSTHROUGH+=("$1"); shift;;
        --blocked)           FLAG_KEYS+=(blocked);         PASSTHROUGH+=("$1"); shift;;
        --id)
            ASP_ID="${2-}"
            PASSTHROUGH+=(--id "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --limit)
            LIMIT="${2-}"
            PASSTHROUGH+=(--limit "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        *)
            # Unknown flag — ignored by daemon, kept for completeness.
            PASSTHROUGH+=("$1")
            shift;;
    esac
done

# --- Daemon path ----------------------------------------------------------
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY="source=${SOURCE_VAL}"
for key in "${FLAG_KEYS[@]+"${FLAG_KEYS[@]}"}"; do
    QUERY+="&${key}=1"
done
[ -n "$ASP_ID" ] && QUERY+="&id=${ASP_ID}"
[ -n "$LIMIT" ]  && QUERY+="&limit=${LIMIT}"

rc=0
rt_call GET /v1/aspirations/read --query "$QUERY" || rc=$?

case $rc in
    0) exit 0;;
    2)
        # Daemon answered with HTTP 4xx/5xx; body already written to stderr.
        exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback. Try one
        # auto-spawn, then fail loud. See .claude/rules/no-python-cli-fallback.md.
        if rt_try_autospawn; then
            rc=0
            rt_call GET /v1/aspirations/read --query "$QUERY" || rc=$?
            if [ "$rc" = "0" ]; then exit 0; fi
        fi
        rt_no_daemon_error "aspirations-read.sh";;
    *)
        exit $rc;;
esac
