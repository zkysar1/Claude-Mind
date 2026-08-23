#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-15 (H2 Wave 3). No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
#   zeta/reports/phase3-h2-wave-plan.md (generic store endpoint)
# pattern-signatures-update -- daemon-aware wrapper. Full replace of a
# pattern-signature record from stdin JSON.
#
# Usage:  pattern-signatures-update.sh <rec_id>
#
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
REC_ID=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -*) shift;;
        *)
            if [ -z "$REC_ID" ]; then REC_ID="$1"; fi
            shift;;
    esac
done

if [ -z "$REC_ID" ]; then
    echo "Usage: pattern-signatures-update.sh <rec_id>" >&2
    exit 1
fi

# Read stdin (the full JSON record) BEFORE invoking the daemon.
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

QUERY="store=pattern-signatures&id=$(rt_url_encode "$REC_ID")"

rc=0
RESPONSE="$(rt_call POST /v1/store/replace \
    --query "$QUERY" \
    --body-string "$BODY")" || rc=$?

case $rc in
    0) _print_record "$RESPONSE"; exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-15 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/store/replace \
                --query "$QUERY" \
                --body-string "$BODY")" || rc=$?
            if [ "$rc" = "0" ]; then _print_record "$RESPONSE"; exit 0; fi
        fi
        rt_no_daemon_error "pattern-signatures-update.sh";;
    *) exit $rc;;
esac
