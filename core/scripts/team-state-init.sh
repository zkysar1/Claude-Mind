#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Initialize team-state.yaml if it doesn't exist — daemon-aware wrapper.
# Daemon path: rt_call POST /v1/team-state/init.
# Usage: bash core/scripts/team-state-init.sh [--author <name>]
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# Parse args (--author accepted for CLI compat but not forwarded;
# daemon uses X-Mind-Agent header which rt_call sets from $MIND_AGENT).
while [[ $# -gt 0 ]]; do
    case "$1" in
        --author) shift $(( $# >= 2 ? 2 : 1 ));;
        *) shift;;
    esac
done

# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

_translate() {
    # Reproduce CLI stdout from daemon JSON response.
    # created=true  -> "Created <absolute-path>"
    # created=false -> "team-state.yaml already exists"
    printf '%s' "$1" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
if resp.get('created'):
    print('Created ' + sys.argv[1])
else:
    print('team-state.yaml already exists')
" "$WORLD_DIR/team-state.yaml"
}

rc=0
RESPONSE="$(rt_call POST /v1/team-state/init)" || rc=$?

case $rc in
    0) _translate "$RESPONSE"; exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/team-state/init)" || rc=$?
            if [ "$rc" = "0" ]; then _translate "$RESPONSE"; exit 0; fi
        fi
        rt_no_daemon_error "team-state-init.sh";;
    *) exit $rc;;
esac
