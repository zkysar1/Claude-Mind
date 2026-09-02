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
_ACCEPTED_FLAGS="--source --reason --reason-kind"

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

# WHY --reason EXISTS (). Release was the ONE exit that recorded
# nothing about WHY a goal left an agent's hands — the schema carries
# defer_reason, skip_reason, last_shelve_reason and cross_world_reason, and had
# no released_* counterpart at all. So the measured negative produced at exactly
# the moment an agent discovers it cannot run a goal HERE was destroyed on the
# spot, and every later box re-derived it (observed: , claimed and
# released by two agents, signal generated twice and lost twice).
#
# EMPTY BY DEFAULT and omitted from the query when empty, so every existing
# caller is byte-identical — this must stay a pure addition to a write path the
# whole fleet uses. Note the flag is EXTENDED into _ACCEPTED_FLAGS rather than
# loosening the `-*)` refusal: that strict refusal is the  fix and is a
# feature, not an obstacle.
REASON_VAL=""

# WHY --reason-kind EXISTS (). --reason above captures the negative;
# this types it. The captured entry was {box, agent, reason, at} with `reason`
# as FREE PROSE, so any consumer wanting the locus subset had to CLASSIFY prose
# — the keyword-regex approach this goal has already falsified twice (echo's
# 60.8% bare-hostname match; zeta's 79-vs-85 bracketing correction).
#
# MEASURED 2026-09-02 (alpha, cc-10) over the 52 live reason strings: the
# over-matching locus regex returns 8, of which 3 are true locus — a 62.5%
# false-positive rate — and the under-matching one MISSES both Studio-gated
# rows, which name no host at all. The sharpest false positive is self-refuting:
# the row reading "this box can still run this goal ... NOT FOR LOCUS" MATCHES
# the locus regex.
#
# The releasing agent already KNOWS why it released. Asserting a token removes
# the classification step entirely, which is design caution (1) of 
# ("key on a MEASURED negative, never a hostname string match") satisfied for
# the first time: measured AND typed by the party that measured it.
#
# locus      — bound to a machine/host/checkout THIS box is not (another box can run it)
# capability — bound to a credential/identity/permission this agent lacks. NOT the
#              same as locus and must not be merged with it: every fleet box carries
#              the same IAM users, so only another PRINCIPAL changes the answer
#              (-b). A consumer conflating the two re-routes work to boxes
#              that also cannot run it.
# role       — bound to a role this Body is not (worker vs reducer)
# not-due    — a recurring goal that was not actually due
# progress   — a partial advance; work remains, no barrier
# other      — a real release that is none of the above
#
# EMPTY BY DEFAULT and omitted from the query when empty, so every existing
# caller stays byte-identical. Consumers MUST fail open: `kind` is absent on
# every row written before this flag, and absent means UNMEASURED, never
# "runnable nowhere" (design caution (2)).
#
# Kept in sync with RELEASE_REASON_KINDS in mind_api/src/endpoints/aspirations_write.py
# by test_release_reason_kind.py::test_wrapper_and_daemon_token_sets_agree — a
# shell/daemon constant pair drifts silently otherwise (guard-742/547 class).
REASON_KIND_VAL=""
_REASON_KINDS="locus capability role not-due progress other"

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
        --reason)
            # Same ARITY GUARD as --source above, and for the same reason
            # (): with --reason as the FINAL argument there is no $2,
            # so `shift 2` returns 1 and `set -e` kills the script silently at
            # rc=1 — indistinguishable from a transport failure. Exit 2 keeps
            # "you invoked me wrong" separate from "the daemon is unreachable".
            if [ $# -lt 2 ]; then
                echo "Error: --reason requires a value." >&2
                echo "  Accepted flags: $_ACCEPTED_FLAGS" >&2
                exit 2
            fi
            REASON_VAL="$2"; shift 2;;
        --reason=*)
            REASON_VAL="${1#--reason=}"; shift;;
        --reason-kind)
            # Same ARITY GUARD as --source/--reason above (): a missing
            # value must be an ARGV defect at rc=2, never a silent set -e death
            # at rc=1 that reads like a transport failure.
            if [ $# -lt 2 ]; then
                echo "Error: --reason-kind requires a value (${_REASON_KINDS// /|})." >&2
                echo "  Accepted flags: $_ACCEPTED_FLAGS" >&2
                exit 2
            fi
            REASON_KIND_VAL="$2"; shift 2;;
        --reason-kind=*)
            REASON_KIND_VAL="${1#--reason-kind=}"; shift;;
        -h|--help)
            # BEFORE the -*) arm: --help is a `-*` token, and refusing it with
            # exit 2 would be a regression the refusal introduced rather than a
            # defect it fixed (). Help exits 0.
            argv_strict_help "$(basename "$0")" "<goal-id> [--source world|agent] [--reason <why>] [--reason-kind locus|capability|role|not-due|progress|other]" \
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

