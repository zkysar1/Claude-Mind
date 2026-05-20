#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# pipeline-archive — daemon-aware wrapper (PR 56).
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. POST /v1/pipeline/archive-sweep (no params — batch sweep)
#   3. On 200, print archived_count to stdout (matches legacy CLI shape)
#
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Passthrough args for fallback ----------------------------------------
declare -a PASSTHROUGH=("$@")

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

rc=0
RESPONSE="$(rt_call POST /v1/pipeline/archive-sweep)" || rc=$?

case $rc in
    0)
        # 200: parse response. Print archived_count to stdout
        # (matches legacy CLI "print(str(count))" shape).
        # shellcheck disable=SC2086
        printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
print(resp.get('archived_count', 0))
"
        exit 0;;
    2)
        # Daemon answered 4xx/5xx; body already written to stderr by rt_curl.
        exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/pipeline/archive-sweep)" || rc=$?
            if [ "$rc" = "0" ]; then
                # shellcheck disable=SC2086
                printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
print(resp.get('archived_count', 0))
"
                exit 0
            fi
        fi
        rt_no_daemon_error "pipeline-archive.sh";;
    *)
        exit $rc;;
esac
