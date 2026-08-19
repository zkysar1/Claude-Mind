#!/usr/bin/env bash
# closure-evidence-write — the ONE implementation of "put the closure narrative
# on the goal record", shared by both loop orchestrators.
#
# WHY THIS FILE EXISTS (). Closure evidence is the `outcome_note`
# field on a goal record. Until now exactly one thing produced it on the close
# path: `iteration-close.sh do_verify` Step 3 (), which the REDUCER
# runs every iteration. A WORKER Body deliberately skips verify — worker-loop
# Phase 4 says so and names do_verify Step 3 by name — so no code produced a
# worker's evidence at all. Measured 2026-08-09 on : the live worker sat
# at 48/48 notes, which looks fine and is the trap — that rate was DISPOSITIONAL
# (one agent following the contract by hand), not mechanical. Nothing would have
# caught the next worker that skipped it, and arming any enforcement gate on
# outcome_note would have refused 100% of worker closures while passing reducer
# ones — manufacturing the very asymmetry  exists to prevent.
#
# WHY A SHARED SCRIPT AND NOT A SECOND COPY IN worker-loop. guard-2676 (the
# no-transcription contract): a worker capability is a scoped CALL into the
# shared component, never a restatement of its steps. Two copies of the
# never-clobber rule would drift the first time one side is edited, and nothing
# would fail when they did. Both orchestrators now call THIS.
#
# WHAT IS DELIBERATELY NOT CHANGED. This script writes evidence only: it does
# not clear team-state in_flight (-d: a second clear on the worker path
# would defeat the claimed_by_sid ownership test in worker_close_in_flight_clear.py),
# does not set status and does not close the goal. The STATUS write is a
# separate step — the reducer's do_verify, and since 2026-08-16 the worker's
# too (worker-loop Phase 4a calls `iteration-close.sh --phase verify` right after
# this producer, ordered so this rich narrative wins over do_verify's one-line
# write-if-absent note; ). state-update / learning-gate / spark /
# productivity-check remain reducer-only and this script touches none of them.
#
# CONTRACT
#   closure-evidence-write.sh --goal <id> --source <world|agent> \
#       (--summary <text> | --summary-file <path>) [--prefix <label>]
#       -> write-if-absent on a ONE-SHOT goal (never clobber); on a RECURRING
#          goal at achievedCount >= 2 the prior occurrence's note is SUPERSEDED
#          with a header naming its length and the run stamp (, and
#          see the block at the write site for why replace beats append).
#          ALWAYS exits 0 (see NON-FATAL below).
#   closure-evidence-write.sh --goal <id> --source <world|agent> --probe-only
#       -> prints the existing outcome_note (empty if absent/unknown), exit 0.
#
# NON-FATAL BY CONTRACT. On the reducer path the goal's status is already
# committed before this runs, so a non-zero exit would strand the close in a
# state the caller cannot read from the rc. On the worker path a hard failure
# here would abort a work unit whose real output already landed. Failures are
# ANNOUNCED on stderr with the exact recovery command, never swallowed and never
# fatal. Callers must not branch on the rc.
#
# FAIL-OPEN PROBE. Any error, unparseable payload, or missing goal yields the
# empty string. Empty means "unknown or absent", never "verified absent" — which
# is safe in the only direction that matters here: empty => write,
# non-empty => refuse to overwrite.
set -uo pipefail

# Skinny PROJECT_ROOT resolve, matching the sibling wrappers (no _paths.sh —
# this runs on the close path of every iteration).
_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_SELF/../.." && pwd)"
SCRIPT_DIR="$PROJECT_ROOT/core/scripts"

GOAL_ID=""
SOURCE=""
SUMMARY=""
SUMMARY_FILE=""
# The caller supplies its OWN bracketed prefix so each orchestrator's existing
# message shape survives verbatim — do_verify's lines were "[iteration-close]
# verify: ..." before this extraction and must stay byte-identical, since
# operators grep for them and the retry hint is copy-pasted from them.
PREFIX="[closure-evidence]"
PROBE_ONLY=0