# Out-of-vocabulary kinds are refused HERE, at the layer that can name the typo,
# exactly as --source is. An unrecognised token must never reach the store: the
# whole value of a typed field is that a consumer can trust the token, and one
# misspelled `locis` silently absent from every query is the failure mode a
# free-text field already had.
if [ -n "$REASON_KIND_VAL" ]; then
    case " $_REASON_KINDS " in
        *" $REASON_KIND_VAL "*) ;;
        *)
            echo "Error: --reason-kind must be one of: ${_REASON_KINDS// /|} (got '${REASON_KIND_VAL}')." >&2
            exit 1;;
    esac
    if [ -z "$REASON_VAL" ]; then
        # A kind with no reason would store a token with no evidence behind it.
        # The pair is the record; neither half is useful alone.
        echo "Error: --reason-kind requires --reason (the token types the reason; it does not replace it)." >&2
        exit 1
    fi
fi

# ── ADOPTION NUDGE () ───────────────────────────────────────────────
# ADVISORY ONLY. It never changes the exit code, never blocks the release, and
# never touches stdout (callers parse the JSON there).
#
# WHY IT LIVES HERE RATHER THAN IN THE CALLERS' PROSE. Ten prescriptive call
# sites tell an LLM to run this script (5 SKILL.md, the loop digest, two
# conventions, an iteration-close hint, consolidation-housekeeping). FIVE are on
# the hot-path SIZE BUDGET, so "also pass --reason-kind" would cost a
# size-budget-override each AND leave ten copies free to drift apart. guard-399
# settles the shape: write the bash baseline first and treat the LLM step as
# optional enrichment on top of it. This wrapper is the ONE funnel every call
# site passes through, so the nudge reaches callers that were never edited —
# including any written after this line, which is the half prose can never do.
#
# DELIBERATELY SCOPED TO "A REASON WAS WRITTEN". A bare release has no evidence
# to type, and is already discouraged on other grounds (), so firing
# there would put noise on the highest-volume path and train readers to ignore
# the line. When a caller has written a reason it has ALREADY done the thinking;
# naming the kind is one more token. That is the moment of maximum leverage and
# minimum noise, and it is why this is not simply "warn on every release".
#
# A caller running this under `2>/dev/null` loses the nudge (guard-3662 class).
# Accepted: stderr is the only channel that cannot corrupt the parsed stdout.
if [ -n "$REASON_VAL" ] && [ -z "$REASON_KIND_VAL" ]; then
    echo "Note: --reason given WITHOUT --reason-kind — this negative is stored as untyped prose." >&2
    echo "  A consumer wanting one subset must then classify text: measured 62.5% false-positive" >&2
    echo "  for locus over the live corpus (g-115-8163). That is what the typed token removes." >&2
    echo "  Prefer: --reason-kind <${_REASON_KINDS// /|}>" >&2
fi

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
# : omitted entirely when empty, so a release with no --reason sends
# a byte-identical query to what it sent before this flag existed. URL-encoded
# via the same helper as the sid above — a reason is free text and will contain
# spaces and `&`.
if [ -n "$REASON_VAL" ]; then
    QUERY="${QUERY}&reason=$(rt_url_encode "$REASON_VAL")"
fi
# : likewise omitted when empty, so a release with no --reason-kind is
# byte-identical to the pre-flag query. Validated above, so only a vocabulary
# token can reach the endpoint from this wrapper.
if [ -n "$REASON_KIND_VAL" ]; then
    QUERY="${QUERY}&reason_kind=$(rt_url_encode "$REASON_KIND_VAL")"
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
