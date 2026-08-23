#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# aspirations-release — daemon-aware wrapper (PR 9b).
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Positional goal_id, optional --source <world|agent> (default world)
#   3. POST /v1/aspirations/release?id=<goal_id>&source=<world|agent>
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

# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_argv_strict.sh"

# ONE literal, referenced by BOTH the --help arm and the refusal ().
_ACCEPTED_FLAGS="--source"

# --- Parse args -----------------------------------------------------------
GOAL_ID=""
# WHY --source EXISTS NOW (). The query below hardcoded `source=world`,
# and the `-*)` arm appended unknown flags to a PASSTHROUGH array that NO LINE OF
# THIS SCRIPT EVER READ — the  silent-swallow shape. So `--source agent`
# was accepted, discarded, and the request still went to the WORLD queue.
#
# That was harmless only while claim() refused the agent queue outright. It no
# longer does: the daemon claim endpoint honors `&source=agent` (,
# LIVE-verified 2026-08-07 — `id=<absent>&source=agent` answers "not found in
# agent queue", and `source=bogus` answers 400 invalid_source). A claim protocol
# with no matching RELEASE strands a claim on every recurring cadence goal, so
# this wrapper had to be able to SAY `agent` before the loop digest could drop its
# `IF source==world` release guard.
#
# The goal that landed this asserted "release() ALREADY supports source=agent ...
# so this is a digest change only, not an endpoint change." True of the daemon
# endpoint, FALSE here — the inverse of guard-2374 (a flag the wrapper accepts and
# the endpoint rejects). Measure the wrapper, not only the endpoint it fronts.
#
# DEFAULT IS "world", so every existing caller is byte-identical.
SOURCE_VAL="world"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source)
            # ARITY GUARD (). WAS `SOURCE_VAL="${2:-}"; shift 2`. With
            # --source as the FINAL argument there is no $2, so `shift 2` is out
            # of range and returns 1 — and under `set -e` that kills the script
            # THERE, before the world|agent validation below can say anything.
            # Measured: exit 1, ZERO BYTES on either stream. That is the worst
            # possible shape for a wrapper failure, because rc=1-and-silent is
            # exactly what the daemon transport path produces, so the caller
            # cannot tell a typo from an outage.
            #
            # Exit 2, not 1, and deliberately NOT matching the invalid-VALUE
            # check below (which exits 1 and is left alone as out of scope). A
            # missing flag value is an ARGV defect, the same class the two
            # argv_strict refusals own, and the whole reason that contract pins
            # rc == 2 is to separate "you invoked me wrong" from "the transport
            # failed". Reusing 1 here would re-create the ambiguity being fixed.
            if [ $# -lt 2 ]; then
                echo "Error: --source requires a value (world|agent)." >&2
                echo "  Accepted flags: $_ACCEPTED_FLAGS" >&2
                exit 2
            fi
            SOURCE_VAL="$2"; shift 2;;
        --source=*)
            SOURCE_VAL="${1#--source=}"; shift;;
        -h|--help)
            # BEFORE the -*) arm: --help is a `-*` token, and refusing it with
            # exit 2 would be a regression the refusal introduced rather than a
            # defect it fixed (). Help exits 0.
            argv_strict_help "$(basename "$0")" "<goal-id> [--source world|agent]" \
                "$_ACCEPTED_FLAGS";;
        -*)
            argv_strict_refuse_unknown "$(basename "$0")" "$1" "$_ACCEPTED_FLAGS";;
        *)
            # POSITIONAL CEILING (). WAS first-wins-rest-discarded, the
            # positional twin of the silent flag swallow  killed above.
            # This wrapper takes exactly ONE positional, so a second one is always
            # a mistake — and the mistake it invites is specific: passing the queue
            # name bare. Measured before the fix, same id, one token apart:
            #   <id> agent          -> "not found in world queue"   <- swallowed
            #   <id> --source agent -> "not found in agent queue"   <- meant
            # Same intent, opposite target, exit 0 on a real id either way.
            #
            # This got MORE dangerous when --source landed (), not less:
            # before it, there was no queue argument to get wrong. A flag's
            # existence teaches callers the wrapper has that dimension, so adding
            # one adds a bare-positional failure mode to the arm that ignores them.
            if [ -n "$GOAL_ID" ]; then
                argv_strict_refuse_extra_positional "$(basename "$0")" "$1" 1 "$_ACCEPTED_FLAGS"
            fi
            GOAL_ID="$1"
            shift;;
    esac
