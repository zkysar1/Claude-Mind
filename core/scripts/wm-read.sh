#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Read a working-memory slot — daemon-aware wrapper.
#
# Migrated for Phase B. Hot path: rt_call /v1/wm/read.
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve -------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# : shared strict-argv refusal helpers (uniform message contract).
# Sourced BEFORE _runtime.sh so a refusal cannot be masked by a daemon failure.
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_argv_strict.sh"

# --- Parse args -----------------------------------------------------------
SLOT=""
AS_JSON=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            echo "Usage: wm-read.sh <slot> [--json]"
            exit 0;;
        --json)
            AS_JSON=1
            shift;;
        --*)
            # : was a silent append to the dead PASSTHROUGH
            # accumulator — a mistyped flag vanished and the read answered
            # with the filter missing (rc=0, wrong population, rb-245 shape).
            argv_strict_refuse_unknown "wm-read.sh" "$1" "--json (plus one <slot> positional)";;
        *)
            # Positional argument — slot name. First positional wins; a SECOND
            # positional used to be silently swallowed () and is now
            # refused like an unknown flag.
            if [ -z "$SLOT" ]; then
                SLOT="$1"
            else
                argv_strict_refuse_extra_positional "wm-read.sh" "$1" 1 "--json"
            fi
            shift;;
    esac
done

# --- Daemon path ----------------------------------------------------------
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY=""
if [ -n "$SLOT" ]; then
    QUERY="slot=$(rt_url_encode "$SLOT")"
fi
if [ "$AS_JSON" = "1" ]; then
    [ -n "$QUERY" ] && QUERY+="&"
    QUERY+="json=1"
fi

rc=0
if [ -n "$QUERY" ]; then
    rt_call GET /v1/wm/read --query "$QUERY" || rc=$?
else
    rt_call GET /v1/wm/read || rc=$?
fi

case $rc in
    0) exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            if [ -n "$QUERY" ]; then
                rt_call GET /v1/wm/read --query "$QUERY" || rc=$?
            else
                rt_call GET /v1/wm/read || rc=$?
            fi
            if [ "$rc" = "0" ]; then exit 0; fi
        fi
        rt_no_daemon_error "wm-read.sh";;
    *)
        exit $rc;;
esac
