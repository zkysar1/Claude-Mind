#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# aspirations-claim — daemon-aware wrapper (PR 9b).
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Positional goal_id + optional agent_name + --cross-lane flag
#   3. POST /v1/aspirations/claim?id=<goal_id>&agent=<name>[&cross_lane=<reason>]
#   4. On 200, print goal JSON to stdout
#
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# Normalize --goal/--goal-id flag aliases → positional goal id (rewrites $@).
# SSOT for the dual-accept goal-id contract; verify-learning enforces that this
# wrapper sources the normalizer (12-wrapper coverage grep). Restored 2026-05-29
# — dropped by a prior daemon cutover, which silently broke dual-accept and the
# verify-learning normalizer-coverage check.
GOAL_NORMALIZE_TARGET=positional source "$CORE_ROOT/scripts/_goal-arg-normalize.sh"

# --- Parse args -----------------------------------------------------------
GOAL_ID=""
AGENT=""
CROSS_LANE=""
DEVIATION=""
declare -a PASSTHROUGH=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --cross-lane)
            CROSS_LANE="${2-}"
            PASSTHROUGH+=("$1" "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --source)
            # Accept-and-ignore for convention symmetry (). Many
            # skill digests (aspirations-select Chaining, aspirations-execute
            # Inputs, loop digest Phase 4) instruct callers to pass
            # `--source {world|agent}` to ALL downstream aspirations-*.sh.
            # claim has no per-source semantics — the daemon endpoint derives
            # source from the goal-id — but without this case the value
            # ("world" or "agent") fell through to the `-*` catch-all which
            # shifts only the flag, leaving the value to be parsed as a
            # positional agent_name. Result was a phantom claimed_by=world
            # row that needed three manual repair calls per occurrence.
            # Consume both flag + value here so the convention is honored.
            shift $(( $# >= 2 ? 2 : 1 ));;
        --deviation)
            # Scorer Sovereignty Layer B (): the sanctioned-deviation
            # code the scorer-verdict gate requires when claiming a goal that is
            # NOT the scorer's fresh top pick. Consumed here (NOT passed through)
            # — the daemon claim endpoint has no per-deviation semantics; the
            # gate below is the only consumer.
            DEVIATION="${2-}"
            shift $(( $# >= 2 ? 2 : 1 ));;
        -*)
            PASSTHROUGH+=("$1"); shift;;
        *)
            if [ -z "$GOAL_ID" ]; then
                GOAL_ID="$1"
            elif [ -z "$AGENT" ]; then
                AGENT="$1"
            fi
            PASSTHROUGH+=("$1"); shift;;
    esac
done

# Default agent from env
if [ -z "$AGENT" ] && [ -n "${MIND_AGENT:-}" ]; then
    AGENT="$MIND_AGENT"
fi

if [ -z "$GOAL_ID" ]; then
    echo "Error: goal_id is required." >&2
    exit 1
fi
if [ -z "$AGENT" ]; then
    echo "Error: agent_name is required (positional or via MIND_AGENT)." >&2
    exit 1
fi

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

# --- Scorer Sovereignty Layer B gate () -------------------------
# BEFORE the daemon claim POST, refuse an UNSANCTIONED divergence from the
# scorer's fresh top pick. The gate reads the per-agent scorer-verdict sidecar
# (written by goal-selector.write_scorer_verdict); claiming a goal that is NOT
# top_goal_id requires --deviation <code> from a closed enum. The gate is
# FAIL-OPEN (missing/stale/malformed verdict -> allow), so a broken selector
# never wedges claiming. exit 2 = refused (distinct from, but same
# "claim-refused, pick again" meaning as, the daemon conflict exit 2 below).
GATE_RC=0
# shellcheck disable=SC2086  # rt_python_launcher is intentionally word-split (`py -3` on Windows)
$(rt_python_launcher) "$CORE_ROOT/scripts/scorer-verdict-gate.py" \
    --agent "$AGENT" --goal-id "$GOAL_ID" --deviation "$DEVIATION" || GATE_RC=$?
if [ "$GATE_RC" = "2" ]; then
    exit 2
fi
# Any non-2 rc (0 allow / fail-open, or an unexpected gate error) proceeds —
# the gate must never block the claim on its own bug.

# --- Claim-announce board post () -------------------------------
# After a SUCCESSFUL claim, atomically announce it on the coordination board —
# the ONLY surface that survives cross-box store partitions (: on
# 07-09 alpha's aspirations/team-state writes never left cc-04, but its board
# posts did; the read-side  fix can only see claims that were POSTED).
# This folds the honor-system Phase-4 board-post.sh step into the claim itself
# so a claim can never land un-announced. Invariants:
#   - FAIL-OPEN: a post failure MUST NEVER fail the claim (the claim already
#     committed in the daemon) — log to stderr, return 0.
#   - Only the rc=0 SUCCESS paths call this; conflict/rejection (rc 2/1) never
#     reach here, so they post nothing.
#   - Agent-queue goals carry NO claim (claimed_by unset in the response) -> skip
#     the announce (single-agent access needs no coordination signal).
_post_claim_effects() {
    local goal_id="$1" agent="$2" response="$3"
    local extracted claimed_by title
    # Extract claimed_by + title[:60] from the claim response, tab-separated.
    # Fail-open on any parse error (empty -> skip).
    extracted="$(printf '%s' "$response" | $(rt_python_launcher) -c "
import json, sys
try:
    resp, _ = json.JSONDecoder().raw_decode(sys.stdin.read())
    g = resp.get('goal') or {}
    cb = (g.get('claimed_by') or '').strip()
    t = (g.get('title') or '').replace(chr(9), ' ').replace(chr(10), ' ')[:60]
    print(cb + chr(9) + t)
except Exception:
    print(chr(9))
" 2>/dev/null)" || true
    claimed_by="${extracted%%$'\t'*}"
    title="${extracted#*$'\t'}"
    # --- in_flight / current_focus stamp () ----------------------
    # Folds the honor-system `team-state-in-flight.sh` step (documented in
    # coordination.md as "Phase 4 claim, before board post") into the claim
    # itself — the same treatment  gave the board announce below,
    # for the same reason: LLM-executed pseudocode drifts. VERIFIED 2026-07-26:
    # the setter had ZERO callers anywhere in the codebase (grep), so every
    # write depended on an LLM remembering the step — and execution was UNEVEN,
    # not uniformly absent. zeta stamped correctly at 19:14; foxtrot never did
    # (current_focus frozen 2h+ on an already-yielded goal across six claims).
    # An UNEVEN signal is worse for consumers than an absent one: a null
    # in_flight cannot be distinguished from "partner genuinely idle", so the
    # three readers below silently mis-answer instead of failing loud — the
    # aspirations-select partner-claim filter,
    # goal-pickup-coordination-check's partner-in_flight-GATED uncommitted-
    # collision probe (guard-741), and _cross_agent_attribution_filter's
    # "Source 1" concurrent-work timestamps.
    # Deliberately NOT gated on claimed_by (unlike the announce): an
    # agent-queue goal carries no world claim, but the agent is genuinely
    # working and the working tree is shared per-box, so partners still need
    # the liveness + uncommitted-ownership signal. FAIL-OPEN — a stamp failure
    # must never fail a claim that already committed in the daemon.
    MIND_AGENT="$agent" bash "$CORE_ROOT/scripts/team-state-in-flight.sh" \
        --agent "$agent" --goal-id "$goal_id" \
        --title "${title:-$goal_id}" --phase 4 \
        >/dev/null 2>&1 \
        || echo "[aspirations-claim] WARN: in_flight stamp failed for ${goal_id} (claim still succeeded)" >&2
    # Only announce a REAL claim. Agent-queue no-claim (empty claimed_by) -> skip.
    [ -z "${claimed_by:-}" ] && return 0
    printf '%s' "Claiming ${goal_id}: ${title}" \
        | MIND_AGENT="$agent" bash "$CORE_ROOT/scripts/board-post.sh" \
            --channel coordination --type claim --tags "claim,${goal_id},${agent}" \
            >/dev/null 2>&1 \
        || echo "[aspirations-claim] WARN: claim-announce board post failed for ${goal_id} (claim still succeeded)" >&2
    return 0
}

QUERY="id=${GOAL_ID}&agent=${AGENT}"
# Session identity on the claim ( slice 1, ADDITIVE — records only,
# changes NO refusal behavior). Claims are identified by AGENT NAME alone, so
# two sessions of the SAME agent both "succeed" and neither is warned (observed
# live 2026-07-25: two sessions of one agent held the same world goal 16min
# apart; the second was one write away from creating duplicate credentials in an
# external service). Nothing session-scoped was even TRANSMITTED, so the
# endpoint could not tell them apart in principle. MIND_SID is injected into every Bash call by
# bash-agent-inject.py. Best-effort: an empty value is simply omitted, so a
# caller without it behaves exactly as before. Deliberately SID-only — this
# script does a skinny PROJECT_ROOT resolve and does NOT source _paths.sh (see
# the header), so reading the runner-token file would need a 6th inlined
# AGENTS_PARENT_DIR copy (CLAUDE.md tracks 5). If SID reuse across windows
# (--continue / --resume) later proves to matter here, add runner-token via the
# daemon side, which already has ctx.paths, rather than inlining a path here.
if [ -n "${MIND_SID:-}" ]; then
    QUERY="${QUERY}&sid=$(rt_url_encode "$MIND_SID")"
fi
if [ -n "$CROSS_LANE" ]; then
    ENCODED_CL="$(rt_url_encode "$CROSS_LANE")"
    QUERY="${QUERY}&cross_lane=${ENCODED_CL}"
fi

rc=0
RESPONSE="$(rt_call POST /v1/aspirations/claim --query "$QUERY" 2>&1)" || rc=$?

case $rc in
    0)
        # shellcheck disable=SC2086
        printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
#  fix: raw_decode tolerates stale-daemon stderr-leakage appended
# after the JSON body (rt_call 2>&1 merges streams). Re-emit residual to
# stderr to preserve daemon-staleness warning visibility.
_src = sys.stdin.read()
resp, _idx = json.JSONDecoder().raw_decode(_src)
_residual = _src[_idx:].strip()
if _residual:
    print(_residual, file=sys.stderr)
goal = resp.get('goal')
if goal is not None:
    print(json.dumps(goal, indent=2, ensure_ascii=False))
"
        _post_claim_effects "$GOAL_ID" "$AGENT" "$RESPONSE"
        exit 0;;
    2)
        # T2.2: parity with CLI cmd_claim exit code. cross_lane_refused -> exit 2.
        if echo "$RESPONSE" | grep -q '"cross_lane_refused"'; then
            printf '%s\n' "$RESPONSE" >&2
            exit 2
        fi
        printf '%s\n' "$RESPONSE" >&2
        exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback. Try one
        # auto-spawn, then fail loud. See .claude/rules/no-python-cli-fallback.md.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/aspirations/claim --query "$QUERY")" || rc=$?
            if [ "$rc" = "0" ]; then
                # shellcheck disable=SC2086
                printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
#  fix: raw_decode tolerates stale-daemon stderr-leakage appended
# after the JSON body (rt_call 2>&1 merges streams). Re-emit residual to
# stderr to preserve daemon-staleness warning visibility.
_src = sys.stdin.read()
resp, _idx = json.JSONDecoder().raw_decode(_src)
_residual = _src[_idx:].strip()
if _residual:
    print(_residual, file=sys.stderr)
goal = resp.get('goal')
if goal is not None:
    print(json.dumps(goal, indent=2, ensure_ascii=False))
"
                _post_claim_effects "$GOAL_ID" "$AGENT" "$RESPONSE"
                exit 0
            fi
        fi
        rt_no_daemon_error "aspirations-claim.sh";;
    *)
        exit $rc;;
esac