done

case "$SOURCE_VAL" in
    world|agent) ;;
    *)
        echo "Error: --source must be world or agent (got '${SOURCE_VAL}')." >&2
        exit 1;;
esac

if [ -z "$GOAL_ID" ]; then
    echo "Error: goal_id is required." >&2
    exit 1
fi

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY="id=${GOAL_ID}&source=${SOURCE_VAL}"
# Session identity ( outcome 5). The daemon warns when a release is
# invoked by a session that does NOT hold the claim — but it can only do that
# if the caller SAYS which session it is. Without this the guard is structurally
# dead, which is the ORIGINAL bug's shape (claims collided precisely because
# nothing session-scoped was ever transmitted). Best-effort: an empty value is
# omitted and the endpoint behaves exactly as before. MIND_SID is injected into
# every Bash call by bash-agent-inject.py.
if [ -n "${MIND_SID:-}" ]; then
    QUERY="${QUERY}&sid=$(rt_url_encode "$MIND_SID")"
fi

# --- in_flight clear () -----------------------------------------
# SYMMETRY, not a new mechanism. aspirations-claim.sh SETS the busy signal
# (L257-282, via team-state-in-flight.sh); release cleared NEITHER surface, so a
# released agent kept reading busy to every partner — and because
# aspirations-select DROPS a partner's in_flight goal_id from its candidates, a
# stale row can suppress the released goal from the very partner the release was
# meant to hand it to. Measured twice on cc-03 (echo, 2026-08-06) on two
# different goals hours apart.
#
# THERE ARE TWO SURFACES AND THE FILING NAMED ONE. `in_flight` is AGENT-keyed and
# reducer-owned (-d: a worker Body must never stamp it, or it clobbers
# the reducer's live row). A worker Body's busy signal is the SID-keyed
# `in_flight_bodies.<sid>` row (). Clearing only the first leaves every
# worker release stranded — measured 2026-08-07 on cc-08, where alpha carried an
# in_flight_bodies entry for  claimed 2026-08-05T22:31, ~30h stale.
#
# BOTH BRANCHES ARE GOAL-CONDITIONAL, which is the whole safety argument. The
# agent-keyed clear passes --if-goal, the compare-and-swap that wrapper already
# implements (guard-2474 clause 2), so it no-ops unless the row names THIS goal —
# that makes it safe to call unconditionally, including from a worker whose
# in_flight is legitimately someone else's. The body branch has NO such CAS
# (clear-body-row.sh takes only --agent/--sid and its arg loop ends in `*) shift;;`,
# so an invented --if-goal would be SILENTLY DISCARDED — guard-1776), so the
# ownership test is done HERE by reading the row's goal_id first. A wrong clear
# misroutes work; a missed clear self-heals on the next close.
#
# FAIL-OPEN THROUGHOUT: the daemon release has already committed by the time this
# runs, so a team-state hiccup must never turn a successful release into a
# failure. Every call is `|| true` and quiet.
_clear_in_flight() {
    [ -n "${MIND_AGENT:-}" ] || return 0

    # Reducer surface — CAS-guarded by the wrapper itself.
    MIND_AGENT="$MIND_AGENT" bash "$CORE_ROOT/scripts/team-state-clear-in-flight.sh" \
        --agent "$MIND_AGENT" --if-goal "$GOAL_ID" >/dev/null 2>&1 || true

    # Body surface — ownership tested here, because the clearer has no CAS.
    [ -n "${MIND_SID:-}" ] || return 0
    _BODY_GOAL="$(MIND_AGENT="$MIND_AGENT" bash "$CORE_ROOT/scripts/team-state-read.sh" \
        --field "agent_status.${MIND_AGENT}.in_flight_bodies.${MIND_SID}.goal_id" \
        2>/dev/null | tr -d '"[:space:]')" || _BODY_GOAL=""
    if [ "$_BODY_GOAL" = "$GOAL_ID" ]; then
        MIND_AGENT="$MIND_AGENT" bash "$CORE_ROOT/scripts/team-state-clear-body-row.sh" \
            --agent "$MIND_AGENT" --sid "$MIND_SID" >/dev/null 2>&1 || true
    fi
    return 0
}

