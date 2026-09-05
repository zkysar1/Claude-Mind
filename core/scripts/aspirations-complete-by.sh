#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# aspirations-complete-by — daemon-aware wrapper (PR 9c).
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Parse --source / --key-finding / positionals
#   3. POST /v1/aspirations/complete-by with params as query string
#   4. On 200, print goal JSON to stdout (matches legacy CLI shape)
#
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Unknown-flag refusal (sourced BEFORE _runtime.sh on purpose) -----------
# . Sourced here rather than beside _runtime.sh so a box where the
# daemon fails to spawn still shows the REFUSAL instead of a transport error --
# a guard whose failure mode is masked by an unrelated error is not a guard.
source "$CORE_ROOT/scripts/_argv_strict.sh"

# The flag surface, in ONE place. The usage heredoc below interpolates this and
# the -*) arm passes it to the refusal, so the two can never drift apart.
_ACCEPTED_FLAGS="--source <world|agent> | --key-finding <text>"

# --- Usage (early exit: BEFORE the normalizer, the arg loop and any rt_call) --
# guard-3872: a usage probe against an arg-tolerant WRITE wrapper can BE the
# invocation (measured: `pipeline-archive.sh --help` performed a permanent
# prune). This wrapper requires a goal_id, so `--help` previously fell through
# as a bogus positional and POSTed it to the daemon, which rejected it
# read-only as goal_not_found -- safe only by luck, not by construction.
# This branch MUST stay above the arg loop and every rt_call so a help flag
# can never ride into a real close (guard-5459: on a passthrough wrapper a
# flag the inner layer handles-and-exits still rides into a real run).
# HELP AT ANY POSITION. The branch below tests $1 alone, which was harmless
# while unknown flags were silently swallowed -- `<id> --source world --help`
# just fell through as a positional. The -*) refusal this goal adds converts
# that latent asymmetry into a LIVE regression, which is exactly what
# test_help_works_after_an_accepted_flag caught on aspirations-add-goal.sh. So
# normalize a help flag found anywhere to $1 first, using that wrapper's shape.
for _a in "$@"; do
    case "$_a" in -h|--help) set -- "--help"; break;; esac
done
case "${1-}" in
    --help|-h)
        cat <<USAGE
Usage: aspirations-complete-by.sh <goal-id> [agent-name] [flags]

  Marks a goal completed with agent attribution. For a RECURRING goal this is
  the required close path: it atomically advances lastAchievedAt, achievedCount
  and currentStreak/longestStreak (the daemon refuses a direct status=completed
  write on a recurring goal).

  Accepted flags: ${_ACCEPTED_FLAGS}
    --source        queue to write to (default: world)
    --key-finding   one-line finding persisted on the goal record

  Any other argument is positional: first goal-id, then agent-name.
USAGE
        exit 0;;
esac

# Normalize --goal/--goal-id flag aliases → positional goal id (rewrites $@).
# SSOT for the dual-accept goal-id contract; verify-learning enforces that this
# wrapper sources the normalizer (12-wrapper coverage grep). Restored 2026-05-29
# — dropped by a prior daemon cutover, which silently broke dual-accept and the
# verify-learning normalizer-coverage check.
GOAL_NORMALIZE_TARGET=positional source "$CORE_ROOT/scripts/_goal-arg-normalize.sh"

# --- Parse args -----------------------------------------------------------
SOURCE_VAL="world"
KEY_FINDING=""
declare -a POSITIONAL=()
declare -a PASSTHROUGH_SOURCE=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source)
            SOURCE_VAL="${2-}"
            PASSTHROUGH_SOURCE=(--source "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --key-finding)
            KEY_FINDING="${2-}"
            shift $(( $# >= 2 ? 2 : 1 ));;
        -*)
            # Was swallowed by the *) arm below into POSITIONAL, where the next
            # token slides one slot left. POSITIONAL[0] is the GOAL-ID and [1]
            # the agent-name, so a typo'd flag closed a DIFFERENT goal, or closed
            # the right goal as the wrong agent, and exited 0. This wrapper
            # CLOSES GOALS -- the write side of the  class.
            argv_strict_refuse_unknown "$(basename "$0")" "$1" "$_ACCEPTED_FLAGS";;
        *)
            POSITIONAL+=("$1"); shift;;
    esac
done
set -- ${POSITIONAL[@]+"${POSITIONAL[@]}"}

GOAL_ID="${1:-}"
AGENT_VAL="${2:-${MIND_AGENT:-}}"

# Missing goal_id -> error
if [ -z "$GOAL_ID" ]; then
    echo "Error: goal_id is required." >&2
    exit 1
fi

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY="goal_id=$(rt_url_encode "$GOAL_ID")&source=$(rt_url_encode "$SOURCE_VAL")"
[ -n "$AGENT_VAL" ] && QUERY="${QUERY}&agent_name=$(rt_url_encode "$AGENT_VAL")"
[ -n "$KEY_FINDING" ] && QUERY="${QUERY}&key_finding=$(rt_url_encode "$KEY_FINDING")"
# Session identity ( outcome 5) — see aspirations-release.sh for why
# this is load-bearing rather than cosmetic. Best-effort; omitted when unset.
if [ -n "${MIND_SID:-}" ]; then
    QUERY="${QUERY}&sid=$(rt_url_encode "$MIND_SID")"
fi

rc=0
RESPONSE="$(rt_call POST /v1/aspirations/complete-by \
    --query "$QUERY")" || rc=$?

case $rc in
    0)
        # shellcheck disable=SC2086
        printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
for w in resp.get('warnings') or []:
    print(w, file=sys.stderr)
goal = resp.get('goal')
if goal is None:
    print(json.dumps(resp, indent=2, ensure_ascii=False))
else:
    print(json.dumps(goal, indent=2, ensure_ascii=False))
"
        exit 0;;
    2)
        exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/aspirations/complete-by \
                --query "$QUERY")" || rc=$?
            if [ "$rc" = "0" ]; then
                # shellcheck disable=SC2086
                printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
for w in resp.get('warnings') or []:
    print(w, file=sys.stderr)
goal = resp.get('goal')
if goal is None:
    print(json.dumps(resp, indent=2, ensure_ascii=False))
else:
    print(json.dumps(goal, indent=2, ensure_ascii=False))
"
                exit 0
            fi
        fi
        rt_no_daemon_error "aspirations-complete-by.sh";;
    *)
        exit $rc;;
esac
