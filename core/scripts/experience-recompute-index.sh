#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Recompute experiential-index.yaml from pipeline data — daemon-aware wrapper.
# Daemon path: rt_call POST /v1/experience/recompute-index.
# The endpoint returns {"ok":true,"index":{...}} — the _print_index translator
# extracts the index object and prints it with indent=2 to match the CLI output.
# Usage: bash core/scripts/experience-recompute-index.sh
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

_print_index() {
    # shellcheck disable=SC2086
    printf '%s' "$1" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
idx = resp.get('index') or resp
print(json.dumps(idx, indent=2, ensure_ascii=False))
"
}

rc=0
RESPONSE="$(rt_call POST /v1/experience/recompute-index)" || rc=$?

case $rc in
    0) _print_index "$RESPONSE"; exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/experience/recompute-index)" || rc=$?
            if [ "$rc" = "0" ]; then _print_index "$RESPONSE"; exit 0; fi
        fi
        rt_no_daemon_error "experience-recompute-index.sh";;
    *) exit $rc;;
esac
