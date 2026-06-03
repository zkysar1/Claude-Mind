#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# meta-set.sh — Set a field in a meta-strategy file (bounds-validated, auto-logged)
# Daemon path: rt_call POST /v1/meta/yaml/set. The CLI printed nothing on success;
# the daemon returns a JSON ack that is suppressed (suppress-empty translation).
# Usage: meta-set.sh <file> <dotpath> <value> [--string] [--reason "..."]
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
FILE=""; DOTPATH=""; VALUE=""; STRING=0; REASON=""
POS=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --string) STRING=1; shift;;
        --reason) REASON="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        *)
            case $POS in
                0) FILE="$1";;
                1) DOTPATH="$1";;
                2) VALUE="$1";;
            esac
            POS=$((POS + 1))
            shift;;
    esac
done

if [ -z "$FILE" ] || [ -z "$DOTPATH" ] || [ "$POS" -lt 3 ]; then
    echo "Usage: meta-set.sh <file> <dotpath> <value> [--string] [--reason \"...\"]" >&2
    exit 1
fi

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

# Build JSON body. Use python to ensure correct JSON encoding of the value.
BODY="$($(rt_python_launcher) -c "
import json, sys
body = {'file': sys.argv[1], 'dotpath': sys.argv[2], 'value': sys.argv[3]}
if sys.argv[4] == '1':
    body['string'] = True
if sys.argv[5]:
    body['reason'] = sys.argv[5]
print(json.dumps(body, ensure_ascii=False))
" "$FILE" "$DOTPATH" "$VALUE" "$STRING" "$REASON")"

rc=0
rt_call POST /v1/meta/yaml/set --body-string "$BODY" > /dev/null || rc=$?

case $rc in
    0) exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            rt_call POST /v1/meta/yaml/set --body-string "$BODY" > /dev/null || rc=$?
            if [ "$rc" = "0" ]; then exit 0; fi
        fi
        rt_no_daemon_error "meta-set.sh";;
    *) exit $rc;;
esac
