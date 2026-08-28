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
OVERRIDE_STALE_SOURCE=""
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
        --override-stale-source) OVERRIDE_STALE_SOURCE="${2:-}"; shift $(( $# >= 2 ? 2 : 1 ));;
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
        # === stale-narrative-source gate () ===
        # Same predicate as iteration-close.sh's, at this script's own
        # --summary-file read: worker-loop calls HERE directly, so a gate wired
        # only into iteration-close would be inert on the worker path (the path
        # the incident was measured on). Refusal posture matches THIS script's
        # existing one for a bad --summary-file: say so loudly and write no
        # note, rc=0 — a provenance fault must not break the close itself.
        # Exit 3 is the ONLY refusal; any other non-zero means the gate could
        # not run (this script is STAGED into a tmp core/scripts by its own
        # tests, where the gate file is absent), and a gate that cannot run
        # must not silently stop every close. Fail OPEN, loudly.
        _sss_rc=0
        py -3 "$SCRIPT_DIR/stale-summary-source-gate.py" \
            --path "$SUMMARY_FILE" --goal "${GOAL_ID:-}" --source "${SOURCE:-}" \
            --caller "closure-evidence-write.sh:summary-file-read" \
            ${OVERRIDE_STALE_SOURCE:+--override-stale-source "$OVERRIDE_STALE_SOURCE"} \
            >/dev/null || _sss_rc=$?
        if [[ "$_sss_rc" -eq 3 ]]; then
            echo "$PREFIX ⚠ closure-evidence-write: refusing stale narrative source; no note written." >&2
            echo "$PREFIX   Re-write the narrative for THIS goal, or pass --override-stale-source \"<why>\"." >&2
            exit 0
        fi
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

# PROVENANCE MARKER (). The recurring supersede branch below needs to
# tell a PRIOR-occurrence note (safe to replace) from one an agent hand-wrote for
# THIS occurrence (must never be replaced), and the goal record carries NO signal
# for it — measured 2026-08-25 over all 98 recurring goals / 80 with a note:
# there is no outcome_note_set_at, `outcome_notes` is a plain string not a
# timestamped list, and the only time-ish fields (lastAchievedAt, last_modified,
# last_substantive_at) are all restamped by cmd_complete_by BEFORE this script
# runs, so none of them separates the two cases. The signal therefore has to be
# one we WRITE. Every write below appends this token, so its ABSENCE means "not
# written by this script" = hand-written (or pre-dating this change).
#
# TRAILING, NOT LEADING, AND THAT IS MEASURED. aspirations.py:2810 previews the
# note as `outcome_note[:300]`; a leading marker is charged against that window.
# The supersede header already costs ~200 of those 300 chars, so prepending a
# second banner would leave previews almost entirely boilerplate.
#
# FAILS CONSERVATIVE BY CONSTRUCTION. Every note written before this change is
# unmarked and is therefore protected, so superseding re-arms one occurrence
# later per goal.
#
# ⚠ THAT RE-ARM CLAIM WAS TRUE ONLY ON THE NOTE-ABSENT PATH, AND THIS FILE
# ASSERTED IT UNCONDITIONALLY FOR A DAY (). "The first close after
# this change writes a marked note and the NEXT one may supersede it" holds
# where NO note exists — the write-if-absent path reaches the stamp at the
# bottom of this file and marks it. Where a note is PRESENT, the decline branch
# below used to `exit 0` BEFORE that stamp, so the note could never acquire the
# marker, every later occurrence re-entered the same branch, and the goal was
# WEDGED PERMANENTLY: occurrence N's evidence dropped silently at rc=0 behind a
# message that reads like correct behaviour.
#
# Measured twice, independently, against world/aspirations.jsonl:
#   cc-08 2026-08-26 — recurring=90, note-absent=17, note-present=73, marked=0,
#                      wedged (unmarked, achievedCount>=2) = 63
#   cc-07 2026-08-26 — same file at 19,263,903 B / 2,978 goals parsed as a
#                      positive control: 90 / 17 / 73 / 0, wedged = 64
# The counts agree; the extra wedged goal is one that closed in between. Skewed
# to the HIGHEST-frequency goals —  (achievedCount 349), 
# (342),  (277),  (187, the inbound email-directive lane).
#
# THE FIX IS A SECOND, DISTINCT MARKER — NOT A BACKFILL OF THE FIRST. Stamping
# CE_AUTO_MARK on the decline path would be the smaller diff and it is the wrong
# one: that token's own text asserts "written ... by closure-evidence-write.sh",
# so putting it on a note this script did NOT write is a false provenance claim,
# and it would authorize destroying a genuine hand-written artifact one
# occurrence later. Measured on the same corpus: 41 of the 73 note-present goals
# carry this file's older "[closure-evidence]" prefix (so they ARE script
# output), and 32 do not. A single token cannot describe both populations
# honestly.
#
# CE_DEFER_MARK says only what is true — "provenance unknown, supersede DEFERRED
# at occurrence N, preserved" — and it carries the achievedCount it was stamped
# at. That count is what separates a NEXT OCCURRENCE (recorded N < current) from
# a SAME-OCCURRENCE RETRY (recorded N == current), which a bare presence test
# cannot do: recurring-close.sh documents retrying a failed verify by name, and
# a retry that superseded would destroy the artifact inside the very occurrence
# that just preserved it. Cost is exactly one deferred note per goal — the trade
# this file already declares cheaper ("one conservative miss costs a deferred
# note, one over-eager supersede costs a hand-written artifact").
CE_AUTO_MARK="[closure-evidence:auto]"
CE_DEFER_MARK="[closure-evidence:deferred]"

# ANCHORED, LITERAL MARKER MATCH ( outcome 3). The old discriminator
# was `"$_existing" != *"$CE_AUTO_MARK"*` — a bare substring test over the whole
# note, so a note that merely MENTIONS the token anywhere, in any prose, was
# misclassified as script-written and became eligible for destruction. That is
# not hypothetical: the worker that filed this goal quoted the token in its own
# close note to EXPLAIN the decline, which armed the test against both that
# evidence and a preserved 2026-06-30 artifact, and caught it only at read-back.
# Any diagnostic note about this mechanism arms the old test — including a
# regression test that stores a sample note inline.
#
# This matcher requires the token to START a line AND that line to carry the
# full written shape. `index()` is a LITERAL search, deliberately: the tokens
# contain `[` and `]`, and a regex-escaping matcher would have to escape them
# correctly in every caller — one missed escape silently turns the anchor into a
# character class that matches almost anything. Nothing here is escaped because
# nothing here is a pattern. Interval expressions ({4}) are avoided too; not
# every awk in the fleet supports them.
#
# Prints the achievedCount recorded on the marker's own line, or nothing when
# the marker is absent. Empty means absent — callers must not read it as zero.
_ce_marker_ach() {
    printf '%s\n' "$1" | awk -v tok="$2" '
        index($0, tok) == 1 &&
        index($0, " by closure-evidence-write.sh (achievedCount=") > 0 {
            if (match($0, /achievedCount=[0-9]+/)) {
                print substr($0, RSTART + 14, RLENGTH - 14)
                exit
            }
        }'
}

_record="$(_probe_record)"
_meta="$(printf '%s\n' "$_record" | head -n 1)"
_existing="$(printf '%s\n' "$_record" | tail -n +2)"

# HOISTED out of the `if [[ -n "$_existing" ]]` block below (). The
# provenance stamp at the write site needs `_rec` on the path where NO note
# exists yet — that is the FIRST close of a recurring goal, and it is precisely
# the write whose marker lets the NEXT occurrence recognise a prior-occurrence
# note. Left inside the block, `_rec` was unset on exactly that path, no marker
# was written, and the supersede branch could never re-arm.
_rec=0
[[ "$_meta" == *"recurring=1"* ]] && _rec=1
_ach="${_meta##*achieved=}"
[[ "$_ach" =~ ^[0-9]+$ ]] || _ach=0

if [[ -n "$_existing" ]]; then

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

    # WHICH OCCURRENCE STAMPED THE DEFERRAL (). Empty when absent —
    # never read empty as zero, which is why this is compared explicitly below
    # rather than defaulted.
    _defer_ach="$(_ce_marker_ach "$_existing" "$CE_DEFER_MARK")"
    [[ "$_defer_ach" =~ ^[0-9]+$ ]] || _defer_ach=""

    # May this note be superseded? TWO independent grants, and the second is the
    # one this goal added:
    #   1. it carries CE_AUTO_MARK on its own line  -> this script wrote it
    #   2. it carries CE_DEFER_MARK stamped at a STRICTLY EARLIER achievedCount
    #      -> a previous occurrence preserved it and warned that the next one
    #         may replace it, and that next occurrence is now here
    # A deferral stamped at the CURRENT count grants nothing: that is a retry of
    # the same occurrence, and recurring-close.sh documents retrying a failed
    # verify by name. Presence alone would let a retry destroy the artifact
    # inside the very occurrence that just preserved it.
    _may_supersede=0
    if [[ -n "$(_ce_marker_ach "$_existing" "$CE_AUTO_MARK")" ]]; then
        _may_supersede=1
    elif [[ -n "$_defer_ach" && "$_defer_ach" -lt "$_ach" ]]; then
        _may_supersede=1
    fi

    if [[ "$_rec" -eq 1 && "$_ach" -ge 2 && "$_may_supersede" -eq 0 ]]; then
        # RECURRING BUT HAND-WRITTEN (). Recurring and past the first
        # occurrence, so the branch below would have superseded — but the note
        # carries no CE_AUTO_MARK, so this script did not write it. It is either
        # an agent's hand-written note for THIS occurrence or a note pre-dating
        # the marker; both are richer artifacts than a one-line summary and both
        # must survive. This is the SILENT half of the defect: field-shrink-guard
        # only refuses below 25%, so a hand-written note merely longer than the
        # summary but under 4x used to be replaced with no warning anywhere.
        #
        # DISTINCT message text, deliberately not shared with the one-shot
        # never-clobber below: guard-2536 requires a negative assertion ("the
        # note was NOT superseded") to be paired with positive proof the path was
        # REACHED, and a test cannot tell "declined here" from "never ran" unless
        # this branch says so in its own words. STDOUT per guard-772.
        #
        # THE DECLINE NOW STAMPS, AND THAT IS THE WHOLE FIX (). This
        # branch used to `exit 0` here, which preserved the note correctly and
        # dropped THIS occurrence's evidence on the floor — permanently, because
        # nothing downstream could ever mark the note and every later occurrence
        # re-entered this same branch. Preserving the old artifact and recording
        # that a decision was made are not in tension: the note is rewritten as
        # ITSELF plus one stamped line.
        if [[ -n "$_defer_ach" && "$_defer_ach" -eq "$_ach" ]]; then
            # Already stamped at THIS occurrence: a retry. Write nothing at all
            # — re-stamping would append a second identical line every retry and
            # grow the note without bound.
            echo "$PREFIX outcome_note on $GOAL_ID (${#_existing} chars) already carries $CE_DEFER_MARK for occurrence ${_ach} — recurring supersede DECLINED (idempotent re-run), note preserved."
            exit 0
        fi
        echo "$PREFIX outcome_note on $GOAL_ID (${#_existing} chars) carries no $CE_AUTO_MARK — provenance unknown, recurring supersede DECLINED (achievedCount=${_ach}), note preserved and stamped $CE_DEFER_MARK so the NEXT occurrence may supersede."
        # ASCII-ONLY IN THE STAMPED LINE, for the reason the provenance stamp
        # below states in full: ${#_existing} counts BYTES under a non-UTF-8
        # locale, so a non-ASCII character here inflates the length the next
        # occurrence reports. This line is STORED; the prose above is printed.
        # : remember what the CALLER passed before we overwrite it.
        # The line below reassigns SUMMARY to the preserved note + stamp, so
        # from here on ${#SUMMARY} measures text the caller never supplied.
        # Without this the terminal success line reports a length that is true
        # of the wrong text (see the report branch at the bottom of this file).
        _CE_DROPPED_SUMMARY_LEN=${#SUMMARY}
        SUMMARY="$_existing

$CE_DEFER_MARK written $(date +%Y-%m-%dT%H:%M:%S) by closure-evidence-write.sh (achievedCount=${_ach}) - the note above predates the provenance marker or was hand-written, so THIS occurrence preserved it rather than superseding it. The NEXT occurrence (achievedCount > ${_ach}) MAY supersede it. Prior text is recoverable from world/.history/snapshots/aspirations.jsonl/; see g-115-7853."
        # Suppress the auto-mark at the bottom of this file. That token asserts
        # this script AUTHORED the note, and the note being written here is the
        # caller's preserved artifact with one line appended — stamping it would
        # re-tell the exact lie this branch exists to avoid.
        _CE_DEFER_STAMPED=1
    elif [[ "$_rec" -eq 1 && "$_ach" -ge 2 ]]; then
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
        # THE RECOVERY ROUTE IS NAMED BECAUSE IT IS THE MITIGATION, and until
        #  it named a route that does not exist here. The comment on
        # --no-supersede above already records that "the 'recoverable from git
        # history' route does not exist on own-cloud" — world/ is external and
        # gitignored, so on a STORAGE_BACKEND=own-cloud box git carries no
        # version of this file at all. It was still what this line, the one text
        # actually STORED in the superseded note, told every future reader.
        # The route that does exist was measured on cc-07 2026-08-26:
        # world/.history/snapshots/aspirations.jsonl/ held 5,309 versioned
        # snapshots, newest 2026-08-26T02-14-29, i.e. current to the minute on an
        # own-cloud box. Both are named rather than one swapped for the other —
        # git IS the route on a local-backend deployment, and this file ships to
        # all of them.
        SUMMARY="[closure-evidence] SUPERSEDES a prior-occurrence note of ${#_existing} chars — recurring occurrence achievedCount=${_ach}, superseded $(date +%Y-%m-%dT%H:%M:%S). Prior text recoverable from world/.history/snapshots/aspirations.jsonl/ (all deployments), or from git history of the queue file where world/ is git-tracked (guard-3983).

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

# Stamp provenance LAST, so the marker rides every note this script writes and
# the branch above can recognise its own work next occurrence. Appended after the
# idempotency compare deliberately: that test looks for the bare summary inside
# the existing note, which still matches once the marker is on the end.
# ASCII-ONLY IN THE MARKER TEXT, and that is not cosmetic. The supersede header
# reports the superseded length as ${#_existing}, which counts BYTES whenever the
# locale is not UTF-8 (it is unset in the loop's own environment). Every non-ASCII
# character this script writes INTO a note therefore inflates the "N chars" the
# next occurrence reports by its extra byte count -- measured here: one em-dash
# made a 255-character note report as 257. The rest of this file's prose may use
# em-dashes freely because it is printed, never stored; this one line is stored.
#
# RECURRING ONLY, and the narrowing is deliberate. The discriminator exists to
# serve the supersede branch, which only runs on recurring goals; a one-shot goal
# takes never-clobber and needs no marker. So a one-shot note still reaches the
# record BYTE-EXACT, which is a contract this suite already pins
# (test_writes_the_note_when_absent asserts the narrative is unaltered in
# transit). Marking every write would have bought nothing and broken that.
# NOT on the deferral path (). `_CE_DEFER_STAMPED` means SUMMARY is
# the CALLER'S preserved artifact with a deferral line appended, not this
# script's own narrative. CE_AUTO_MARK asserts authorship; adding it there would
# claim this script wrote a note it deliberately declined to touch, and would
# grant the next occurrence a supersede on the wrong evidence.
if [[ "$_rec" -eq 1 && "${_CE_DEFER_STAMPED:-0}" -eq 0 ]]; then
    SUMMARY="$SUMMARY

$CE_AUTO_MARK written $(date +%Y-%m-%dT%H:%M:%S) by closure-evidence-write.sh (achievedCount=${_ach:-0}) - absence of this line on a recurring goal means a human wrote the note, so it is never superseded; see g-115-7733."
fi

# CAPTURE the writer's output instead of letting it stream past. The refusal is
# an error-JSON the caller must READ to answer "did this actually fail?"
# (guard-1007), and the old code discarded it and then asserted an outcome it had
# no evidence for. Merged 2>&1 because the CLI path prints its refusal on stderr
# and the daemon path on stdout; dropping either would re-bury the one line that
# explains the exit code (guard-3662).
_upd_out="$(bash "$SCRIPT_DIR/aspirations-update-goal.sh" ${SOURCE:+--source "$SOURCE"} \
        "$GOAL_ID" outcome_note "$SUMMARY" 2>&1)"
_upd_rc=$?

if [[ "$_upd_rc" -eq 0 ]]; then
    # Re-emit verbatim: capturing must not silence the writer for callers that
    # were reading its record echo before this change.
    printf '%s\n' "$_upd_out"
    # : the DEFERRED path must not report as a plain write. There,
    # SUMMARY is the PRESERVED note plus the stamp -- so "outcome_note written
    # to X (N chars)" is true of text the caller never passed, and the caller's
    # own narrative was dropped. Measured 2026-08-27T16:50:06 (cc-07,
    # ): a 6,744-byte --summary-file was dropped and the run printed
    # "written to  (8265 chars)" -- 8265 being preserved+stamp.
    # iteration-close then compounded it with its own literally-true line, so
    # two success-shaped messages covered a dropped artifact. The decline itself
    # is CORRECT and stays (preserving a hand-written note is the whole point of
    # the branch, ); what changes is that the drop is now stated
    # rather than implied, and the dropped length is named so a caller can tell
    # its summary went nowhere. Emitter-located defect, fixed at the emitter
    # (guard-3299).
    if [[ "${_CE_DEFER_STAMPED:-0}" -eq 1 ]]; then
        echo "$PREFIX outcome_note on $GOAL_ID PRESERVED + stamped (${#SUMMARY} chars = prior note + $CE_DEFER_MARK). YOUR SUMMARY (${_CE_DROPPED_SUMMARY_LEN:-0} chars) WAS NOT WRITTEN -- this occurrence declined to supersede a note of unknown provenance. Re-run at the NEXT occurrence (achievedCount > ${_ach}), which may supersede; or write it yourself with aspirations-update-goal.sh if it must land now."
    else
        echo "$PREFIX outcome_note written to $GOAL_ID (${#SUMMARY} chars)"
    fi
else
    printf '%s\n' "$_upd_out" >&2
    _old_len="$(printf '%s' "$_upd_out" | sed -n 's/.*"old_len"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' | head -n 1)"
    [[ -z "$_old_len" ]] && _old_len="$(printf '%s' "$_upd_out" | sed -n 's/.*would shrink it from \([0-9][0-9]*\) chars.*/\1/p' | head -n 1)"

    if [[ "$_upd_out" == *field_shrink_blocked* || "$_upd_out" == *field-shrink-guard* ]]; then
        # NOT A FAILURE TO REPORT AS ONE ( defect 2, guard-5049). The
        # gate refused precisely BECAUSE a longer note is already on the record,
        # so "the narrative is NOT on the record" was false BY CONSTRUCTION here
        # — and the re-run the old message printed either failed identically or,
        # with --override-shrink, destroyed the very note the gate had just
        # saved. Both offered outcomes were wrong for the observed state. Say
        # what happened and prescribe nothing.
        echo "$PREFIX outcome_note NOT overwritten on $GOAL_ID — field-shrink-guard declined this ${#SUMMARY}-char summary because a LONGER note (${_old_len:-unknown} chars) is already on the record. THE NARRATIVE IS ON THE RECORD; NO ACTION IS NEEDED. Do NOT re-run with --override-shrink: that would replace the longer note with this summary (guard-5049). To see it: bash core/scripts/completed-not-closed-slate.sh --show $GOAL_ID --note-chars 400" >&2
    else
        # A genuine, unclassified failure. Still no bare re-run — verify FIRST,
        # because the same instruction that is right for an absent note is
        # destructive for a present one.
        echo "$PREFIX ⚠ outcome_note write FAILED for $GOAL_ID (rc=$_upd_rc) — reason above. VERIFY BEFORE WRITING: bash core/scripts/completed-not-closed-slate.sh --show $GOAL_ID --note-chars 400. Only if the read-back shows the narrative absent or truncated, write the FULL narrative (not a summary) with aspirations-update-goal.sh." >&2
    fi
fi
exit 0
