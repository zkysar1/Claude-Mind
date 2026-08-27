#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# pipeline-recompute-meta — daemon-aware wrapper. Full recount of
# pipeline-meta.json from live + archive records.
#
# Daemon path: rt_call POST /v1/pipeline/recompute-meta
#
# The endpoint returns {"ok":true,"meta":{...}}. The old CLI printed
# json.dumps(meta, indent=2, ensure_ascii=False), so we extract .meta
# and re-serialize with the same format.
#
# Usage: bash core/scripts/pipeline-recompute-meta.sh
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

_print_meta() {
    # shellcheck disable=SC2086
    printf '%s' "$1" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
meta = resp.get('meta') or resp
print(json.dumps(meta, indent=2, ensure_ascii=False))
"
}

rc=0
RESPONSE="$(rt_call POST /v1/pipeline/recompute-meta)" || rc=$?

case $rc in
    0) _print_meta "$RESPONSE"; exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/pipeline/recompute-meta)" || rc=$?
            if [ "$rc" = "0" ]; then _print_meta "$RESPONSE"; exit 0; fi
        fi
        rt_no_daemon_error "pipeline-recompute-meta.sh";;
    *) exit $rc;;
esac
