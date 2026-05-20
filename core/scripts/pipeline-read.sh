#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Read pipeline records — daemon-aware wrapper.
#
# Migrated for Phase B PR 4. Daemon path: rt_call /v1/pipeline/read.
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

declare -a FLAG_KEYS=()
declare -a PASSTHROUGH=()
STAGE=""
REC_ID=""

# Value-arg pattern: "${2-}" + safe shift; see retrieve.sh for rationale.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --stage)
            STAGE="${2-}"
            PASSTHROUGH+=(--stage "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --id)
            REC_ID="${2-}"
            PASSTHROUGH+=(--id "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --summary)            FLAG_KEYS+=(summary);            PASSTHROUGH+=("$1"); shift;;
        --counts)             FLAG_KEYS+=(counts);             PASSTHROUGH+=("$1"); shift;;
        --accuracy)           FLAG_KEYS+=(accuracy);           PASSTHROUGH+=("$1"); shift;;
        --unreflected)        FLAG_KEYS+=(unreflected);        PASSTHROUGH+=("$1"); shift;;
        --replay-candidates)  FLAG_KEYS+=(replay_candidates);  PASSTHROUGH+=("$1"); shift;;
        --archive)            FLAG_KEYS+=(archive);            PASSTHROUGH+=("$1"); shift;;
        --meta)               FLAG_KEYS+=(meta);               PASSTHROUGH+=("$1"); shift;;
        *)
            PASSTHROUGH+=("$1"); shift;;
    esac
done

source "$CORE_ROOT/scripts/_runtime.sh"
source "$CORE_ROOT/scripts/_paths.sh"

QUERY=""
[ -n "$STAGE" ]  && QUERY="stage=$(rt_url_encode "$STAGE")"
[ -n "$REC_ID" ] && { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="id=$(rt_url_encode "$REC_ID")"; }
for key in "${FLAG_KEYS[@]+"${FLAG_KEYS[@]}"}"; do
    [ -n "$QUERY" ] && QUERY+="&"
    QUERY+="${key}=1"
done

if [ -z "$QUERY" ]; then
    echo "Error: at least one filter is required." >&2
    exit 1
fi

# Root cause of the  all-zero-counts symptom was fixed 2026-05-18
# in mind_api/src/world/pipeline_write.py::_update_meta — the read of
# pipeline.jsonl now acquires live_path.lock so a concurrent
# _atomic_write_with_fallback truncate-rewrite window can't be observed as
# an empty file. Regression guard: mind_api/tests/test_runtime_pipeline_update_meta_lock.py.
# The previous defensive disk-reparse here was removed in the same change —
# wrapper-side band-aids mask daemon bugs and violate the daemon-only
# architecture. Gate: core/scripts/check-no-daemon-wrapper-reparse.sh.
rc=0
rt_call GET /v1/pipeline/read --query "$QUERY" || rc=$?
if [ "$rc" = "0" ]; then exit 0; fi

case $rc in
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            rt_call GET /v1/pipeline/read --query "$QUERY" || rc=$?
            if [ "$rc" = "0" ]; then exit 0; fi
        fi
        rt_no_daemon_error "pipeline-read.sh";;
    *)
        exit $rc;;
esac