# guard-1224: every value-taking arm uses `shift $(( $# >= 2 ? 2 : 1 ))`, never a
# bare `shift 2`. With the flag passed LAST and no value, `${2:-}` substitutes
# empty and `shift 2` FAILS at $#==1; this script sets `-uo pipefail` but not
# `-e`, so the failure is not fatal, $1 never advances, and the loop spins
# forever. Caught here by the repo-wide scanner test_shift2_argv_hang.py on the
# first full-suite run after this file was written — the 12 targeted tests in
# test_worker_closure_evidence.py all passed, because none of them passes a flag
# last with no value.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --goal|--goal-id) GOAL_ID="${2:-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --source)         SOURCE="${2:-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --summary)        SUMMARY="${2:-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --summary-file)   SUMMARY_FILE="${2:-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --prefix)         PREFIX="${2:-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --probe-only)     PROBE_ONLY=1; shift;;
        # : caller asserts a note may ALREADY have been written for
        # THIS occurrence, so an existing note is NOT prior-occurrence residue
        # and the recurring-supersede branch below must not fire. Set by
        # iteration-close.sh do_verify on the WORKER path, where worker-loop
        # Phase 3.9 writes the rich narrative seconds before Phase 4a calls
        # do_verify. Never set on the reducer path, where do_verify's write IS
        # the first write of the occurrence and superseding is correct.
        --no-supersede)   NO_SUPERSEDE=1; shift;;
        *)
            # Refuse rather than silently absorbing ( / : a
            # write-only PASSTHROUGH array is how a value meant for a flag gets
            # dropped while the command still exits 0).
            echo "$PREFIX closure-evidence-write: unknown argument '$1'" >&2
            exit 2;;
    esac
done

if [[ -z "$GOAL_ID" ]]; then
    echo "$PREFIX closure-evidence-write: --goal is required" >&2
    exit 2
fi

# --- probe -----------------------------------------------------------------
# Deliberately aspirations-query.sh --full, NOT aspirations-read.sh: read
# returns the WHOLE aspiration ( measured at 15 MB on 2026-08-08) and
# this runs on the close path of every iteration. --full projects outcome_note
# on a single-goal query.
#
# ONE call, and deliberately NO --source and NO --json (, merged in
# from origin/main while this file was being written). That wrapper never parsed
# either flag — both hit its catch-all arm and landed in a write-only PASSTHROUGH
# array nothing read — and as of 21c516981 it REFUSES unknown flags with rc=2
# instead of swallowing them. The endpoint is union-by-design (aspirations_query.py
# builds `sources` from world + agent unconditionally), so one invocation already
# covers both queues and the `for src in ${SOURCE:-world agent}` loop this
# replaced was running the identical query twice.
#
# THE ORDER MATTERED AND IT NEARLY INVERTED.  deliberately fixed the
# caller BEFORE landing the refusal, precisely because a refused query returns
# empty here and empty means "no note -> safe to write" — a refusal would have
# silently disarmed the never-clobber guard below. That care was defeated by the
# merge: this file was new and unconflicted, so it re-introduced the exact caller
# shape they had removed, invisibly. Extracting the probe is what makes this a
# ONE-PLACE port instead of a second silent re-introduction.
#
# --source is still a real flag on THIS script — it is forwarded to the WRITE
# (aspirations-update-goal.sh does parse it). It is only the QUERY that must not
# receive it. That asymmetry is deliberate, not an oversight.
#
# RESIDUAL, stated rather than hidden: 2>/dev/null on the query keeps a future
# unknown-flag refusal quiet, which is the same silence that let the original
# defect live. Kept to match 's own shape in iteration-close.sh rather
# than diverging (guard-2676); the real protection is that no unknown flag is
# passed. Keep the invocation on ONE line — shape-based test pins locate this
# call by source text and stop matching if it is reshaped (guard-2921).
# ONE query, TWO consumers. _probe_record emits a single metadata line followed
# by the raw note; _probe_note keeps its original contract by dropping line 1.
# The recurring metadata rides along on the query that already runs, so the
# close path pays for no second round trip ().
#
# FAIL-OPEN DIRECTION IS PRESERVED AND IS NOT SYMMETRIC. Any failure yields
# `recurring=0 achieved=0` plus an empty note. Empty note => write (as before).
# A present note with unreadable metadata => recurring=0 => REFUSE, i.e. exactly
# today's behaviour. So a broken probe can never turn the supersede branch on.
_probe_record() {
    bash "$SCRIPT_DIR/aspirations-query.sh" --goal-field id "$GOAL_ID" --full 2>/dev/null \
          | CEW_GID="$GOAL_ID" python3 -c '
import json, os, sys
try:
    rows = json.load(sys.stdin)
except Exception:
    sys.stdout.write("recurring=0 achieved=0\n")
    sys.exit(0)
gid = os.environ["CEW_GID"]
for g in rows or []:
    if g.get("id") == gid or g.get("goal_id") == gid:
        try:
            ach = int(g.get("achievedCount") or 0)
        except Exception:
            ach = 0
        sys.stdout.write("recurring=%d achieved=%d\n" % (1 if g.get("recurring") else 0, ach))
        sys.stdout.write(g.get("outcome_note") or "")
        break
else:
    sys.stdout.write("recurring=0 achieved=0\n")
' 2>/dev/null
    return 0
}

