#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Restore a historical version of a file — daemon-aware wrapper.
# Daemon path: rt_call POST /v1/history/restore. The endpoint prints
# byte-compat-to-CLI stdout, so the response body is emitted verbatim
# (no translation).
# Usage: bash core/scripts/history-restore.sh <file> <version-name>
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse positional args ---------------------------------------------------
FILE="${1-}"
VERSION="${2-}"

if [ -z "$FILE" ] || [ -z "$VERSION" ]; then
    echo "Usage: history-restore.sh <file> <version-name>" >&2
    exit 1
fi

# --- Daemon path -------------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY="file=$(rt_url_encode "$FILE")&version=$(rt_url_encode "$VERSION")"

rc=0
rt_call POST /v1/history/restore --query "$QUERY" || rc=$?
case $rc in
    0) exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            rt_call POST /v1/history/restore --query "$QUERY" || rc=$?
            if [ "$rc" = "0" ]; then exit 0; fi
        fi
        rt_no_daemon_error "history-restore.sh";;
    *) exit $rc;;
esac
