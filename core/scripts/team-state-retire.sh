#!/usr/bin/env bash
# DAEMON-ONLY. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Sanctioned agent-row retirement (): removes an agent's team-state
# presence (core-file agent_status residual + per-agent shard), gated by
# archive-before-delete (archive lands in world/team-state/.graveyard/).
# Daemon path: rt_call POST /v1/team-state/retire-agent.
# Usage: bash core/scripts/team-state-retire.sh --agent <name> [--source <str>] [--dry-run]
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# Value-arg pattern: "${2-}" + safe shift; see _runtime.sh / team-state-update.sh.
AGENT=""; SOURCE=""; DRY_RUN=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --agent)   AGENT="${2-}";  shift $(( $# >= 2 ? 2 : 1 ));;
        --source)  SOURCE="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --dry-run) DRY_RUN="true"; shift;;
        --author)  shift $(( $# >= 2 ? 2 : 1 ));;  # daemon uses X-Mind-Agent header
        *) shift;;
    esac
done

if [ -z "$AGENT" ]; then
    echo "team-state-retire.sh: --agent <name> required" >&2
    exit 1
fi

# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY=""
_append_q() { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="$1"; }
_append_q "agent=$(rt_url_encode "$AGENT")"
[ -n "$SOURCE" ]  && _append_q "source=$(rt_url_encode "$SOURCE")"
[ -n "$DRY_RUN" ] && _append_q "dry_run=true"

_translate() {
    # Response is the retire result dict (or {"error":...}). Surface app
    # errors as exit 1 (not swallowed as transport success); print the
    # result JSON on success.
    # shellcheck disable=SC2086
    printf '%s' "$1" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
if 'error' in resp:
    print(resp.get('detail') or resp['error'], file=sys.stderr)
    sys.exit(1)
print(json.dumps(resp, ensure_ascii=False))
"
}

rc=0
RESPONSE="$(rt_call POST /v1/team-state/retire-agent --query "$QUERY")" || rc=$?

case $rc in
    0) _translate "$RESPONSE"; exit $?;;
    2) exit 1;;
    3)
        # DAEMON-ONLY: no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/team-state/retire-agent --query "$QUERY")" || rc=$?
            if [ "$rc" = "0" ]; then _translate "$RESPONSE"; exit $?; fi
        fi
        rt_no_daemon_error "team-state-retire.sh";;
    *) exit $rc;;
esac