_probe_note() {
    _probe_record | tail -n +2
    return 0
}

if [[ "$PROBE_ONLY" -eq 1 ]]; then
    _probe_note
    exit 0
fi

# --- write -----------------------------------------------------------------
if [[ -n "$SUMMARY_FILE" ]]; then
    if [[ -r "$SUMMARY_FILE" ]]; then
        SUMMARY="$(cat "$SUMMARY_FILE")"
    else
        echo "$PREFIX ⚠ closure-evidence-write: --summary-file '$SUMMARY_FILE' is not readable; no note written" >&2
        exit 0
    fi
fi

# Guarded on non-empty SUMMARY so a caller passing none reaches none of this and
# closes exactly as before (guard-1423).
if [[ -z "$SUMMARY" ]]; then
    exit 0
fi

_record="$(_probe_record)"
_meta="$(printf '%s\n' "$_record" | head -n 1)"
_existing="$(printf '%s\n' "$_record" | tail -n +2)"

if [[ -n "$_existing" ]]; then
    _rec=0
    [[ "$_meta" == *"recurring=1"* ]] && _rec=1
    _ach="${_meta##*achieved=}"
    [[ "$_ach" =~ ^[0-9]+$ ]] || _ach=0

    # RECURRING SUPERSEDE (). Never-clobber is CORRECT for a one-shot
    # goal, where a pre-existing note can only mean the agent wrote the richer
    # artifact by hand before closing. It is WRONG for a recurring goal, because
    # the goal RECORD persists across occurrences: status flips back to pending
    # and lastAchievedAt is restamped, but outcome_note is never cleared. So
    # occurrence N's note is still on the record when N+1 closes, this guard
    # cannot tell that from the hand-written case, and every occurrence after
    # the first loses its evidence silently at rc=0 behind a message that reads
    # as correct behaviour.
    #
    # REPLACE, NOT APPEND, AND THAT CHOICE IS NOT FREE-HAND. guard-3626 says
    # never bare-set this field because on a recurring goal it is "accumulated
    # evidence of every prior cycle"; guard-3983 — measured 2026-08-16 over 85
    # recurring goals, and naming this file — says "Replace rather than append:
    # a goal at achievedCount 308 cannot carry 308 appended notes", with a
    # one-line header naming the run and the superseded length, the prior text
    # staying recoverable from git history of the queue file. The header is what
    # reconciles them: guard-3626's real concern is silent destruction, not
    # replacement. The header deliberately opens with '[' — guard-3626's second
    # clause records that aspirations-update-goal.sh parses a leading '-' as a
    # flag, so a '---' banner would die with 'unknown option'.
    #
    # THRESHOLD IS achievedCount >= 2, AND IT FAILS CLOSED. cmd_complete_by bumps
    # achievedCount BEFORE this runs on the reducer path (iteration-close.sh
    # ~L902 vs the evidence write at ~L1036), so >= 2 means "at least one PRIOR
    # occurrence exists". At the FIRST close (1) an existing note can only be
    # hand-written, so it is still refused — that is verification outcome 2.
    # On the WORKER path the order is inverted (worker-loop Phase 4a calls this
    # producer BEFORE `iteration-close --phase verify`, ), so the count
    # lags by one and superseding starts at the third occurrence instead of the
    # second. Deliberate: one conservative miss costs a deferred note, one
    # over-eager supersede costs a hand-written artifact.
    # : THE SUPERSEDE PREMISE IS FALSE ON THE WORKER PATH. The branch
    # below reasons that on a recurring goal an existing note can only be a
    # PRIOR-occurrence leftover, because status flips back to pending and
    # outcome_note is never cleared. True on the REDUCER path, where do_verify's
    # write is the first write of the occurrence. FALSE on the WORKER path:
    # worker-loop Phase 3.9 deliberately writes the rich narrative in THIS
    # occurrence, and Phase 4a then calls do_verify with a one-line --summary
    # seconds later. The branch cannot tell stale-prior from fresh-this-occurrence
    # and guesses stale, so it destroyed the richest artifact a worker produces.
    # Measured 3x in one session on cc-07 (4916 chars -> 563 on ;
    # 5454 -> 380 on ), each at rc=0 behind a message reading as
    # correct behaviour. Compounded by : the header's "recoverable from
    # git history" recovery route does not exist on own-cloud, so the loss is
    # silent AND irrecoverable.
    # The caller knows which path it is on; it says so with --no-supersede. This
    # errs toward the direction this file already declares safe ("one
    # conservative miss costs a deferred note, one over-eager supersede costs a
    # hand-written artifact") — a worker whose 3.9 did NOT run keeps a stale note
    # rather than risking destruction of a fresh one.
    if [[ "${NO_SUPERSEDE:-0}" -eq 1 && "$_rec" -eq 1 && "$_ach" -ge 2 ]]; then
        # DISTINCT marker, deliberately not reusing the never-clobber text
        # below: guard-2536 requires a negative assertion ("the note was NOT
        # superseded") to be paired with positive proof the path was REACHED,
        # and a test cannot distinguish "declined here" from "never ran" unless
        # this branch says so in its own words. STDOUT for the guard-772 reason
        # the supersede branch documents — a stderr-only notice is invisible to
        # a backgrounded or piped caller.
        echo "$PREFIX outcome_note on $GOAL_ID (${#_existing} chars) is THIS occurrence's note (--no-supersede) — recurring supersede DECLINED, note preserved."
        exit 0
    fi

    if [[ "$_rec" -eq 1 && "$_ach" -ge 2 ]]; then
        # IDEMPOTENCY IS PRESERVED, AND IT HAD TO BE RESTORED EXPLICITLY. The
        # refusal branch below gets it free ("a re-run finds the note it wrote
        # and declines") and this branch does not. recurring-close.sh documents
        # retrying a failed verify by name (its own L1107-1119), so a retry
        # would re-supersede the note this script just wrote and the header's
        # "superseded N chars" would be counting its own previous header.
        if [[ "$_existing" == *"$SUMMARY"* ]]; then
            echo "$PREFIX outcome_note on $GOAL_ID already carries THIS summary (${#_existing} chars) — not re-written (idempotent re-run)."
            exit 0
        fi
        SUMMARY="[closure-evidence] SUPERSEDES a prior-occurrence note of ${#_existing} chars — recurring occurrence achievedCount=${_ach}, superseded $(date +%Y-%m-%dT%H:%M:%S). Prior text recoverable from git history of the queue file (guard-3983).

