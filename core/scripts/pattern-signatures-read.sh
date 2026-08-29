#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Read pattern-signature records — daemon-aware wrapper.
#
# Migrated for Phase B PR 4. Daemon path: rt_call /v1/pattern-signatures/read.
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# : shared strict-argv refusal helpers (uniform message contract).
# Sourced BEFORE _runtime.sh so a refusal cannot be masked by a daemon failure.
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_argv_strict.sh"

declare -a FLAG_KEYS=()
REC_ID=""

# Value-arg pattern: "${2-}" + safe shift; see retrieve.sh for rationale.
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            echo "Usage: pattern-signatures-read.sh (--id <id> | --all | --active | --summary | --count)"
            exit 0;;
        --id)
            REC_ID="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --all)      FLAG_KEYS+=(all); shift;;
        --active)   FLAG_KEYS+=(active); shift;;
        --summary)  FLAG_KEYS+=(summary); shift;;
        # --count: record count only (). See guardrails-read.sh.
        --count)    FLAG_KEYS+=(count);   shift;;
        *)
            # : this arm silently appended to a dead PASSTHROUGH
            # accumulator (fed the pre-2026-05-14 CLI fallback, read by nothing
            # since), so a mistyped filter vanished and the call answered the
            # WRONG population with rc=0 (the rb-245 authoritative-false-count
            # shape). Refuse loudly. Exit 2 per the _argv_strict.sh convention
            # (the daemon path exits 1, so tests need a distinct rc).
            argv_strict_refuse_unknown "pattern-signatures-read.sh" "$1" "--id <id> | --all | --active | --summary | --count";;
    esac
done

source "$CORE_ROOT/scripts/_runtime.sh"

QUERY=""
[ -n "$REC_ID" ] && QUERY="id=$(rt_url_encode "$REC_ID")"
for key in "${FLAG_KEYS[@]+"${FLAG_KEYS[@]}"}"; do
    [ -n "$QUERY" ] && QUERY+="&"
    QUERY+="${key}=1"
done

if [ -z "$QUERY" ]; then
    echo "Error: at least one filter is required (--id, --all, --active, --summary, --count)." >&2
    exit 1
fi
rc=0
rt_call GET /v1/pattern-signatures/read --query "$QUERY" || rc=$?

case $rc in
    0) exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            rt_call GET /v1/pattern-signatures/read --query "$QUERY" || rc=$?
            if [ "$rc" = "0" ]; then exit 0; fi
        fi
        rt_no_daemon_error "pattern-signatures-read.sh";;
    *)
        exit $rc;;
esac
