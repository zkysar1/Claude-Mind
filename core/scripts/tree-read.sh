#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Read tree data — daemon-aware wrapper.
#
# Migrated for Phase B PR 4. Daemon path: rt_call /v1/tree/read.
#
# Simple lookups (--node, --path, --ancestors, --children, --leaves,
# --leaves-under, --stats, --child-path, --summary, --maintenance) are
# served by the daemon. Computational flags (--validate,
# --decompose-candidates, --redistribute-candidates, --distill-candidates,
# --active-content) and the legacy --find shorthand go direct to python —
# they run cross-file logic that isn't yet daemon-safe, or have a
# dedicated endpoint (tree-find-node.sh for --find).
#
# NOTE (authoritative-store-blind limitation): --validate checks the LOCAL
# mirror (os.path.exists) only, so on a remote-synced backend it is blind to
# index->authoritative-store-absent bodies — both index-body desync and the
# never-pushed-at-risk class. The authoritative-store-aware complement is
# core/scripts/tree-body-presence-audit.py (remote-backend-only; backend.stat
# HEAD), run on cadence via recurring goal . See rb-4089 for the
# single-box coverage asymmetry.
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

source "$CORE_ROOT/scripts/_argv_strict.sh"

# ONE literal, shared by the help text and the refusal message — never two
# copies (see argv_strict_refuse_unknown's header in _argv_strict.sh).
_ACCEPTED_FLAGS="--node <key> | --path <key> | --ancestors <key> | --children <key> | --leaves | --leaves-under <key> | --child-path <parent> <slug> | --stats | --summary | --maintenance | --by-l1 | --find <text> | --validate | --decompose-candidates | --redistribute-candidates | --distill-candidates | --active-content <key>"

# PASSTHROUGH IS LIVE HERE — do NOT delete it. Unlike the sibling read wrappers
# in this rollout (pipeline-read.sh:57, aspirations-read.sh), whose PASSTHROUGH
# arrays had no reader and were removed, this one IS the argv handed to tree.py
# on the FORCE_FALLBACK branch below. Deleting it would silently strip every
# argument from --validate / --by-l1 / --*-candidates / --active-content / --find.
declare -a PASSTHROUGH=()
declare -a FLAG_KEYS=()
NODE=""; PATH_KEY=""; ANCESTORS=""; CHILDREN=""; LEAVES_UNDER=""; FIND=""
CHILD_PATH_P=""; CHILD_PATH_S=""
BY_L1=0
FORCE_FALLBACK=0

# Value-arg pattern: "${2-}" + safe shift; see retrieve.sh for rationale.
# --child-path takes TWO values, so it gets a three-tier shift guard.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --node)         NODE="${2-}";        PASSTHROUGH+=(--node "${2-}"); shift $(( $# >= 2 ? 2 : 1 ));;
        --path)         PATH_KEY="${2-}";    PASSTHROUGH+=(--path "${2-}"); shift $(( $# >= 2 ? 2 : 1 ));;
        --ancestors)    ANCESTORS="${2-}";   PASSTHROUGH+=(--ancestors "${2-}"); shift $(( $# >= 2 ? 2 : 1 ));;
        --children)     CHILDREN="${2-}";    PASSTHROUGH+=(--children "${2-}"); shift $(( $# >= 2 ? 2 : 1 ));;
        --leaves-under) LEAVES_UNDER="${2-}"; PASSTHROUGH+=(--leaves-under "${2-}"); shift $(( $# >= 2 ? 2 : 1 ));;
        --leaves)       FLAG_KEYS+=(leaves);      PASSTHROUGH+=("$1"); shift;;
        --stats)        FLAG_KEYS+=(stats);       PASSTHROUGH+=("$1"); shift;;
        --summary)      FLAG_KEYS+=(summary);     PASSTHROUGH+=("$1"); shift;;
        --maintenance)  FLAG_KEYS+=(maintenance); PASSTHROUGH+=("$1"); shift;;
        --by-l1)        BY_L1=1; PASSTHROUGH+=("$1"); shift;;
        --child-path)
            CHILD_PATH_P="${2-}"; CHILD_PATH_S="${3-}"
            PASSTHROUGH+=(--child-path "${2-}" "${3-}")
            shift $(( $# >= 3 ? 3 : ($# >= 2 ? 2 : 1) ));;
        --find)
            # Use tree-find-node.sh for richer matching; fall through here so
            # behavior parity with the pre-migration CLI is preserved.
            FIND="${2-}"; PASSTHROUGH+=(--find "${2-}")
            FORCE_FALLBACK=1
            shift $(( $# >= 2 ? 2 : 1 ));;
        # Computationally-heavy flags — fall through unconditionally.
        --validate|--decompose-candidates|--redistribute-candidates|--distill-candidates)
            FORCE_FALLBACK=1
            PASSTHROUGH+=("$1"); shift;;
        --active-content)
            FORCE_FALLBACK=1
            PASSTHROUGH+=("$1" "${2-}"); shift $(( $# >= 2 ? 2 : 1 ));;
        -h|--help)
            # BEFORE the -*) arm: --help is a `-*` token, and refusing it with
            # exit 2 would be a regression the refusal introduced rather than a
            # defect it fixed (). Help exits 0.
            # This REPLACES a genuinely unhelpful answer, measured before the
            # change: `tree-read.sh --help` was rc=1 "Error: at least one query
            # parameter is required." — the flag fell to the catch-all, set no
            # QUERY, and the caller was told nothing about what the flags are.
            # The `extra` slot carries the two facts the flag list cannot show
            # and that a caller most needs (both already documented at the top
            # of this file, where a caller reaching for --help never looks).
            argv_strict_help "$(basename "$0")" "<at least one query flag>" \
                "$_ACCEPTED_FLAGS" \
"  Two routing facts the flag list cannot show:
  * Most flags are served by the daemon, but --validate, --by-l1, --find,
    --active-content and the three *-candidates flags force a direct-python
    path that forwards its arguments to tree.py. Both paths are supported;
    the second is slower and runs cross-file logic.
  * --validate checks the LOCAL MIRROR only (os.path.exists), so on a
    remote-synced backend it is BLIND to index-entry-present/body-absent
    nodes. The authoritative-store-aware complement is
    core/scripts/tree-body-presence-audit.py (see rb-4089).";;
        -*)
            # REFUSE (). MEASURED on this box before the fix, and the
            # result is asymmetric across this wrapper's two paths — which is why
            # the refusal belongs HERE, at the wrapper, rather than being left to
            # the downstream parser:
            #   DAEMON path      `--node root --bogus-flag XVAL` was rc=0 and
            #     BYTE-IDENTICAL to `--node root`. The flag and its value were
            #     appended to PASSTHROUGH, which that path never reads, so the
            #     caller got a confident answer to a different question.
            #   FALLBACK path    `--validate --bogus-flag XVAL` was ALREADY
            #     rc=2, refused loudly by tree.py's argparse ("unrecognized
            #     arguments"), precisely because PASSTHROUGH *is* read there.
            # So half of this wrapper was already correct and the other half was
            # silent, with nothing at the call site to say which one you were on.
            # This arm makes both halves refuse, at the SAME rc=2 the fallback
            # path already returned — so no accepted invocation changes status.
            argv_strict_refuse_unknown "$(basename "$0")" "$1" "$_ACCEPTED_FLAGS";;
        *)
            # NON-FLAG tokens still flow to PASSTHROUGH deliberately: on the
            # FORCE_FALLBACK branch this array is tree.py's argv, and that parser
            # owns positional arity. Refusing them here would duplicate — and
            # could contradict — a check that already exists downstream.
            PASSTHROUGH+=("$1"); shift;;
    esac
