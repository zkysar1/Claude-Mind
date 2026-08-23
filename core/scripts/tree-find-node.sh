#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Find tree nodes matching a text query — daemon-aware wrapper.
#
# Migrated for Phase B. Hot path: rt_call /v1/tree/find-node.
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve -------------------------------------------
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
_ACCEPTED_FLAGS="--text <query> | --find <query> | --top <n> | --leaf-only"

# --- Parse args -----------------------------------------------------------
TEXT=""
TOP=""
LEAF=0

# Value-arg pattern: "${2-}" + safe shift handle the case where the user
# passes a flag with no following value. See retrieve.sh for the rationale.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --text|--find)
            TEXT="${2-}"
            shift $(( $# >= 2 ? 2 : 1 ));;
        --top)
            TOP="${2-}"
            shift $(( $# >= 2 ? 2 : 1 ));;
        --leaf-only)
            LEAF=1
            shift;;
        -h|--help)
            # BEFORE the -*) arm: --help is a `-*` token, and refusing it with
            # exit 2 would be a regression the refusal introduced rather than a
            # defect it fixed (). Help exits 0.
            argv_strict_help "$(basename "$0")" "[no positionals]" \
                "$_ACCEPTED_FLAGS";;
        -*)
            # REFUSE (). This arm used to append the unknown flag to
            # PASSTHROUGH on the strength of a comment reading "Unknown flag
            # passes through to the fallback so argparse can surface a canonical
            # error". BOTH halves were false: PASSTHROUGH had NO READER anywhere
            # in this file (it was appended in four places and consumed in none —
            # the daemon path builds QUERY from TEXT/TOP/LEAF alone), and the
            # "fallback" it names was DELETED in the 2026-05-14 daemon-only
            # cutover, so there has been no argparse to surface anything for
            # three months. Measured on this box before the fix:
            # `--text session --bogus-flag` returned rc=0, 1313 bytes of real
            # output, EMPTY stderr — a different question answered silently.
            argv_strict_refuse_unknown "$(basename "$0")" "$1" "$_ACCEPTED_FLAGS";;
        *)
            # POSITIONALS stay accepted-and-ignored, the same boundary the
            # aspirations-read.sh and pipeline-read.sh adoptions drew
            # (guard-1562: never ship a refusal without enumerating what would
            # newly fire). Unlike those wrappers this one loses nothing by it:
            # the required-arg guard below already exits 1 with
            # "Error: --text or --find argument is required." when a caller
            # passes a bare topic, so the two documented `tree-find-node.sh
            # {artifact_or_topic}` sites fail LOUDLY today and are unchanged by
            # this arm.
            shift;;
    esac
done

# --- Daemon path ----------------------------------------------------------
source "$CORE_ROOT/scripts/_runtime.sh"

if [ -z "$TEXT" ]; then
    echo "Error: --text or --find argument is required." >&2
    exit 1
else
    QUERY="text=$(rt_url_encode "$TEXT")"
    [ -n "$TOP" ] && QUERY+="&top=${TOP}"
    [ "$LEAF" = "1" ] && QUERY+="&leaf_only=1"

    rc=0
    rt_call GET /v1/tree/find-node --query "$QUERY" || rc=$?
fi

case $rc in
    0) exit 0;;
    2)
        # Daemon answered with HTTP 4xx/5xx; body already on stderr.
        exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            rt_call GET /v1/tree/find-node --query "$QUERY" || rc=$?
            if [ "$rc" = "0" ]; then exit 0; fi
        fi
        rt_no_daemon_error "tree-find-node.sh";;
    *)
        exit $rc;;
esac
