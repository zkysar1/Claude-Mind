#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# tree-reconcile-capabilities — daemon-aware wrapper. Recomputes
# capability_level for every node from its confidence vs competence_mapping
# thresholds.
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. POST /v1/tree/write  {"op":"reconcile-capabilities"}
#   3. On 200, translate: strip daemon envelope (ok, op) and print the
#      CLI-compat JSON (reconciled, total_nodes, changes) with indent=2.
#
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# No args to parse — reconcile-capabilities takes no flags.

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

BODY='{"op":"reconcile-capabilities"}'

_translate() {
    # Strip daemon envelope (ok, op) to reproduce CLI stdout:
    #   json.dumps({"reconciled":N,"total_nodes":N,"changes":[...]}, indent=2, ensure_ascii=False)
    # shellcheck disable=SC2086
    printf '%s' "$1" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
out = {
    'reconciled': resp.get('reconciled', 0),
    'total_nodes': resp.get('total_nodes', 0),
    'changes': resp.get('changes', []),
}
print(json.dumps(out, indent=2, ensure_ascii=False))
"
}

rc=0
RESPONSE="$(rt_call POST /v1/tree/write \
    --body-string "$BODY")" || rc=$?

case $rc in
    0) _translate "$RESPONSE"; exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/tree/write \
                --body-string "$BODY")" || rc=$?
            if [ "$rc" = "0" ]; then _translate "$RESPONSE"; exit 0; fi
        fi
        rt_no_daemon_error "tree-reconcile-capabilities.sh";;
    *) exit $rc;;
esac
