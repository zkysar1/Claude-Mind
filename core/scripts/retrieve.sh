#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Unified context retrieval — daemon-aware wrapper.
#
# Decision #58 (supersedes #24): the daemon /v1/retrieve endpoint now serves
# BOTH the read-only path AND the counter-bump path. The direct-python branch
# that used to handle counter-bump was deleted — the 2026-05-14 cutover had
# already removed retrieve.py's argparse+main()+__main__, so `python3
# retrieve.py` exited 0 with no output and every autonomous retrieve silently
# returned nothing. There is now a single code path: daemon for everything.
#
# Observer safety preserved: auto-injects --read-only when session mode is
# reader or assistant. Mode detection reads <agent>/session/agent-mode
# directly to skip the ~700ms session-mode-get.sh subprocess (the original
# wrapper's biggest single cost on Windows).
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
# core/scripts/retrieve.sh → PROJECT_ROOT is two `..`s up.
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# Source _runtime.sh now (early) so rt_session_mode is available for the
# auto-inject below.
source "$CORE_ROOT/scripts/_runtime.sh"

# --- Auto-inject --read-only for reader/assistant mode --------------------
# MIND_AGENT comes from the bash-agent-inject PreToolUse hook. If it's unset
# (background processes, daemon-internal callers) rt_session_mode returns
# empty and we skip the auto-inject — autonomous is the safe default there.
if [[ " $* " != *" --read-only "* ]]; then
    mode_val="$(rt_session_mode)"
    if [[ "$mode_val" == "reader" || "$mode_val" == "assistant" ]]; then
        set -- "$@" --read-only
    fi
fi

# --- Parse args (single daemon path — read-only AND counter-bump) ---------
# Recognized flags become query params. Decision #58: there is no longer a
# direct-python branch; --read-only is now just one more query param, not a
# code-path selector. The auto-inject above already appended --read-only for
# reader/assistant mode, so READ_ONLY below reflects the EFFECTIVE mode.
CATEGORY=""
DEPTH=""
SUPP_ONLY=""
FULL_CONTENT=""
INCLUDE_FRAMEWORK=""
READ_ONLY=""
GOAL=""
TREE_NODES=""

# Value-arg helper: ${2-} uses empty when $2 is unset. Without this,
# `retrieve.sh --category` (no value) crashes under set -u with "unbound
# variable" instead of falling through to the endpoint's canonical error.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --category)
            CATEGORY="${2-}"
            shift $(( $# >= 2 ? 2 : 1 ));;
        --depth)
            DEPTH="${2-}"
            shift $(( $# >= 2 ? 2 : 1 ));;
        --supplementary-only)
            SUPP_ONLY=1
            shift;;
        --full-content)
            FULL_CONTENT=1
            shift;;
        --include-framework)
            INCLUDE_FRAMEWORK=1
            shift;;
        --read-only)
            READ_ONLY=1
            shift;;
        --quiet)
            # CLI-only stderr suppressor. The daemon returns JSON only (no
            # stderr summary), so this is a no-op — accept and drop it.
            shift;;
        --goal)
            GOAL="${2-}"
            shift $(( $# >= 2 ? 2 : 1 ));;
        --tree-nodes)
            TREE_NODES="${2-}"
            shift $(( $# >= 2 ? 2 : 1 ));;
        *)
            # Unknown arg — drop it (no direct-python passthrough exists now).
            shift;;
    esac
done

# --- Daemon path ----------------------------------------------------------
# _runtime.sh was sourced at the top (before the auto-inject block); the
# idempotency guard inside it skips re-sourcing _paths.sh on this hot path.

# Missing --category is required for the daemon path.
if [ -z "$CATEGORY" ]; then
    echo "Error: --category is required." >&2
    exit 1
fi

QUERY="category=$(rt_url_encode "$CATEGORY")"
[ -n "$READ_ONLY" ]         && QUERY+="&read_only=1"
[ -n "$DEPTH" ]             && QUERY+="&depth=$(rt_url_encode "$DEPTH")"
[ -n "$SUPP_ONLY" ]         && QUERY+="&supplementary_only=1"
[ -n "$FULL_CONTENT" ]      && QUERY+="&full_content=1"
[ -n "$INCLUDE_FRAMEWORK" ] && QUERY+="&include_framework=1"
[ -n "$GOAL" ]              && QUERY+="&goal=$(rt_url_encode "$GOAL")"
[ -n "$TREE_NODES" ]        && QUERY+="&tree_nodes=$(rt_url_encode "$TREE_NODES")"

rc=0
rt_call GET /v1/retrieve --query "$QUERY" || rc=$?

case $rc in
    0) exit 0;;
    2)
        # Daemon answered with HTTP 4xx/5xx; body already on stderr.
        exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            rt_call GET /v1/retrieve --query "$QUERY" || rc=$?
            if [ "$rc" = "0" ]; then exit 0; fi
        fi
        rt_no_daemon_error "retrieve.sh";;
    *)
        exit $rc;;
esac
