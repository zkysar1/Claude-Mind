#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Prune old history snapshots — daemon-aware wrapper. Daemon path:
# rt_call POST /v1/history/prune. The endpoint prints byte-compat-to-CLI
# stdout, so the response body is emitted verbatim (no translation).
# Usage: bash core/scripts/history-prune.sh [--dry-run]
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# Value-arg pattern: see _runtime.sh / changelog-read.sh.
DRY_RUN=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift;;
        *) shift;;
    esac
done

# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY=""
[ "$DRY_RUN" = "1" ] && QUERY="dry_run=1"

rc=0
rt_call POST /v1/history/prune --query "$QUERY" || rc=$?
case $rc in
    0) exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            rt_call POST /v1/history/prune --query "$QUERY" || rc=$?
            if [ "$rc" = "0" ]; then exit 0; fi
        fi
        rt_no_daemon_error "history-prune.sh";;
    *) exit $rc;;
esac
