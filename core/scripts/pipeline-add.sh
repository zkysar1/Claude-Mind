#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# pipeline-add — daemon-aware wrapper (PR 8).
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Read stdin body (JSON pipeline record)
#   3. POST /v1/pipeline/add
#   4. On 200, print the record to stdout
#
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# : shared strict-argv refusal helpers (uniform message contract).
# Sourced BEFORE _runtime.sh so a refusal cannot be masked by a daemon failure.
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_argv_strict.sh"

# --- Parse args -----------------------------------------------------------
SCHEMA=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            echo "Usage: printf '%s' '<json>' | pipeline-add.sh"
            exit 0;;
        --schema)
            SCHEMA=1
            shift;;
        *)
            # : was a silent append to the dead PASSTHROUGH
            # accumulator. Worst case measured: `--help` fell through here,
            # then BODY="$(cat)" below blocked FOREVER waiting on stdin —
            # a 120s Bash-tool timeout plus a backgrounded orphan. Refusing
            # BEFORE the cat converts the hang into an immediate error.
            argv_strict_refuse_unknown "pipeline-add.sh" "$1" "(none — the record JSON arrives on STDIN)";;
    esac
done

# --- Pre-daemon validation ------------------------------------------------
if [ "$SCHEMA" = "1" ]; then
    echo "Error: --schema is no longer available. See mind_api/src/endpoints/ for API docs." >&2
    exit 1
fi

# Read stdin (the JSON pipeline record) BEFORE invoking the daemon.
BODY="$(cat)"

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

rc=0
RESPONSE="$(rt_call POST /v1/pipeline/add \
    --body-string "$BODY" \
    )" || rc=$?

case $rc in
    0)
        # shellcheck disable=SC2086
        printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
rec = resp.get('record') or resp
print(json.dumps(rec, indent=2, ensure_ascii=False))
"
        exit 0;;
    2)
        exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/pipeline/add \
                --body-string "$BODY")" || rc=$?
            if [ "$rc" = "0" ]; then
                # shellcheck disable=SC2086
                printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
rec = resp.get('record') or resp
print(json.dumps(rec, indent=2, ensure_ascii=False))
"
                exit 0
            fi
        fi
        rt_no_daemon_error "pipeline-add.sh";;
    *)
        exit $rc;;
esac
