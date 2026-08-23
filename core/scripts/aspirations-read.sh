#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Layer 1 — CLIENT: agent framework. See core/BOUNDARY.md.
# Read aspirations — daemon-aware wrapper.
#
# Migrated for Phase 2. The hot path is:
#   1. Resolve PROJECT_ROOT from $0 (trivial; no _paths.sh source)
#   2. Source _runtime.sh (~1ms; no _paths.sh, no _platform.sh)
#   3. rt_call /v1/aspirations/read via curl
#
# When daemon is unreachable, auto-spawns and retries once; fails loud if
# still down.
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve -------------------------------------------
# core/scripts/aspirations-read.sh → PROJECT_ROOT is two `..`s up.
# _runtime.sh sees these via shell-source inheritance.
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# Shared unknown-flag refusal (, the sweep  mandated).
# Sourced BEFORE _runtime.sh so the refusal is cheap and cannot be masked by a
# daemon failure.
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_argv_strict.sh"
# ONE literal, referenced by BOTH the --help arm and the refusal, so the two
# strings that must agree cannot drift apart ( fresh-eyes F-002).
_ACCEPTED_FLAGS="--source <world|agent> | --id <asp-id> | --limit <N> | --active | --active-compact | --summary | --archive | --stepping-stones | --meta | --blocked"

# --- Parse args -----------------------------------------------------------
SOURCE_VAL="world"
ASP_ID=""
LIMIT=""
declare -a FLAG_KEYS=()

# Value-arg pattern: "${2-}" + safe shift handle the case where the user
# passes a flag with no following value (e.g., `aspirations-read.sh --source`).
# Without this, set -u trips "unbound variable" at $2 expansion; with it,
# the empty value flows through to argparse for a canonical error message.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --source)
            SOURCE_VAL="${2-}"
            shift $(( $# >= 2 ? 2 : 1 ));;
        --active)            FLAG_KEYS+=(active);          shift;;
        --active-compact)    FLAG_KEYS+=(active_compact);  shift;;
        --summary)           FLAG_KEYS+=(summary);         shift;;
        --archive)           FLAG_KEYS+=(archive);         shift;;
        --stepping-stones)   FLAG_KEYS+=(stepping_stones); shift;;
        --meta)              FLAG_KEYS+=(meta);            shift;;
        --blocked)           FLAG_KEYS+=(blocked);         shift;;
        --id)
            ASP_ID="${2-}"
            shift $(( $# >= 2 ? 2 : 1 ));;
        --limit)
            LIMIT="${2-}"
            shift $(( $# >= 2 ? 2 : 1 ));;
        -h|--help)
            # BEFORE the -*) arm: --help is a `-*` token, and refusing it with
            # exit 2 would be a regression the refusal introduced rather than a
            # defect it fixed (). Help exits 0.
            argv_strict_help "$(basename "$0")" "[no positionals]" \
                "$_ACCEPTED_FLAGS";;
        -*)
            # REFUSE (). This arm used to append the unknown flag to
            # PASSTHROUGH and shift, on the strength of a comment reading
            # "Unknown flag — ignored by daemon, kept for completeness". Both
            # halves were false: PASSTHROUGH had NO READER anywhere in this file,
            # so nothing was kept and nothing reached the daemon — the flag was
            # simply dropped and the command answered a different question with
            # rc=0. Measured on this box before the fix: `--summary --bogus-flag`
            # returned rc=0, 3374 bytes of real output, EMPTY stderr.
            # This is the loop's busiest read path, and an over-broad READ is the
            # dangerous direction: a wrong write is caught by the mandated
            # read-back, while a wrong read exits 0 and never looks like failure.
            argv_strict_refuse_unknown "$(basename "$0")" "$1" "$_ACCEPTED_FLAGS";;
        *)
            # POSITIONALS are still accepted-and-ignored, deliberately. This
            # wrapper takes none, so any positional is already a caller error —
            # but refusing them is a WIDER blast radius than this goal measured
            # (guard-1562: never ship a refusal without enumerating what would
            # newly fire), and it is the same boundary  and the
            # pipeline-read.sh adoption drew. Documented here rather than hidden.
            shift;;
    esac
done

# --- Daemon path ----------------------------------------------------------
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY="source=${SOURCE_VAL}"
for key in "${FLAG_KEYS[@]+"${FLAG_KEYS[@]}"}"; do
    QUERY+="&${key}=1"
done
[ -n "$ASP_ID" ] && QUERY+="&id=${ASP_ID}"
[ -n "$LIMIT" ]  && QUERY+="&limit=${LIMIT}"

rc=0
rt_call GET /v1/aspirations/read --query "$QUERY" || rc=$?

case $rc in
    0) exit 0;;
    2)
        # Daemon answered with HTTP 4xx/5xx; body already written to stderr.
        exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback. Try one
        # auto-spawn, then fail loud. See .claude/rules/no-python-cli-fallback.md.
        if rt_try_autospawn; then
            rc=0
            rt_call GET /v1/aspirations/read --query "$QUERY" || rc=$?
            if [ "$rc" = "0" ]; then exit 0; fi
        fi
        rt_no_daemon_error "aspirations-read.sh";;
    *)
        exit $rc;;
esac
