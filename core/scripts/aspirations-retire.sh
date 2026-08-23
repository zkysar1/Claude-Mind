#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# aspirations-retire — daemon-aware wrapper (PR 9b).
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Parse --source / --force flags + positional asp_id
#   3. POST /v1/aspirations/retire?asp_id=<a>&source=<s>[&force=true]
#   4. On 200, re-emit warnings[] to stderr and print aspiration to stdout
#
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
# Sourced BEFORE _runtime.sh so the refusal cannot be masked by a daemon
# failure (see _argv_strict.sh header).
source "$CORE_ROOT/scripts/_argv_strict.sh"

SOURCE_VAL="world"
FORCE=0
ASP_ID=""
# ONE literal, shared by the help text and the refusal message — never two
# (_argv_strict.sh header: two strings that must agree is the failure class
# the refusal exists to remove).
_ACCEPTED_FLAGS="--source <world|agent> | --force"
declare -a PASSTHROUGH=()
declare -a PASSTHROUGH_SOURCE=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source)
            SOURCE_VAL="${2-}"
            PASSTHROUGH_SOURCE=(--source "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --force)
            FORCE=1
            PASSTHROUGH+=("$1"); shift;;
        -h|--help)
            argv_strict_help "aspirations-retire.sh" "<asp-id> [--source world|agent] [--force]" "$_ACCEPTED_FLAGS";;
        -*)
            # REFUSE (). Was `PASSTHROUGH+=("$1"); shift`, and
            # PASSTHROUGH has NO READER here — a vestige of the Python CLI
            # fallback deleted 2026-05-14. The unrecognised flag vanished and
            # its VALUE slid into the `*)` arm below to become ASP_ID, so the
            # command retired a DIFFERENT aspiration than the caller named,
            # with exit status 0.
            argv_strict_refuse_unknown "aspirations-retire.sh" "$1" "$_ACCEPTED_FLAGS";;
        *)
            [ -z "$ASP_ID" ] && ASP_ID="$1"
            PASSTHROUGH+=("$1"); shift;;
    esac
done

# Missing asp_id → error
if [ -z "$ASP_ID" ]; then
    echo "Error: asp_id is required." >&2
    exit 1
fi

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY="asp_id=${ASP_ID}&source=${SOURCE_VAL}"
[ "$FORCE" = "1" ] && QUERY="${QUERY}&force=true"

rc=0
RESPONSE="$(rt_call POST /v1/aspirations/retire --query "$QUERY")" || rc=$?

case $rc in
    0)
        # shellcheck disable=SC2086
        printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
for w in resp.get('warnings') or []:
    print(w, file=sys.stderr)
asp = resp.get('aspiration')
if asp is not None:
    print(json.dumps(asp, indent=2, ensure_ascii=False))
"
        exit 0;;
    2)
        exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/aspirations/retire --query "$QUERY")" || rc=$?
            if [ "$rc" = "0" ]; then
                # shellcheck disable=SC2086
                printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
for w in resp.get('warnings') or []:
    print(w, file=sys.stderr)
asp = resp.get('aspiration')
if asp is not None:
    print(json.dumps(asp, indent=2, ensure_ascii=False))
"
                exit 0
            fi
        fi
        rt_no_daemon_error "aspirations-retire.sh";;
    *)
        exit $rc;;
esac
