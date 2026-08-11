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
GOAL_TITLE=""
TREE_NODES=""
ENTRY_TYPE=""
AS_OF=""

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
        --goal-title)
            # NOT forwarded to the daemon — consumed locally by the
            # commons-retrieval hook below, whose query token set is
            # tokens(category) | tokens(title). Dropping it would silently
            # narrow every commons match.
            GOAL_TITLE="${2-}"
            shift $(( $# >= 2 ? 2 : 1 ));;
        --tree-nodes)
            TREE_NODES="${2-}"
            shift $(( $# >= 2 ? 2 : 1 ));;
        --entry-type)
            # : restrict reasoning-bank results to this entry_type
            # (e.g. procedure). Forwarded as the entry_type query param.
            ENTRY_TYPE="${2-}"
            shift $(( $# >= 2 ? 2 : 1 ));;
        --as-of)
            # : bi-temporal point-in-time read. ISO-8601 instant T —
            # returns the record versions valid at T (valid_from<=T<valid_to)
            # across RB/guardrails/patterns/beliefs. Forwarded as as_of param.
            AS_OF="${2-}"
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
[ -n "$ENTRY_TYPE" ]        && QUERY+="&entry_type=$(rt_url_encode "$ENTRY_TYPE")"
[ -n "$AS_OF" ]             && QUERY+="&as_of=$(rt_url_encode "$AS_OF")"

# --- Commons-retrieval hook (Pattern B slot `commons-retrieval`) ----------
# Fires the domain's shared-commons producer for goal-scoped retrievals.
#
# WHY HERE AND NOT IN THE DIGEST (). Prose and a `Bash:` line inside
# a loaded digest are the SAME enforcement class: both need the model to elect
# to run them.  converted Step 4a from prose to a Bash: line and the
# fire rate stayed partial — this box logged 2 invocations while another box
# logged 0 across three Phase-4 executions. Folding the call in here means it
# rides the ONE retrieval election that already has enforcement behind it
# (iteration-close.sh writes a no-retrieval stub when retrieval-session.json
# is absent; phase-4-26-gate.py and the learning gate read it). That does not
# make the slot unconditional — no unconditional mid-Phase-4 script chokepoint
# exists, every step there is an LLM-elected Bash: line — but it removes the
# one election that had NO enforcement at all.
#
# ORDERING IS NOW STRUCTURAL, NOT DOCUMENTED. The daemon call above has
# already rewritten retrieval-session.json wholesale, so the producer's
# `commons_patterns` merge can no longer be clobbered by it. The digest
# previously had to warn about that ordering in prose.
#
# CONTRACT: stdout stays pure JSON for callers that parse it, so the
# producer's verdict is redirected to stderr (still visible to the caller,
# which is what Step 4a's "report the verdict" obligation needs).
# Fail-open — a commons miss enriches execution, it never gates it.
# `test -f` not `-x`: own-cloud materialization drops the exec bit (guard-1124).
_commons_draw() {
    [ -n "$GOAL" ] || return 0          # goal-scoped retrievals only
    [ -z "$READ_ONLY" ] || return 0     # observer safety: never write in reader/assistant
    local hook="${WORLD_DIR:-}/scripts/commons-retrieve.sh"
    if [ -n "${WORLD_DIR:-}" ] && [ -f "$hook" ]; then
        bash "$hook" --goal-id "$GOAL" --category "$CATEGORY" \
             --title "$GOAL_TITLE" --draw-top 2 >&2 || true
    else
        # Do NOT go silent. "No producer on this box" and "ran and drew
        # nothing" are different facts, and the digest's `|| true` rendered
        # them identically — which is how a never-wired slot looked healthy
        # for seven days. Under own-cloud a `test -f` miss can also just mean
        # the file was never materialized into this box's read-through cache
        # (guard-980), so the distinction is load-bearing (guard-2352).
        echo "[retrieve] commons-retrieval: no producer at \$WORLD_DIR/scripts/commons-retrieve.sh — slot fired, nothing drawn" >&2
    fi
    return 0
}

rc=0
rt_call GET /v1/retrieve --query "$QUERY" || rc=$?

case $rc in
    0) _commons_draw; exit 0;;
    2)
        # Daemon answered with HTTP 4xx/5xx; body already on stderr.
        exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            rt_call GET /v1/retrieve --query "$QUERY" || rc=$?
            if [ "$rc" = "0" ]; then _commons_draw; exit 0; fi
        fi
        rt_no_daemon_error "retrieve.sh";;
    *)
        exit $rc;;
esac
