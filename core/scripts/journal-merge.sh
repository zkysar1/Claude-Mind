#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-15 (H2 Wave 1). No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
#   zeta/reports/phase3-h2-wave-plan.md (generic store endpoint)
# journal-merge — daemon-aware wrapper. Merge a JSON patch into an existing
# journal session index record (union goals_completed/tags, append
# key_events, scalar overwrite — semantics from store_registry.merge_lists).
#
# Usage:  journal-merge.sh <session-id>   (session-N or bare N) + stdin JSON
#
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
SESSION_ID=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -*) shift;;
        *) [ -z "$SESSION_ID" ] && SESSION_ID="$1"; shift;;
    esac
done

if [ -z "$SESSION_ID" ]; then
    echo "Error: journal-merge.sh requires a session id argument (session-N or N)." >&2
    exit 1
fi

# Read stdin (the JSON merge patch) BEFORE invoking the daemon.
BODY="$(cat)"

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

_print_record() {
    # shellcheck disable=SC2086
    printf '%s' "$1" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
rec = resp.get('record') or resp
print(json.dumps(rec, indent=2, ensure_ascii=False))
"
}

QUERY="store=journal&id=$(rt_url_encode "$SESSION_ID")"

rc=0
RESPONSE="$(rt_call POST /v1/store/merge \
    --query "$QUERY" \
    --body-string "$BODY")" || rc=$?

case $rc in
    0) _print_record "$RESPONSE"; exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/store/merge \
                --query "$QUERY" \
                --body-string "$BODY")" || rc=$?
            if [ "$rc" = "0" ]; then _print_record "$RESPONSE"; exit 0; fi
        fi
        rt_no_daemon_error "journal-merge.sh";;
    *) exit $rc;;
esac
