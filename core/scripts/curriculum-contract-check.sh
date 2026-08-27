#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# curriculum-contract-check — daemon-aware wrapper. Checks whether an
# action is permitted by the current curriculum stage.
#
# Daemon path: rt_call GET /v1/curriculum/contract-check?action=<cap>
#
# EXIT CODE PARITY: the CLI did sys.exit(0 if permitted else 1).
# The daemon always returns HTTP 200 with permitted=true/false in the
# JSON body. The wrapper reproduces the CLI exit-code contract by
# parsing the response.
#
# Usage: bash core/scripts/curriculum-contract-check.sh --action <capability>
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
ACTION=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --action) ACTION="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        *) shift;;
    esac
done

if [ -z "$ACTION" ]; then
    echo "Error: --action is required." >&2
    exit 1
fi

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY="action=$(rt_url_encode "$ACTION")"

# _exit_for_permitted: print the response body and exit 0 if permitted,
# exit 1 if not permitted (reproduces CLI sys.exit(0 if permitted else 1)).
_exit_for_permitted() {
    # $() strips trailing newlines from rt_call output. The CLI's print()
    # emits a trailing newline, so restore it with printf '%s\n'.
    printf '%s\n' "$1"
    # The response is single-line JSON (no indent). Check for permitted field.
    case "$1" in
        *'"permitted": true'*) exit 0;;
        *'"permitted": false'*) exit 1;;
        # Fallback: if permitted field not found, exit 0 (matches CLI
        # graceful-degradation where no curriculum = exit 0).
        *) exit 0;;
    esac
}

rc=0
RESPONSE="$(rt_call GET /v1/curriculum/contract-check --query "$QUERY")" || rc=$?

case $rc in
    0) _exit_for_permitted "$RESPONSE";;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call GET /v1/curriculum/contract-check --query "$QUERY")" || rc=$?
            if [ "$rc" = "0" ]; then _exit_for_permitted "$RESPONSE"; fi
        fi
        rt_no_daemon_error "curriculum-contract-check.sh";;
    *) exit $rc;;
esac