done

source "$CORE_ROOT/scripts/_runtime.sh"

QUERY=""
[ -n "$NODE" ]         && QUERY="node=$(rt_url_encode "$NODE")"
[ -n "$PATH_KEY" ]     && { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="path=$(rt_url_encode "$PATH_KEY")"; }
[ -n "$ANCESTORS" ]    && { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="ancestors=$(rt_url_encode "$ANCESTORS")"; }
[ -n "$CHILDREN" ]     && { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="children=$(rt_url_encode "$CHILDREN")"; }
[ -n "$LEAVES_UNDER" ] && { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="leaves_under=$(rt_url_encode "$LEAVES_UNDER")"; }
if [ -n "$CHILD_PATH_P" ]; then
    [ -n "$QUERY" ] && QUERY+="&"
    QUERY+="child_path=$(rt_url_encode "$CHILD_PATH_P,$CHILD_PATH_S")"
fi
for key in "${FLAG_KEYS[@]+"${FLAG_KEYS[@]}"}"; do
    [ -n "$QUERY" ] && QUERY+="&"
    QUERY+="${key}=1"
done
if [ "$BY_L1" = "1" ]; then
    [ -n "$QUERY" ] && QUERY+="&"
    QUERY+="by_l1=1"
fi

# --by-l1 (S1) computes a full per-L1 walk — fall through to direct python
# until the daemon endpoint learns the param. Cheap on a cold path; safe
# default since the daemon would silently ignore unknown query params.
if [ "$BY_L1" = "1" ]; then
    FORCE_FALLBACK=1
fi

# Computational flags require direct python (not a fallback — these operations
# are not yet daemon-safe). This is a feature path, not a CLI fallback.
if [ "$FORCE_FALLBACK" = "1" ]; then
    # shellcheck disable=SC1091
    source "$CORE_ROOT/scripts/_paths.sh"
    cd "$PROJECT_ROOT"
    # shellcheck disable=SC1091
    source "$CORE_ROOT/scripts/_platform.sh"
    # shellcheck disable=SC2086
    exec python3 "$CORE_ROOT/scripts/tree.py" read \
        "${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}"
fi

if [ -z "$QUERY" ]; then
    echo "Error: at least one query parameter is required." >&2
    exit 1
fi

rc=0
rt_call GET /v1/tree/read --query "$QUERY" || rc=$?

case $rc in
    0) exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            rt_call GET /v1/tree/read --query "$QUERY" || rc=$?
            if [ "$rc" = "0" ]; then exit 0; fi
        fi
        rt_no_daemon_error "tree-read.sh";;
    *)
        exit $rc;;
esac
