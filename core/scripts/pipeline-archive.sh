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
        # pruned_count/stamped_count go to STDERR, never stdout: the PRUNE is a
        # DELETE (it permanently removes tombstones older than PRUNE_GRACE_DAYS
        # from the live file), and reporting zero of it made the destructive
        # half of this sweep invisible to every CLI caller — the observability
        # gap archive-before-delete.md exists to close. Kept off stdout because
        # callers parse it as a bare integer.
        # shellcheck disable=SC2086
        printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
pruned = resp.get('pruned_count', 0)
stamped = resp.get('stamped_count', 0)
if pruned or stamped:
    sys.stderr.write('[pipeline-archive] pruned_count=%s stamped_count=%s\n' % (pruned, stamped))
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
pruned = resp.get('pruned_count', 0)
stamped = resp.get('stamped_count', 0)
if pruned or stamped:
    sys.stderr.write('[pipeline-archive] pruned_count=%s stamped_count=%s\n' % (pruned, stamped))
print(resp.get('archived_count', 0))
"
                exit 0
            fi
        fi
        rt_no_daemon_error "pipeline-archive.sh";;
    *)
        exit $rc;;
esac
