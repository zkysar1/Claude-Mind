#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# meta-log-append — daemon-aware wrapper. Appends a JSON record from stdin
# to meta/meta-log.jsonl.
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Read stdin body (JSON record)
#   3. POST /v1/meta/yaml/log
#   4. On 200, print the daemon's confirmation
#      ({"status":"logged","path":...,"offset":...,"bytes":...})
#
# The confirmation is the contract meta-strategies.md documents, and it is what
# makes a dropped or misrouted append self-detecting: `path` exposes a daemon
# bound to an unexpected meta root, `offset` proves the store actually grew.
# This wrapper used to discard the response with `> /dev/null` and exit 0
# silently, so a caller could not distinguish "written" from "swallowed"
# (; the /dev/null-on-a-governed-store shape guard-989 warns about).
#
# Usage: echo '{"event":"..."}' | meta-log-append.sh
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# Read stdin (the JSON record) BEFORE invoking the daemon.
BODY="$(cat)"

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

rc=0
RESP="$(rt_call POST /v1/meta/yaml/log --body-string "$BODY")" || rc=$?

case $rc in
    0) printf '%s\n' "$RESP"; exit 0;;
    # Surface the daemon's error body rather than exiting 1 mute — a refused
    # append must be distinguishable from a landed one at the call site, which
    # is the whole point of this wrapper emitting anything at all (guard-989).
    2) [ -n "$RESP" ] && printf '%s\n' "$RESP" >&2; exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESP="$(rt_call POST /v1/meta/yaml/log --body-string "$BODY")" || rc=$?
            if [ "$rc" = "0" ]; then printf '%s\n' "$RESP"; exit 0; fi
        fi
        rt_no_daemon_error "meta-log-append.sh";;
    *) exit $rc;;
esac