$SUMMARY"
        # STDOUT, not stderr: this is a successful write, and guard-772 records
        # that a stderr-only notice is invisible to a backgrounded or piped
        # caller — which is exactly how the refusal below stayed unnoticed.
        echo "$PREFIX superseding prior-occurrence outcome_note on $GOAL_ID (recurring, achievedCount=${_ach}; ${#_existing} chars superseded)"
    else
        # WRITE-IF-ABSENT, NEVER CLOBBER — unchanged for one-shot goals. An agent
        # who authored a note by hand BEFORE closing wrote the richer artifact;
        # replacing it with a shorter summary would be a worse defect than the
        # one being fixed. So an existing note wins and the skip is ANNOUNCED
        # rather than silent. Idempotent by construction: a re-run finds the note
        # it wrote and declines.
        echo "$PREFIX outcome_note already present on $GOAL_ID (${#_existing} chars) — verify summary NOT written (never clobber). It is in the execution diary and the board." >&2
        exit 0
    fi
fi

if bash "$SCRIPT_DIR/aspirations-update-goal.sh" ${SOURCE:+--source "$SOURCE"} \
        "$GOAL_ID" outcome_note "$SUMMARY"; then
    echo "$PREFIX outcome_note written to $GOAL_ID (${#SUMMARY} chars)"
else
    echo "$PREFIX ⚠ outcome_note write FAILED for $GOAL_ID — the narrative is NOT on the record. Re-run: bash core/scripts/aspirations-update-goal.sh --source ${SOURCE:-world} $GOAL_ID outcome_note \"...\"" >&2
fi
exit 0
