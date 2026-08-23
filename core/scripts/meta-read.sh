#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Read a meta-strategy file or field — daemon-aware wrapper. Daemon path:
# rt_call GET /v1/meta/yaml/read. The endpoint prints byte-compat-to-CLI
# stdout, so the response body is emitted verbatim (no translation).
# Usage: bash core/scripts/meta-read.sh <file> [--field <dotpath>] [--json]
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# Value-arg pattern: "${2-}" + safe shift; see _runtime.sh / tree-read.sh.
FILE=""; FIELD=""; JSON=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --field) FIELD="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --json)  JSON=1; shift;;
        -*)      shift;;
        *)
            # First positional arg is the file
            if [ -z "$FILE" ]; then
                FILE="$1"
            fi
            shift;;
    esac
done

if [ -z "$FILE" ]; then
    echo "Error: file argument is required." >&2
    echo "Usage: meta-read.sh <file> [--field <dotpath>] [--json]" >&2
    exit 1
fi

# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY="file=$(rt_url_encode "$FILE")"
[ -n "$FIELD" ]   && QUERY+="&field=$(rt_url_encode "$FIELD")"
[ "$JSON" = "1" ] && QUERY+="&json=1"

rc=0
rt_call GET /v1/meta/yaml/read --query "$QUERY" || rc=$?
case $rc in
    0) exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            rt_call GET /v1/meta/yaml/read --query "$QUERY" || rc=$?
            if [ "$rc" = "0" ]; then exit 0; fi
        fi
        rt_no_daemon_error "meta-read.sh";;
    *) exit $rc;;
esac
