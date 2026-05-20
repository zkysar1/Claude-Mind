#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Read tree data — daemon-aware wrapper.
#
# Migrated for Phase B PR 4. Daemon path: rt_call /v1/tree/read.
#
# Simple lookups (--node, --path, --ancestors, --children, --leaves,
# --leaves-under, --stats, --child-path, --summary, --maintenance) are
# served by the daemon. Computational flags (--validate,
# --decompose-candidates, --redistribute-candidates, --distill-candidates,
# --active-content) and the legacy --find shorthand go direct to python —
# they run cross-file logic that isn't yet daemon-safe, or have a
# dedicated endpoint (tree-find-node.sh for --find).
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

declare -a PASSTHROUGH=()
declare -a FLAG_KEYS=()
NODE=""; PATH_KEY=""; ANCESTORS=""; CHILDREN=""; LEAVES_UNDER=""; FIND=""
CHILD_PATH_P=""; CHILD_PATH_S=""
BY_L1=0
FORCE_FALLBACK=0

# Value-arg pattern: "${2-}" + safe shift; see retrieve.sh for rationale.
# --child-path takes TWO values, so it gets a three-tier shift guard.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --node)         NODE="${2-}";        PASSTHROUGH+=(--node "${2-}"); shift $(( $# >= 2 ? 2 : 1 ));;
        --path)         PATH_KEY="${2-}";    PASSTHROUGH+=(--path "${2-}"); shift $(( $# >= 2 ? 2 : 1 ));;
        --ancestors)    ANCESTORS="${2-}";   PASSTHROUGH+=(--ancestors "${2-}"); shift $(( $# >= 2 ? 2 : 1 ));;
        --children)     CHILDREN="${2-}";    PASSTHROUGH+=(--children "${2-}"); shift $(( $# >= 2 ? 2 : 1 ));;
        --leaves-under) LEAVES_UNDER="${2-}"; PASSTHROUGH+=(--leaves-under "${2-}"); shift $(( $# >= 2 ? 2 : 1 ));;
        --leaves)       FLAG_KEYS+=(leaves);      PASSTHROUGH+=("$1"); shift;;
        --stats)        FLAG_KEYS+=(stats);       PASSTHROUGH+=("$1"); shift;;
        --summary)      FLAG_KEYS+=(summary);     PASSTHROUGH+=("$1"); shift;;
        --maintenance)  FLAG_KEYS+=(maintenance); PASSTHROUGH+=("$1"); shift;;
        --by-l1)        BY_L1=1; PASSTHROUGH+=("$1"); shift;;
        --child-path)
            CHILD_PATH_P="${2-}"; CHILD_PATH_S="${3-}"
            PASSTHROUGH+=(--child-path "${2-}" "${3-}")
            shift $(( $# >= 3 ? 3 : ($# >= 2 ? 2 : 1) ));;
        --find)
            # Use tree-find-node.sh for richer matching; fall through here so
            # behavior parity with the pre-migration CLI is preserved.
            FIND="${2-}"; PASSTHROUGH+=(--find "${2-}")
            FORCE_FALLBACK=1
            shift $(( $# >= 2 ? 2 : 1 ));;
        # Computationally-heavy flags — fall through unconditionally.
        --validate|--decompose-candidates|--redistribute-candidates|--distill-candidates)
            FORCE_FALLBACK=1
            PASSTHROUGH+=("$1"); shift;;
        --active-content)
            FORCE_FALLBACK=1
            PASSTHROUGH+=("$1" "${2-}"); shift $(( $# >= 2 ? 2 : 1 ));;
        *)
            PASSTHROUGH+=("$1"); shift;;
    esac
done

source "$CORE_ROOT/scripts/_runtime.sh"

QUERY=""
[ -n "$NODE" ]         && QUERY="node=$(rt_url_encode "$NODE")"
[ -n "$PATH_KEY" ]     && { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="path=$(rt_url_encode "$PATH_KEY")"; }
[ -n "$ANCESTORS" ]    && { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="ancestors=$(rt_url_encode "$ANCESTORS")"; }
[ -n "$CHILDREN" ]     && { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="children=$(rt_url_encode "$CHILDREN")"; }
[ -n "$LEAVES_UNDER" ] && { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="leaves_under=$(rt_url_encode "$LEAVES_UNDER")"; }
if [ -n "$CHILD_PATH_P" ]; then
    [ -n "$QUERY" ] && QUERY+="&"
    QUERY+="child_path=$(rt_url_encode "$CHILD_PATH_P,$CHILD_PATH_S")"
fi
for key in "${FLAG_KEYS[@]+"${FLAG_KEYS[@]}"}"; do
    [ -n "$QUERY" ] && QUERY+="&"
    QUERY+="${key}=1"
done
if [ "$BY_L1" = "1" ]; then
    [ -n "$QUERY" ] && QUERY+="&"
    QUERY+="by_l1=1"
fi

# --by-l1 (S1) computes a full per-L1 walk — fall through to direct python
# until the daemon endpoint learns the param. Cheap on a cold path; safe
# default since the daemon would silently ignore unknown query params.
if [ "$BY_L1" = "1" ]; then
    FORCE_FALLBACK=1
fi

# Computational flags require direct python (not a fallback — these operations
# are not yet daemon-safe). This is a feature path, not a CLI fallback.
if [ "$FORCE_FALLBACK" = "1" ]; then
    # shellcheck disable=SC1091
    source "$CORE_ROOT/scripts/_paths.sh"
    cd "$PROJECT_ROOT"
    # shellcheck disable=SC1091
    source "$CORE_ROOT/scripts/_platform.sh"
    # shellcheck disable=SC2086
    exec python3 "$CORE_ROOT/scripts/tree.py" read \
        "${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}"
fi

if [ -z "$QUERY" ]; then
    echo "Error: at least one query parameter is required." >&2
    exit 1
fi

rc=0
rt_call GET /v1/tree/read --query "$QUERY" || rc=$?

case $rc in
    0) exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            rt_call GET /v1/tree/read --query "$QUERY" || rc=$?
            if [ "$rc" = "0" ]; then exit 0; fi
        fi
        rt_no_daemon_error "tree-read.sh";;
    *)
        exit $rc;;
esac
