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

# Shared unknown-flag refusal (, rolled out here by ).
# Sourced BEFORE _runtime.sh so the refusal is cheap and cannot be masked by a
# daemon failure.
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_argv_strict.sh"
# ONE literal, referenced by BOTH the --help arm and the refusal — two strings
# that must agree is the drift surface the refusal exists to remove.
_ACCEPTED_FLAGS="--stage <stage> | --id <rec-id> | --summary | --counts | --accuracy | --unreflected | --replay-candidates | --narrative | --archive | --meta"

declare -a FLAG_KEYS=()
STAGE=""
REC_ID=""

# Value-arg pattern: "${2-}" + safe shift; see retrieve.sh for rationale.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --stage)
            STAGE="${2-}"
            shift $(( $# >= 2 ? 2 : 1 ));;
        --id)
            REC_ID="${2-}"
            shift $(( $# >= 2 ? 2 : 1 ));;
        --summary)            FLAG_KEYS+=(summary);            shift;;
        --counts)             FLAG_KEYS+=(counts);             shift;;
        --accuracy)           FLAG_KEYS+=(accuracy);           shift;;
        --unreflected)        FLAG_KEYS+=(unreflected);        shift;;
        --replay-candidates)  FLAG_KEYS+=(replay_candidates);  shift;;
        # --narrative: normalized outcome narrative (gap-062). Emits
        # {id, stage, outcome, narrative_key, narrative, chars} per record; the
        # 10-key fallback chain lives ONCE in mind_api/src/world/pipeline.py
        # (NARRATIVE_CHAIN) instead of being re-derived by each caller.
        # Composes with --id (one record) and --stage (filtered).
        --narrative)          FLAG_KEYS+=(narrative);          shift;;
        --archive)            FLAG_KEYS+=(archive);            shift;;
        --meta)               FLAG_KEYS+=(meta);               shift;;
        -h|--help)
            # BEFORE the -*) arm: --help is a `-*` token, and refusing it with
            # exit 2 would be a regression the refusal introduced rather than a
            # defect it fixed (). Help exits 0.
            argv_strict_help "$(basename "$0")" "<at least one filter>" \
                "$_ACCEPTED_FLAGS";;
        -*)
            # REFUSE (). Every unrecognized flag used to land in a
            # write-only PASSTHROUGH array (now deleted — it was never read), so
            # the query silently answered a BROADER question than the caller
            # asked and still exited 0. An over-broad READ returns rows and never
            # looks like a failure, which is why this has to fail loudly rather
            # than be documented. Caller scan first ( procedure):
            # 0 blocking callers, 0 unresolved, 0 hazards across 5 roots.
            argv_strict_refuse_unknown "$(basename "$0")" "$1" "$_ACCEPTED_FLAGS";;
        *)
            # KNOWN RESIDUAL, deliberately not fixed here ( ->
            # ). This wrapper takes ZERO positionals and a stray one is
            # still swallowed; the filter-required check below catches a
            # positional ALONE (rc=1, loud), so it cannot produce a wrong answer
            # by itself. The blast radius of refusing extra positionals fleet-wide
            # is unmeasured, and guard-1562 requires enumerating what would NEWLY
            # fire before shipping a refusal.
            shift;;
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
