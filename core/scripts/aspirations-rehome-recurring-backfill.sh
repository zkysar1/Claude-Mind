#!/usr/bin/env bash
# aspirations-rehome-recurring-backfill.sh — one-shot sweep over ALREADY-ARCHIVED
# aspirations for stranded live recurring goals (, 2026-09-01).
#
# The archive-time guard (complete / complete-intent / retire) now re-homes a
# live recurring goal into a live container instead of archiving it, because
# an archived recurring goal is undetectably dead: the selector never reads the
# archive and every cadence instrument is scoped away from it. This wrapper
# applies the same rule to aspirations archived BEFORE the guard existed.
#
# Usage: bash core/scripts/aspirations-rehome-recurring-backfill.sh
#            [--source world|agent] [--target <live-asp-id>] [--dry-run]
#   --target   the live aspiration that adopts the goals; without it the daemon
#              auto-detects (recurring.rehome_container in core/config/
#              aspirations.yaml, else the live aspiration flagged
#              `recurring_home: true`, else the live ACTIVE aspiration holding
#              the most recurring goals). No live container => rc=1 with
#              recurring_rehome_target_missing, nothing written.
#   --dry-run  report the plan (moved ids per archived aspiration), write nothing.
# Output: one JSON object (archived_scanned, stranded_aspirations, moved,
# moved_count, target, target_how); warnings on stderr. Idempotent.
# Daemon-only (no Python CLI fallback): POST /v1/aspirations/rehome-recurring-backfill.
set -euo pipefail
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

SOURCE_VAL="world"
TARGET_VAL=""
DRY_RUN="false"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --source)  SOURCE_VAL="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --target)  TARGET_VAL="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --dry-run) DRY_RUN="true"; shift;;
        -h|--help) sed -n '2,22p' "$0"; exit 0;;
        *) shift;;
    esac
done

source "$CORE_ROOT/scripts/_runtime.sh"

QUERY="source=${SOURCE_VAL}&dry_run=${DRY_RUN}"
[[ -n "$TARGET_VAL" ]] && QUERY="${QUERY}&rehome_target=${TARGET_VAL}"

_print_response() {
    printf '%s' "$1" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
for w in resp.get('warnings') or []:
    print('WARNING: ' + w, file=sys.stderr)
resp.pop('warnings', None)
print(json.dumps(resp, ensure_ascii=False))
"
}

rc=0
RESPONSE="$(rt_call POST /v1/aspirations/rehome-recurring-backfill \
    --query "$QUERY")" || rc=$?
case $rc in
    0)
        _print_response "$RESPONSE"
        exit 0;;
    2)
        exit 1;;
    3)
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/aspirations/rehome-recurring-backfill \
                --query "$QUERY")" || rc=$?
            if [ "$rc" = "0" ]; then
                _print_response "$RESPONSE"
                exit 0
            fi
        fi
        rt_no_daemon_error "aspirations-rehome-recurring-backfill.sh";;
    *)
        exit $rc;;
esac
