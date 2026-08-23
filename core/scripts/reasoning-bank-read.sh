#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Read reasoning-bank records — daemon-aware wrapper.
#
# Migrated for Phase B PR 4. Daemon path: rt_call /v1/rb/read.
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
CATEGORY=""
TAG=""
RECENT=""

# Value-arg pattern: "${2-}" + safe shift; see retrieve.sh for rationale.
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            echo "Usage: reasoning-bank-read.sh (--id <id> | --category <cat> | --tag <tag> | --recent [N] | --active | --universal | --summary)"
            exit 0;;
        --id)
            REC_ID="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --category)
            CATEGORY="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --tag)
            TAG="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --recent)
            # --recent may be bare (CLI default 10) or take an integer.
            if [ $# -gt 1 ] && [[ "$2" =~ ^[0-9]+$ ]]; then
                RECENT="$2"; shift $(( $# >= 2 ? 2 : 1 ))
            else
                RECENT="10"; shift
            fi;;
        --active)      FLAG_KEYS+=(active);    shift;;
        --universal)   FLAG_KEYS+=(universal); shift;;
        --summary)     FLAG_KEYS+=(summary);   shift;;
        *)
            # : this arm was `PASSTHROUGH+=("$1"); shift` — a dead
            # accumulator that fed the Python CLI fallback deleted in the
            # 2026-05-14 daemon cutover and has been read by NOTHING since. A
            # mistyped filter was silently swallowed and the call answered with
            # the filter MISSING: rc=0, well-formed JSON, WRONG population —
            # the rb-245 authoritative-false-count shape, on a wrapper the
            # loop's audits read through daily. Refuse loudly instead. Exit 2
            # SPECIFICALLY (the _argv_strict.sh convention): the daemon path
            # also exits 1, so a test pinning this refusal needs a distinct rc.
            argv_strict_refuse_unknown "reasoning-bank-read.sh" "$1" "--id <id> | --category <cat> | --tag <tag> | --recent [N] | --active | --universal | --summary";;
    esac
done

source "$CORE_ROOT/scripts/_runtime.sh"

QUERY=""
[ -n "$REC_ID" ]   && QUERY="id=$(rt_url_encode "$REC_ID")"
[ -n "$CATEGORY" ] && { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="category=$(rt_url_encode "$CATEGORY")"; }
[ -n "$TAG" ]      && { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="tag=$(rt_url_encode "$TAG")"; }
[ -n "$RECENT" ]   && { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="recent=${RECENT}"; }
for key in "${FLAG_KEYS[@]+"${FLAG_KEYS[@]}"}"; do
    [ -n "$QUERY" ] && QUERY+="&"
    QUERY+="${key}=1"
done

if [ -z "$QUERY" ]; then
    echo "Error: at least one filter is required." >&2
    exit 1
fi

_rb_call() {
    #  bounded-load tripwire, sibling of the guardrail-manifest budget
    # check (): --recent is prime's session-start RB load, deliberately
    # bounded by  to N full bodies — but the bound lived in prose with
    # no gate, so a shape regression (a wider population, or entries fattening
    # past the allowance) would ship silently to every session start of every
    # agent. When --recent is set the payload is captured and measured against
    # RECENT x 8192 B (~2.5x the 2026-08-20 mean entry of 2,916 B). WARN, never
    # refuse — stdout is emitted verbatim either way. Non-recent reads stream
    # straight through: a --universal read is ~21 MB and has no bound to check,
    # so never buffer it.
    if [ -n "$RECENT" ]; then
        local out
        out="$(rt_call GET /v1/rb/read --query "$QUERY")" || return $?
        local nbytes=${#out}
        local ceil=$(( RECENT * 8192 ))
        if [ "$nbytes" -gt "$ceil" ]; then
            echo "[reasoning-bank-read] BOUNDED-LOAD WARNING: --recent $RECENT returned $nbytes bytes, over the $ceil-byte allowance ($RECENT entries x 8192 B). Either entries fattened past ~2.5x the 2026-08-20 mean (2,916 B) or the recency bounding regressed to a wider population. Every session start pays this load — investigate before raising the allowance (g-115-4428)." >&2
        fi
        printf '%s\n' "$out"
        return 0
    fi
    rt_call GET /v1/rb/read --query "$QUERY"
}

rc=0
_rb_call || rc=$?

case $rc in
    0) exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            _rb_call || rc=$?
            if [ "$rc" = "0" ]; then exit 0; fi
        fi
        rt_no_daemon_error "reasoning-bank-read.sh";;
    *)
        exit $rc;;
esac