# --- iteration-checkpoint clear () -------------------------------
# SYMMETRY with the block above, and with claim. aspirations-claim.sh writes the
# checkpoint (L262-266 `loop-state-save.sh init`); nothing ever cleared it, so
# `loop-state-save.sh clear` shipped implemented, documented in its own header,
# and with ZERO production call sites — the checkpoint was only ever corrected by
# the NEXT claim's init.
#
# WHAT THAT COSTS, twice observed. Between a release and the next successful
# claim the checkpoint asserts a goal that is not in flight, and the
# SessionStart:compact hook reads it and emits "CRITICAL: Your in-flight goal is
# <id> ... Resume execution on THIS goal. Do NOT re-run goal-selector.sh" — an
# instruction that is wrong in every clause for a released goal and that forbids
# the corrective action by name. alpha hit it via DEFER (cc-04, );
# zeta hit it via explicit release-then-skip (cc-02, ).
#
# WHY HERE and not on each exit path: measured on this goal, the DEFER path
# (aspirations-execute L234-244) does call release, and the SKIP path (L296,
# "mark goal skipped, GOTO Phase 7") does NOT. So release is the right chokepoint
# for the paths that reach it and is provably not the only one — which is why the
# read-side cross-check in postcompact-restore.py was widened in the same change
# rather than instead of this. Neither alone covers both paths.
#
# GOAL-CONDITIONAL, exactly like the body branch above and for the same reason:
# an unconditional clear would unlink an anchor naming a DIFFERENT, live goal.
# The compare-and-swap lives in `loop-state-save.sh clear --if-goal`, NOT here:
# doing it in the caller means read -> pipe through python -> strip a trailing
# \r before comparing, and skipping that strip makes the whole function inert on
# Windows alone (the round-trip is text-mode; aspirations-claim.sh documents the
# same trap at its own ENSURE check). The single-writer already holds the parsed
# value, so it is the only place that should compare it.
#
# FAIL-OPEN: the daemon release has already committed by the time this runs. A
# stale checkpoint is recoverable — the next claim re-inits it, and the read-side
# cross-check in postcompact-restore.py now refuses to build a resume imperative
# on one. A release reported as failed is not recoverable.
_clear_iteration_checkpoint() {
    [ -n "${MIND_AGENT:-}" ] || return 0
    MIND_AGENT="$MIND_AGENT" bash "$CORE_ROOT/scripts/loop-state-save.sh" \
        clear --if-goal "$GOAL_ID" >/dev/null 2>&1 || true
    return 0
}

rc=0
RESPONSE="$(rt_call POST /v1/aspirations/release --query "$QUERY")" || rc=$?

case $rc in
    0)
        # shellcheck disable=SC2086
        printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
# : surface daemon warnings (e.g. a non-holder release) to stderr.
# Without this the endpoint-side guard is invisible to the caller — the
# warning would be computed, returned, and silently dropped here. Mirrors
# aspirations-complete-by.sh, which already forwards warnings this way.
for w in resp.get('warnings') or []:
    print(w, file=sys.stderr)
goal = resp.get('goal')
if goal is not None:
    print(json.dumps(goal, indent=2, ensure_ascii=False))
"
        _clear_in_flight
        _clear_iteration_checkpoint
        exit 0;;
    2)
        exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/aspirations/release --query "$QUERY")" || rc=$?
            if [ "$rc" = "0" ]; then
                # shellcheck disable=SC2086
                printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
# : surface daemon warnings (e.g. a non-holder release) to stderr.
# Without this the endpoint-side guard is invisible to the caller — the
# warning would be computed, returned, and silently dropped here. Mirrors
# aspirations-complete-by.sh, which already forwards warnings this way.
for w in resp.get('warnings') or []:
    print(w, file=sys.stderr)
goal = resp.get('goal')
if goal is not None:
    print(json.dumps(goal, indent=2, ensure_ascii=False))
"
                _clear_in_flight
                _clear_iteration_checkpoint
                exit 0
            fi
        fi
        rt_no_daemon_error "aspirations-release.sh";;
    *)
        exit $rc;;
esac
