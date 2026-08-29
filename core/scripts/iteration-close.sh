#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# domain-leak-exempt: cross-repo support code names AyoAI product repos (Roblox-Integration, Environment-Server) in comments documenting the cross-repo commit pattern; goal-completion observation comments cite real production metrics (jose 1.8x, RichmondKey 2x, BT failures 0) as audit-trail evidence of why a closing protocol exists. Both are documentation, not behavior — the script reads paths dynamically from local-paths.conf and is host-agnostic.
# iteration-close.sh — Collapse per-iteration obligation bookkeeping.
#
# HOT PATH: runs ≥3x per iteration of the autonomous loop. Any sub-command
# added here multiplies the per-iteration token cost — which is the whole
# reason this script exists. If a step can run as a single Bash, keep it
# here; if it requires LLM judgment, put it in the companion digest
# (core/config/iteration-close-digest.md), NOT here.
#
# Replaces the token-heavy Skill() reloads of:
#   Phase 5  : Skill(aspirations-verify)
#   Phase 8  : Skill(aspirations-state-update)
#   Phase 12 : Skill(aspirations-learning-gate)
# with a single bash script called three times per iteration. The LLM-irreducible
# residue (narrative compression, curator gate scoring, decision rule extraction,
# meta-learning signal, Q1/Q2/Q3 summaries) is documented in the companion digest
# (core/config/iteration-close-digest.md) for the LLM to read ONCE per session
# and reference at each phase.
#
# Phases (all idempotent / set -e / partial completion retained in checkpoint):
#   --phase verify           bookkeeping previously in aspirations-verify SKILL.md
#   --phase state-update     bookkeeping previously in aspirations-state-update SKILL.md
#   --phase learning-gate    bookkeeping previously in aspirations-learning-gate SKILL.md
#   --phase productivity-check  Item 2 — productivity-stop-gate.sh (runs after learning-gate)
#
# Args:
#   --phase <name>           required (one of the four above)
#   --goal <id>              required (except productivity-check)
#   --status <status>        required for verify (completed|blocked|skipped|...)
#   --source <world|agent>   required (except productivity-check which is agent-scoped)
#   --outcome <deep|routine> required for state-update and learning-gate
#   --summary "<text>"       optional; journal entry + dependent unblock + the
#                            goal record's outcome_note (g-115-5157). MULTI-
#                            PARAGRAPH IS EXPECTED — this field read "<one-line>"
#                            until 2026-08-08 while every real caller passed a
#                            full verify narrative, so the contract and reality
#                            disagreed and neither a caller nor a maintainer
#                            could tell which was intended (g-115-4208).
#   --summary-file <path>    optional; same destinations, read VERBATIM from
#                            disk. Mutually exclusive with --summary. PREFER
#                            THIS for anything containing backticks, $(...) or
#                            a bare $ — an inline --summary is a double-quoted
#                            shell argument, so those expand before this script
#                            runs and the prose is silently holed at rc=0.
#
# Returns exit 0 on success, non-zero on error. set -e means any sub-step failure
# aborts the phase — the checkpoint file retains phase_completed, and the
# existing crash-recovery path (Phase -1.4 + aspirations-graceful-stop) handles it.
#
# The LLM must still do the LLM-residue work documented in
# core/config/iteration-close-digest.md at each phase. This script is the bash half;
# the digest is the LLM half.

# INVARIANT: `set -o pipefail` is LOAD-BEARING. Every `cmd | python3 -c ... || echo WARN`
# pattern below depends on pipefail to propagate upstream failures through the pipeline
# so the `||` fallback fires. Removing pipefail silently disables every WARN breadcrumb
# on pipeline-based writes. Do not split this `set` line without preserving pipefail.
#
# INVARIANT: All Python invocations in this file use `python3` (never `py -c` or `py -3`).
# `python3` resolves via core/scripts/.python-shim/ once _paths.sh is sourced below. See
# core/config/conventions/python-invocation.md. Don't flip back to `py -c` — this file
# was swept to a single form specifically so mixed use doesn't return.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"
# _platform.sh converts MSYS /c/... paths to Windows C:/... paths on Git Bash
# so every inline `python3 -c "open(r'$path', ...)"` block resolves correctly. Without
# this, Python treats /c/ZakNoCloud/... as relative-to-drive-root and the open
# silently fails (exception caught, 'false' returned). g-001-132 discovered this
# when the new retrieval-stub write landed at C:\c\ZakNoCloud\... — the same bug
# had silently disabled the learning-gate pending-check for every Windows
# iteration prior. Sourcing here fixes both paths in one edit.
source "$SCRIPT_DIR/_platform.sh"
cd "$PROJECT_ROOT"

# _winpath <path>: portable path for `python3 <file>` invocations. The
# .python-shim routes python3 to Windows Python (needs C:/ paths); native
# Linux/macOS python3 needs the POSIX path. cygpath exists ONLY under MSYS/
# Cygwin, so it must be guarded — an UNGUARDED `cygpath -w` crashed
# do_state_update + do_productivity_check with rc=127 on a non-Windows box
# (cc-03 Linux transplant), emptying the path so `python3 ""` failed and
# loop_state counters + the maintenance sweeps silently stopped advancing.
# Mirrors _platform.sh's `command -v cygpath` guard. Any future
# `python3 <file-arg>` invocation in this file MUST route through this helper,
# never a raw `cygpath -w`. (g-328 cygpath cross-platform fix.)
_winpath() {
    if command -v cygpath >/dev/null 2>&1; then
        cygpath -w "$1" 2>/dev/null || printf '%s' "$1"
    else
        printf '%s' "$1"
    fi
}

# Dual-accept goal-id: rewrite --goal-id, --goal=<id>, --goal-id=<id> into
# the canonical --goal <id> before the strict parse loop below (which
# rejects anything else). See _goal-arg-normalize.sh.
GOAL_NORMALIZE_TARGET=--goal source "$SCRIPT_DIR/_goal-arg-normalize.sh"

# --------------------------- arg parse ---------------------------
PHASE=""
GOAL_ID=""
GOAL_STATUS=""
SOURCE=""
OUTCOME=""
SUMMARY=""
SUMMARY_FILE=""
OVERRIDE_STALE_SOURCE=""
NO_RETRIEVAL_APPLICABLE=""
# g-115-228: Quality inputs forwarded to state-update-audit.sh velocity (rb-428
# bash-consolidation-drift twin). Without these flags, state-update-audit.py's
# argparse defaults made compute_learning_value return 0.0 across 206/206 goals
# (improvement-velocity.yaml signal dead since week 17 ship). All four are
# optional: empty values forward as "not passed" so absent quality data
# preserves pre-fix behavior (no regression for legacy callers). LLM populates
# from in-turn observations per iteration-close-digest.md § STATE-UPDATE.
TREE_UPDATED=""
TREE_UPDATED_OVERRIDE=""
ARTIFACTS_COUNT=""
ENCODING_SCORE=""
FINDINGS_COUNT=""
OVERRIDE_UNCOMMITTED=""
OVERRIDE_MISSING_ARTIFACT=""
OVERRIDE_RESIDUAL=""
OVERRIDE_DOMAIN_SUITE=""

# g-284-04: Recovery instructions on non-zero exit. The trap below reads
# _CURRENT_PHASE (set by each do_* function at entry) and prints
# phase-specific recovery commands when the script exits with rc != 0.
# Goal: when a verify-rejection / state-update-failure / lock-conflict
# leaves the goal in an indeterminate state, the caller (or operator)
# sees the exact retry command — not a generic "exit 1".
_CURRENT_PHASE=""

# g-115-4096: read the LIVE goal record before asserting its state in a
# recovery message. The state-update/learning-gate recovery texts previously
# asserted "verify succeeded / goal is closed" UNCONDITIONALLY — false
# whenever the phase was rejected at entry validation (missing --goal/--source
# exits 2 AFTER _CURRENT_PHASE is set) with verify never having run. Measured
# cost (bravo, 2026-07-30, g-115-4084): a pending goal held a live claim ~40min
# because the operator trusted "No goal-status revert needed". A recovery
# message is read by an operator deciding what NOT to redo, so it must report
# what it READ (verify-before-assuming.md "Positive File-State Claims").
# Prints the live status string, or nothing when unreadable (no GOAL_ID,
# unparseable id, read failure). Error-path only (rc!=0), so the daemon read
# is off the hot path. Never returns non-zero (trap safety).
# Prints "<status>\t<recurring>\t<claim_held>" for the live goal record, where
# the last two are the literals "true"/"false". Prints nothing when unreadable.
#
# ONE read, three fields (g-115-5216). The record was ALREADY fully parsed here
# to extract `status`; `recurring` and `claimed_by` ride along on the same
# aspirations-read round-trip and cost nothing. Callers that need only the
# status keep using the narrow _probe_goal_status wrapper below, whose output
# shape is deliberately unchanged (guard-695 — a widened shared helper breaks
# strict-equality assertions in test code, and only on a full-suite run).
#
# WHY `claimed_by` AND NOT `lastAchievedAt`. The obvious "did this close
# actually happen" signal is a freshly-advanced lastAchievedAt, and that is the
# discriminator five independent confirmations on g-115-5216 converged on. It is
# wrong: guard-2197 measured recurring-precondition-sweep.py advancing
# lastAchievedAt on a PRECONDITION-SHELVED goal while never writing
# achievedCount, deliberately, so overdue_ratio does not inflate. A shelved goal
# and a genuinely-closed one are therefore indistinguishable at that field —
# which is exactly the case the warning must keep firing on. claimed_by carries
# no such ambiguity: aspirations_write.py L3592-3594 pops it in the recurring
# branch of complete-by BEFORE touching lastAchievedAt, and both g-115-5001
# incidents left the claim live (~19 and ~11 min).
_probe_goal_record() {
    local gid="${GOAL_ID:-}" asp src rec st
    [[ -z "$gid" ]] && return 0
    [[ "$gid" =~ ^g-([0-9]+)- ]] || return 0   # g-xw-* etc: aspiration id not derivable
    asp="asp-${BASH_REMATCH[1]}"
    # ${SOURCE:-world agent} unquoted on purpose: try the caller's source when
    # given, else probe both queues (a missing --source is a common way to land here).
    for src in ${SOURCE:-world agent}; do
        rec="$(bash "$SCRIPT_DIR/aspirations-read.sh" --source "$src" --id "$asp" 2>/dev/null \
              | PGS_GID="$gid" python3 -c '
import json, os, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
asp = data if isinstance(data, dict) else (data[0] if data else {})
for g in asp.get("goals", []):
    if g.get("id") == os.environ["PGS_GID"]:
        print("\t".join([
            g.get("status") or "",
            "true" if g.get("recurring") else "false",
            "true" if g.get("claimed_by") else "false",
        ]))
        break
' 2>/dev/null)" || rec=""
        # Gate the source loop on the STATUS field, not on the whole line: a
        # found-but-statusless goal now yields a non-empty "\tfalse\tfalse",
        # which would otherwise stop the loop early where it used to fall
        # through to the next queue.
        st="${rec%%$'\t'*}"
        if [[ -n "$st" ]]; then printf '%s' "$rec"; return 0; fi
    done
    return 0
}

# Narrow back-compat contract: prints the live status string alone, exactly as
# before g-115-5216. Kept as a wrapper so its two callers below (and any test
# that stubs it) see no change in shape (guard-695).
#
# RECONCILE NOTE (2026-08-13, cc-07): the g-115-5573 tri-state
# _probe_goal_recurring that lived here was RETIRED in the fork reconcile —
# _probe_goal_record carries `recurring` (and `claim_held`) in the SAME read,
# so a second aspirations-read round-trip bought nothing. Its load-bearing
# invariant — unknown must stay distinguishable from a clean "false", because
# "false" authorizes a REFUSAL — survives structurally: the record probe prints
# NOTHING unless a record with a non-empty status was found, so `rec == ""` is
# the unknown case and fails open, and a found record always carries a definite
# recurring boolean. (Its other caution — never read recurring from the LOCAL
# aspirations.jsonl cache via _probe_is_recurring, guard-980 — still stands and
# applies to the record probe's daemon-backed read, which is why neither helper
# touches the local file.)
_probe_goal_status() {
    local rec
    rec="$(_probe_goal_record)"
    [[ -z "$rec" ]] && return 0
    printf '%s' "${rec%%$'\t'*}"
    return 0
}

# g-115-5157: read the goal record's CURRENT outcome_note. Two live call sites
# (do_verify's never-clobber check; do_state_update's metric-gate input), which
# is why it is a helper and not inlined twice.
#
# Deliberately uses aspirations-query.sh --full rather than _probe_goal_status's
# aspirations-read.sh: read returns the WHOLE aspiration (asp-115 measured at
# 15 MB on 2026-08-08), and this runs on the close path of every iteration.
# --full projects outcome_note on a single-goal query.
#
# Fail-open by contract: any error, any unparseable payload, any missing goal
# yields the empty string. Callers must treat empty as "unknown or absent" and
# never as "verified absent" — the emptiness is load-bearing in do_verify only
# in the safe direction (empty => write; non-empty => refuse to overwrite).
_probe_goal_outcome_note() {
    local gid="${GOAL_ID:-}"
    [[ -z "$gid" ]] && return 0
    # DELEGATES to the shared helper (g-115-5158). The query shape and its
    # fail-open contract now live in exactly ONE place, because a WORKER Body
    # needs the same probe and worker-loop must call a shared component rather
    # than restate it (guard-2676, the no-transcription contract). Keeping the
    # function name means the two in-file consumers — this do_verify
    # never-clobber check and do_state_update's metric-gate input — are
    # unchanged at their call sites.
    bash "$SCRIPT_DIR/closure-evidence-write.sh" --goal "$gid" \
        ${SOURCE:+--source "$SOURCE"} --probe-only 2>/dev/null || true
    return 0
}

# Echo back the § STATE-UPDATE quality flags THIS invocation was given, so a
# printed retry line is copy-paste complete (g-115-3480). Without this, every
# retry line below re-ran state-update at argparse defaults, and the rerun hit
# the UNMEASURED advisory (~L1673) even though the original caller had passed
# correct flags — so the imp@k hole opened on the DEFAULT path after any
# state-update block, not only when a closer forgot. Values are already in
# scope; the verify branch has always echoed its own flags this way.
_quality_flag_suffix() {
    local s=""
    [[ "$TREE_UPDATED" == "true" ]] && s+=" --tree-updated"
    [[ -n "$ARTIFACTS_COUNT" ]] && s+=" --artifacts-count $ARTIFACTS_COUNT"
    [[ -n "$ENCODING_SCORE" ]]  && s+=" --encoding-score $ENCODING_SCORE"
    [[ -n "$FINDINGS_COUNT" ]]  && s+=" --findings-count $FINDINGS_COUNT"
    printf '%s' "$s"
}

# FORWARD PRECONDITION (g-115-5001) — on the SUCCESS path, check that verify
# actually closed the goal record before a later phase reports success over it.
#
# THE DEFECT, measured twice in one day (echo, hostname cc-03, uname -r
# 6.8.0-136-generic, 2026-08-05) from two OPPOSITE causes:
#   - g-315-532: verify NEVER RAN (the diary jumps from claim straight to
#     state-update). state-update, learning-gate and productivity-check all
#     reported success, loop_state counted the goal, a commit landed.
#   - g-115-4718, 56 min later: verify RAN and was REFUSED — the call omitted
#     --status, so do_verify exits 2 at its entry check before doing anything.
#     The same three phases again reported success.
# Both times the goal sat status=pending with a live claim (~19 and ~11 min) as
# a live goal-selector candidate, one step from re-executing already-committed
# work. Detection was luck both times.
#
# WHY THE EXISTING MACHINERY CANNOT CATCH IT: _probe_goal_status is called ONLY
# inside _print_recovery_instructions, which by construction runs on rc!=0. In
# both instances every phase returned 0, so nothing read the record. Each phase
# trusts that its predecessor ran and nothing verifies it forward.
#
# WARN, NOT REFUSE — and that narrowing is guard-2760, not timidity. Adding a
# consumer of a failure signal with a DESTRUCTIVE remedy (here: halting a close
# mid-sequence) requires evidence that a REVERSIBLE remedy is insufficient, and
# no loud warning has ever been tried. A refusal also has a live false-positive
# path: on own-cloud the record is read through a cache, so a verify that DID
# close the goal can still read stale and would block a legitimate close. The
# warning reaches the model — iteration-close.sh is invoked as a Bash tool call,
# so its stderr lands in tool output (unlike a non-blocking hook, guard-1680).
# If a warning proves insufficient in the field, THAT is the evidence guard-2760
# asks for, and escalating to a refusal becomes justified.
#
# THE PREDICATE IS NOT "not terminal", AND THIS IS THE CORRECTION THAT MATTERS.
# g-115-5001 proposed refusing when the live status "is not terminal". Reading
# the code falsifies that (guard-1719 — the goal's diagnosis was measured, its
# remedy was reasoned forward): do_verify legitimately accepts
# --status <completed|blocked|skipped>, and `blocked` is NOT in
# _goal_census.TERMINAL_STATUSES (= completed + skipped/expired/decomposed/
# superseded). A not-terminal predicate would false-fire on EVERY legitimate
# blocked close. So this gates on the two NOT-CLOSED statuses instead, which is
# exactly what both incidents exhibited and cannot misfire on any close verify
# can produce.
#
# FAIL-OPEN on an unreadable record, matching the "" branch the recovery block
# already models: assert neither direction. _probe_goal_status prints "" and
# returns 0 for an unset/unparseable goal id, an unreadable queue, and g-xw-*
# ids whose aspiration is not derivable — all of which must stay silent here.
#
# COST, measured rather than left for a future reader to wonder about (cc-08,
# uname -r 6.8.0-136-generic, own-cloud, live daemon): the probe is one
# aspirations-read round-trip at ~1.6-2.0s, so ~4s added per full close. It is
# ONE read, not two — _probe_goal_status only falls back to scanning both queues
# when SOURCE is unset, and both phases validate --source at entry above. Weigh
# that against what it guards: each incident left already-committed work (~45min
# in g-315-532) sitting re-executable in the selector. 10 test files invoke these
# phases and now pay the read too.
# g-115-5001 established the forward READ (unconditional, on the success path,
# at both close phases). Two successors evolved it IN PARALLEL, neither aware
# of the other (fork window; sig-29 class), merged at the 2026-08-13 cc-07
# reconcile:
#   - g-115-5573 (local) escalated warn -> REFUSAL: warning and proceeding
#     still let the loop count the goal closed, advance its counters and score
#     productivity over a record that stayed open — the goal then re-entered
#     the selectable pool carrying no outcome_note and was a candidate for
#     re-execution (observed: g-115-5104). That field evidence is exactly what
#     the original WARN-NOT-REFUSE decision said would justify escalation
#     (guard-2760's own clause).
#   - g-115-5216 (origin) kept the warn but fixed its recurring
#     false-positive: ONE _probe_goal_record read (status + recurring +
#     claim_held) instead of two round-trips, and a claim_held discriminator
#     separating a healthy recurring close from an abandoned one.
# The function below keeps 5573's refusal and 5216's probe + carve-out.
#
# THE FAIL LADDER IS THE DESIGN. Refuse ONLY on independent CLEAN positive
# reads; every unknown proceeds. A close phase that wedges on a transient
# store blip is worse than the miscount it prevents, because it stops the
# loop rather than mislabelling one goal.
#
#   rec unreadable ("")                  -> PROCEED (fail-open, `*)` arm)
#   status completed/blocked/skipped     -> PROCEED (verify wrote SOMETHING)
#   status pending|in-progress ...
#     ... recurring true, claim released -> PROCEED SILENTLY. A recurring goal
#         RETURNS to pending on a successful close BY DESIGN (guard-1483), so
#         pending carries no information here — the old warning was wrong on
#         100% of these closes (g-115-5216: confirmed on 4 agents / 4 goals /
#         3 boxes / both queues; 63 world-queue recurring goals tripped it
#         twice per close). Worse than noise: the remedy it printed
#         (--phase verify) DOUBLE-BUMPS achievedCount, currentStreak and
#         lastAchievedAt on an already-closed recurring goal (the increments
#         in aspirations_write.py's recurring branch are unconditional, no
#         same-window guard). Silence is correctness, not leniency.
#     ... recurring true, claim HELD     -> WARN, proceed. guard-1562: a bare
#         `recurring` carve-out would also silence a recurring goal whose
#         verify genuinely did NOT run — the real defect. claim_held separates
#         them: a successful recurring close pops claimed_by BEFORE touching
#         lastAchievedAt, an abandoned one leaves it live, and both g-115-5001
#         incidents sat pending WITH a live claim. The printed remedy is safe
#         for THIS branch — verify never ran, so re-running it bumps once, not
#         twice. NOT escalated to a refusal: the g-115-5104 evidence behind
#         the refusal is non-recurring, and extending a destructive remedy to
#         a population with no field evidence is what guard-2760 forbids.
#         Residual set newly permitted by the silent branch (recurring +
#         verify failed + never claimed, i.e. a hand-run close) is covered by
#         recurring-starvation-check.sh, which runs every precheck, anchors on
#         the cadence rather than on this close, and is shelve-aware
#         (_is_shelved).
#     ... recurring false                -> REFUSE.
#
# There is no "recurring unknown while status known" branch: the record probe
# prints nothing unless it found a record with a non-empty status, and a found
# record carries a definite recurring boolean. The retired tri-state probe's
# unknown case (g-115-5573) collapses into rec == "".
#
# NOTE these comments live ABOVE the function deliberately:
# test_iteration_close_forward_precondition.py extracts the function BODY and
# asserts it never mentions the fields the carve-out must not key on — prose
# inside the body would trip the source-level pins it enforces.
_assert_verify_landed() {
    local phase="$1" rec rest live recurring claim_held
    rec="$(_probe_goal_record)"
    # Empty rec (unreadable record) leaves all three empty -> `*)` arm ->
    # silent, which is the fail-open direction the recovery block models.
    #
    # Parsed by expansion, NOT `IFS=$'\t' read`: tab is IFS whitespace, so
    # read COLLAPSES a run of tabs and an empty middle field shifts the next
    # field left — a stubbed "pending\t\tfalse" put "false" into `recurring`
    # and an AMBIGUOUS read authorized a refusal (measured at the 2026-08-13
    # reconcile; pinned by test_unknown_recurring_fails_open). Expansion
    # preserves empty fields.
    live="${rec%%$'\t'*}"
    rest="${rec#*$'\t'}"
    recurring="${rest%%$'\t'*}"
    claim_held="${rest#*$'\t'}"
    # A malformed line with no tabs leaves recurring == the whole line, which
    # is neither "true" nor "false" -> the neither-direction arm proceeds.
    case "$live" in
        pending|in-progress) ;;
        *) return 0 ;;
    esac

    if [[ "$recurring" == "true" ]]; then
        [[ "$claim_held" != "true" ]] && return 0
        echo "" >&2
        echo "[iteration-close] ⚠ FORWARD-PRECONDITION WARNING (g-115-5001):" >&2
        echo "  Goal ${GOAL_ID:-?} is status=$live at the ENTRY of $phase with a LIVE CLAIM — verify has NOT closed it." >&2
        echo "  (Recurring: pending alone would be normal after a complete-by cycle, but a successful" >&2
        echo "  recurring close releases the claim first — a held claim means the close never landed.)" >&2
        echo "  This phase will still run and will report success, but the goal record stays open:" >&2
        echo "  it remains a live goal-selector candidate and may be re-executed." >&2
        echo "  Run verify FIRST, then re-run this phase:" >&2
        echo "    bash core/scripts/iteration-close.sh --phase verify --goal ${GOAL_ID:-<id>} --status ${GOAL_STATUS:-completed} --source ${SOURCE:-world} --outcome ${OUTCOME:-<deep|routine>}" >&2
        echo "" >&2
        return 0
    fi

    if [[ "$recurring" != "false" ]]; then
        # Refusal requires the CLEAN "false" — anything else asserts neither
        # direction and proceeds (the fail ladder above). Unreachable via the
        # real probe (a found record carries a definite boolean), but a stub
        # or a future probe may not honor that, and a refusal must never rest
        # on an ambiguous read.
        echo "" >&2
        echo "[iteration-close] ⚠ FORWARD-PRECONDITION WARNING (g-115-5001):" >&2
        echo "  Goal ${GOAL_ID:-?} is status=$live at the ENTRY of $phase — verify has NOT closed it." >&2
        echo "  Could not determine whether this goal is recurring — asserting neither" >&2
        echo "  direction and PROCEEDING (a refusal must never rest on an ambiguous read)." >&2
        echo "" >&2
        return 0
    fi

    echo "" >&2
    echo "[iteration-close] ✖ REFUSED — FORWARD PRECONDITION FAILED (g-115-5573):" >&2
    echo "  Goal ${GOAL_ID:-?} is status=$live at the ENTRY of $phase, and it is NOT recurring." >&2
    echo "  verify did not close this goal, so running $phase would report success over an" >&2
    echo "  open record: the loop would count the goal closed while it stays a live" >&2
    echo "  goal-selector candidate with no outcome_note, and may be re-executed." >&2
    echo "  Run verify FIRST, then re-run this phase:" >&2
    # --outcome is UNCONDITIONAL with an explicit <deep|routine> placeholder, NOT
    # `${OUTCOME:+ ...}` and NOT `${OUTCOME:-deep}`. Ported here from the g-115-4996
    # trap hint (see the `verify)` arm of the _CURRENT_PHASE trap above, which carries
    # the full rationale) when the g-115-5573 refactor relocated this line out of the
    # inline warn block: do_verify REFUSES a call without --outcome, so a hint that
    # omits the flag prints the failing command back at the caller; and for verify
    # --outcome is a BEHAVIOURAL gate (the uncommitted-work auto-override arms only on
    # `deep`), so defaulting a caller who never chose is the silent-wrong class.
    # The two edits collided in git and the port is deliberate — do not "simplify" it
    # back to the conditional form (guard-3035: a refactor invalidates the inherited
    # claim that a fix travelled with the code).
    echo "    bash core/scripts/iteration-close.sh --phase verify --goal ${GOAL_ID:-<id>} --status ${GOAL_STATUS:-completed} --source ${SOURCE:-world} --outcome ${OUTCOME:-<deep|routine>}" >&2
    echo "" >&2
    return 1
}

_print_recovery_instructions() {
    # rc passed as first arg from the trap (captured BEFORE other commands
    # in the trap body run, since trap chains reset $? on each call).
    local rc="${1:-$?}"
    [[ $rc -eq 0 ]] && return 0
    [[ -z "$_CURRENT_PHASE" ]] && return 0  # exited during arg-parse, not in a phase

    echo "" >&2
    echo "[iteration-close] RECOVERY (rc=$rc, phase=$_CURRENT_PHASE, goal=${GOAL_ID:-?}):" >&2
    case "$_CURRENT_PHASE" in
        verify)
            # g-115-7663: PROBE, do not assert. This branch printed ONE
            # unconditional "may be in indeterminate state" plus a "Revert
            # (mark pending)" line on EVERY rc — including rc=2 from the entry
            # check, where nothing ran and the goal was never touched, and
            # including an interruption AFTER the status write landed, where
            # reverting UNDOES a completed goal. The sibling state-update
            # branch below has probed live state since g-115-4096; this one
            # never did. Same helper, same fail-open contract (empty => assert
            # neither direction), and it costs one read on the rc!=0 path only.
            #
            # WHAT CAN STILL REACH HERE, re-derived after this goal made every
            # post-status-write side effect non-fatal (guard-2186 step 5 — an
            # upstream fix can invalidate a message premise while leaving its
            # observation true):
            #   - entry-check refusal (rc=2) or a gate refusal: nothing landed
            #   - the status write itself failing: nothing landed
            #   - INTERRUPTION after the status write (SIGTERM / harness
            #     timeout / exit 143, guard-3511): the write DID land. This is
            #     now the only path that reaches here with a closed record, and
            #     it is the one the old unconditional Revert line corrupted.
            # A failing stamp no longer reaches here at all — it warns and the
            # close continues.
            local _vlive; _vlive="$(_probe_goal_status)"
            if [[ -z "$_vlive" ]]; then
                # The hedge phrase is PINNED by test_iteration_close_recovery_probe.py
                # ::test_verify_case_unchanged_hedge_survives (g-115-4096), which
                # exercises exactly this unreadable branch. g-115-4096 deliberately
                # left the verify case alone because a HEDGE does not assert unread
                # state; g-115-7663 changes only the REMEDIES, so the hedge stays
                # here — the one branch where hedging is the honest answer.
                echo "  Goal ${GOAL_ID:-?} may be in indeterminate state — could not read its live record; asserting neither direction." >&2
                echo "  Probe first: bash core/scripts/aspirations-read.sh --source ${SOURCE:-<world|agent>} --id asp-<NNN>" >&2
            elif [[ "$_vlive" == "$GOAL_STATUS" ]]; then
                echo "  Goal ${GOAL_ID:-?} already reads status=$_vlive — the status this call was writing is ALREADY on the record, so the write landed (here or on a prior run); this rc came after it." >&2
                echo "  Do NOT revert to pending: on a terminal status that re-opens a closed goal." >&2
                echo "  What may be missing is the non-fatal bookkeeping ordered after it (outcome_class," >&2
                echo "  completed_by_role, outcome_note, the diary breadcrumb, the COORDINATION BOARD POST," >&2
                echo "  the team-state in_flight clear). An absent outcome_class is the fingerprint." >&2
                echo "  Re-running verify re-attempts those; the status write is accepted again (no" >&2
                echo "  same-value short-circuit), so the record stays $_vlive — but the board post can" >&2
                echo "  DOUBLE-POST. Check the board before retrying." >&2
            else
                echo "  Goal ${GOAL_ID:-?} still reads status=$_vlive — the status write did NOT land; nothing downstream ran." >&2
                echo "  (No revert needed — the goal is not closed.)" >&2
            fi
            # --outcome is UNCONDITIONAL here, and the empty-case placeholder is
            # explicit rather than the `${OUTCOME:-deep}` default used by the
            # state-update / learning-gate hints below. Two reasons, both specific
            # to verify (g-115-4996):
            #   1. do_verify now REFUSES a call without --outcome, and
            #      _CURRENT_PHASE is set BEFORE that check — so this trap fires on
            #      exactly that refusal, with OUTCOME empty. A hint that omitted
            #      the flag would print the failing command back at the caller.
            #   2. For verify (and ONLY verify) --outcome is a BEHAVIOURAL gate,
            #      not a recorded field: the uncommitted-work auto-override arms
            #      only on `deep`. Defaulting a caller who never chose into the
            #      permissive branch is the silent-wrong class this goal removes,
            #      so make them pick. `<deep|routine>` mirrors the existing
            #      `${SOURCE:-<world|agent>}` placeholder idiom below.
            local cmd="bash core/scripts/iteration-close.sh --phase verify --goal ${GOAL_ID:-<id>} --status ${GOAL_STATUS:-completed} --source ${SOURCE:-world} --outcome ${OUTCOME:-<deep|routine>}"
            [[ -n "$SUMMARY" ]] && cmd+=" --summary \"$SUMMARY\""
            [[ -n "$OVERRIDE_UNCOMMITTED" ]] && cmd+=" --override-uncommitted \"$OVERRIDE_UNCOMMITTED\""
            [[ -n "$OVERRIDE_MISSING_ARTIFACT" ]] && cmd+=" --override-missing-artifact \"$OVERRIDE_MISSING_ARTIFACT\""
            [[ -n "$OVERRIDE_DOMAIN_SUITE" ]] && cmd+=" --override-domain-suite \"$OVERRIDE_DOMAIN_SUITE\""
            echo "  Retry: $cmd" >&2
            # The revert line is now CONDITIONAL. Offering it when the record is
            # already closed is a destructive remedy for a state that does not
            # need one (guard-2760: a destructive remedy needs evidence a
            # reversible one is insufficient).
            if [[ -n "$_vlive" && "$_vlive" != "$GOAL_STATUS" ]]; then
                echo "  Revert (mark pending): bash core/scripts/aspirations-update-goal.sh --source ${SOURCE:-world} ${GOAL_ID:-<id>} status pending" >&2
            fi
            ;;
        state-update)
            # g-115-4096: branch on the READ status — rc!=0 here is commonly the
            # entry validation at do_state_update rejecting the call (missing
            # --goal/--source) before anything ran, in which case verify never
            # fired and the goal may still be pending with a live claim.
            local _live; _live="$(_probe_goal_status)"
            # g-115-3480: carry THIS invocation's quality flags into every retry
            # line below. Empty when none were passed, so the line is unchanged
            # for a caller that genuinely had none.
            local _qf; _qf="$(_quality_flag_suffix)"
            case "$_live" in
                completed)
                    echo "  Goal ${GOAL_ID:-?} has status=completed (verify succeeded) but state-update failed mid-phase." >&2
                    echo "  Retry: bash core/scripts/iteration-close.sh --phase state-update --goal ${GOAL_ID:-<id>} --source ${SOURCE:-world} --outcome ${OUTCOME:-deep}${_qf}" >&2
                    echo "  (No goal-status revert needed — verify already closed the goal record.)" >&2
                    ;;
                "")
                    echo "  Could not read goal ${GOAL_ID:-?}'s live record — verify state UNKNOWN; asserting neither direction." >&2
                    echo "  Probe first: bash core/scripts/aspirations-read.sh --source ${SOURCE:-<world|agent>} --id asp-<NNN>  # check this goal's status" >&2
                    echo "  If status=completed, retry: bash core/scripts/iteration-close.sh --phase state-update --goal ${GOAL_ID:-<id>} --source ${SOURCE:-world} --outcome ${OUTCOME:-deep}${_qf}" >&2
                    echo "  If not, run verify first (--phase verify --status ${GOAL_STATUS:-completed} --outcome ${OUTCOME:-<deep|routine>}), then state-update." >&2
                    ;;
                *)
                    echo "  Goal ${GOAL_ID:-?} is still status=$_live — verify has NOT marked it completed (a claim may still be live on it)." >&2
                    echo "  This rc likely came from entry validation (bad/missing flags): nothing ran, so do NOT trust any prior-phase assumption." >&2
                    echo "  Run verify first: bash core/scripts/iteration-close.sh --phase verify --goal ${GOAL_ID:-<id>} --status ${GOAL_STATUS:-completed} --source ${SOURCE:-world} --outcome ${OUTCOME:-<deep|routine>}" >&2
                    echo "  Then retry:      bash core/scripts/iteration-close.sh --phase state-update --goal ${GOAL_ID:-<id>} --source ${SOURCE:-world} --outcome ${OUTCOME:-deep}${_qf}" >&2
                    ;;
            esac
            ;;
        learning-gate)
            # g-115-4096: same read-before-assert discipline as state-update above.
            local _lg; _lg="$(_probe_goal_status)"
            case "$_lg" in
                completed)
                    echo "  Goal ${GOAL_ID:-?} is closed (verify + state-update done) but learning-gate sub-step failed." >&2
                    echo "  Retry: bash core/scripts/iteration-close.sh --phase learning-gate --goal ${GOAL_ID:-<id>} --source ${SOURCE:-world} --outcome ${OUTCOME:-deep}" >&2
                    ;;
                "")
                    echo "  Could not read goal ${GOAL_ID:-?}'s live record — closure state UNKNOWN; asserting neither direction." >&2
                    echo "  Probe first: bash core/scripts/aspirations-read.sh --source ${SOURCE:-<world|agent>} --id asp-<NNN>  # check this goal's status" >&2
                    echo "  If status=completed, retry: bash core/scripts/iteration-close.sh --phase learning-gate --goal ${GOAL_ID:-<id>} --source ${SOURCE:-world} --outcome ${OUTCOME:-deep}" >&2
                    ;;
                *)
                    echo "  Goal ${GOAL_ID:-?} is still status=$_lg — it is NOT closed (verify/state-update did not complete)." >&2
                    echo "  Run the close sequence from verify: bash core/scripts/iteration-close.sh --phase verify --goal ${GOAL_ID:-<id>} --status ${GOAL_STATUS:-completed} --source ${SOURCE:-world} --outcome ${OUTCOME:-<deep|routine>}, then state-update, then learning-gate." >&2
                    ;;
            esac
            ;;
        productivity-check)
            echo "  productivity-check is observational — failure does NOT affect goal closure." >&2
            echo "  Retry: bash core/scripts/iteration-close.sh --phase productivity-check" >&2
            echo "  Or skip — goal is already closed; productivity gate is advisory." >&2
            ;;
    esac
    return 0  # never modify the script's exit code (trap is informational)
}

# Trap registration moved below dispatch (line ~1100) so phase-end marker
# and recovery instructions fire from the SAME EXIT trap.

while [[ $# -gt 0 ]]; do
    case "$1" in
        --phase)   PHASE="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --goal)    GOAL_ID="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --status)  GOAL_STATUS="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        # WORLD_AGENT_ONLY: cross-agent goals arrive with MIND_AGENT already
        # overridden to the owner (g-115-978 Option 3); enum stays world|agent.
        --source)  SOURCE="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --outcome) OUTCOME="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --summary) SUMMARY="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        # g-115-4208: file-based alternative to --summary. The prose is read
        # VERBATIM from disk, so the shell never sees it and backticks, $(...)
        # and bare $ survive intact. --summary takes an inline double-quoted
        # argument, which means the shell expands those BEFORE this script
        # runs; the write then succeeds at rc=0 with a hole in the prose. That
        # is the dangerous shape — measured four times in one session, once
        # silently deleting the exact command a goal description existed to
        # record. Precedent for the flag: notify-build-payload.py --message-file.
        --summary-file) SUMMARY_FILE="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --override-stale-source) OVERRIDE_STALE_SOURCE="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        # g-242-10: documented opt-out for the Phase 4.26 gate. Forwarded to
        # phase-4-26-gate.sh; logs to world/phase-4-26-overrides.jsonl.
        --no-retrieval-applicable) NO_RETRIEVAL_APPLICABLE="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        # g-115-228: state-update-audit velocity quality inputs. Forwarded only
        # at --phase state-update. Validation (range/type) happens downstream
        # in state-update-audit.py argparse — keep this layer thin.
        --tree-updated)     TREE_UPDATED="true"; shift ;;
        # g-115-464: bypass --tree-updated validation for cases where the
        # caller knows tree was updated but mtime probing won't see it
        # (programmatic update path, mtime-touched-back, etc.).
        --tree-updated-override) TREE_UPDATED_OVERRIDE="true"; shift ;;
        --artifacts-count)  ARTIFACTS_COUNT="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --encoding-score)   ENCODING_SCORE="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --findings-count)   FINDINGS_COUNT="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        # Forward --override-uncommitted to the aspirations-update-goal.sh
        # status=completed call inside do_verify. Without this, audit-only
        # Investigate goals (no source-code changes) and other goals that
        # don't own the dirty files in the working tree cannot reach
        # iteration-close — the gate fires correctly at the update layer
        # but iteration-close was previously stripping the override path.
        # Logged to world/uncommitted-work-overrides.jsonl by the gate.
        --override-uncommitted) OVERRIDE_UNCOMMITTED="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        # Forward --override-missing-artifact to the aspirations-update-goal.sh
        # status=completed call inside do_verify. Mirrors --override-uncommitted
        # forwarding above. Without this, goals whose description path-strings
        # don't exactly match the on-disk placement (e.g., a tree node placed
        # in a structurally-correct alternate location per the goal's "or
        # similar location under X" clause) cannot reach iteration-close —
        # goal-completion-artifact-gate.py fires at the update layer but
        # iteration-close was previously stripping the override path. Logged
        # to world/missing-artifact-overrides.jsonl by the gate.
        --override-missing-artifact) OVERRIDE_MISSING_ARTIFACT="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        # Forward --override-residual to the aspirations-update-goal.sh
        # status=completed call inside do_verify. Third member of the same
        # family as the two above, and it was MISSING until g-335-1216
        # (2026-08-13): gates/residual_work.py names this flag verbatim in its
        # own refusal message ("or pass --override-residual \"<justification>\""),
        # but iteration-close stripped it, so the escape hatch the gate
        # advertises was unreachable from the standard close path — the caller
        # got `unknown arg: --override-residual` (rc=2) and the only way past a
        # false positive was to bypass do_verify entirely, skipping the
        # pending-deploys gate, the ordered-write intent marker, completed_date,
        # outcome_class, the diary breadcrumb, the board post and clear-in-flight.
        # Logged to world/residual-work-overrides.jsonl by the gate.
        --override-residual) OVERRIDE_RESIDUAL="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        # g-353-75: forwarded to domain-suite-gate.py inside do_verify. Turns a
        # domain-suite BLOCK into a logged pass (world/domain-suite-overrides.jsonl).
        --override-domain-suite) OVERRIDE_DOMAIN_SUITE="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        *)
            # (2026-08-29): a bare `unknown arg: --outcome-class` sent a downstream
            # Body (small local model) into a second invented flag (`--executed-by`),
            # then a third, each a full model turn. Name the accepted set -- read
            # from THIS parser's own case labels so it cannot drift -- and the
            # nearest accepted flag when one shares a prefix with the guess.
            # Bounded to this `while ... done` block (guard-2172: accepted flags
            # live in the parsing case block ONLY, never a whole-file flag grep).
            echo "unknown arg: $1" >&2
            _accepted=$(sed -n '/^while \[\[ \$# -gt 0 \]\]; do$/,/^done$/p' "${BASH_SOURCE[0]}" \
                        | grep -oE '^\s+--[a-z-]+\)' | tr -d ' )' | sort -u | tr '\n' ' ')
            echo "  accepted: ${_accepted}" >&2
            _near=$(for _f in $_accepted; do
                        case "$1" in "$_f"*) echo "$_f" ;; esac
                        case "$_f" in "$1"*) echo "$_f" ;; esac
                    done | sort -u | tr '\n' ' ')
            [[ -n "$_near" ]] && echo "  did you mean: ${_near}" >&2
            exit 2 ;;
    esac
done

# g-115-881 fix: normalize --outcome short forms to canonical full forms.
# The Phase header help (line 189 below pre-fix) advertised <d|r>, but every
# downstream consumer expects the full word "deep" or "routine":
#   - line 299: [[ "$OUTCOME" == "deep" ]] (auto-override-uncommitted gate)
#   - line 332: aspirations-update-goal outcome_class "$OUTCOME"  (goal record)
#   - line 584: loop-state-bump-counters.py --outcome (argparse choices=routine,deep)
#   - line 658: state-update-audit.py --outcome-class
#   - lines 677, 776: [[ "$OUTCOME" == "deep" ]] (deep-only paths)
#   - line 762: iteration-checkpoint outcome_class=$OUTCOME
# Echo's g-315-34 close (2026-05-17) passed --outcome d per the documented
# signature and triggered "invalid choice: 'd' (choose from routine, deep)"
# from loop-state-bump-counters.py, leaving goals_completed_this_session
# stale and productivity-gate underestimating. Translate here so all
# downstream callers see the canonical form regardless of caller convention.
if [[ -n "$OUTCOME" ]]; then
    case "$OUTCOME" in
        d) OUTCOME="deep" ;;
        r) OUTCOME="routine" ;;
        deep|routine) ;;  # already canonical
        *) echo "iteration-close: invalid --outcome '$OUTCOME' (must be deep|routine|d|r)" >&2; exit 2 ;;
    esac
fi

# g-115-4208: resolve --summary-file into SUMMARY. Mutually exclusive with
# --summary — passing both is a caller bug with no safe silent resolution
# (picking either one discards prose the caller believed it had supplied), so
# it is refused rather than merged or precedence-ordered.
if [[ -n "$SUMMARY_FILE" ]]; then
    if [[ -n "$SUMMARY" ]]; then
        echo "iteration-close: --summary and --summary-file are mutually exclusive (g-115-4208)" >&2
        echo "  Pass ONE. Use --summary-file for multi-paragraph prose: the shell never" >&2
        echo "  expands it, so backticks, \$(...) and bare \$ survive verbatim." >&2
        exit 2
    fi
    if [[ ! -f "$SUMMARY_FILE" ]]; then
        echo "iteration-close: --summary-file '$SUMMARY_FILE' not found" >&2
        exit 2
    fi
    # Read VERBATIM. $(<file) strips only trailing newlines and performs no
    # word-splitting, globbing or expansion on the content — the whole point.
    SUMMARY="$(<"$SUMMARY_FILE")"
    if [[ -z "$SUMMARY" ]]; then
        echo "iteration-close: --summary-file '$SUMMARY_FILE' is empty" >&2
        exit 2
    fi
    # === stale-narrative-source gate (g-115-7425) ===
    # A file older than this goal's claim cannot describe this unit of work.
    # The two existing narrative gates are both blind to it: the empty-VALUE
    # refusal sees valid prose, and gates.field_shrink sees GROWTH (the measured
    # incident was 36,260 chars landing over 6,580). Placed HERE, immediately
    # after the verbatim read, because this is where the path is still known —
    # every later consumer sees only $SUMMARY. py -3, never bash (guard-156).
    # Exit 3 is the ONLY refusal. Any other non-zero means the gate could not
    # run, and a gate that cannot run must not refuse a write — fail OPEN, but
    # LOUDLY (guard-2298: a malfunction that renders as a clean result is worse
    # than the fault). `|| true` keeps `set -e` out of the decision.
    _sss_rc=0
    py -3 "$SCRIPT_DIR/stale-summary-source-gate.py" \
        --path "$SUMMARY_FILE" --goal "${GOAL_ID:-}" --source "${SOURCE:-}" \
        --caller "iteration-close.sh:summary-file-resolve" \
        ${OVERRIDE_STALE_SOURCE:+--override-stale-source "$OVERRIDE_STALE_SOURCE"} \
        >/dev/null || _sss_rc=$?
    if [[ "$_sss_rc" -eq 3 ]]; then
        echo "  Pass --override-stale-source \"<why this file is genuinely current>\" to proceed." >&2
        exit 2
    elif [[ "$_sss_rc" -ne 0 ]]; then
        echo "iteration-close: WARN stale-summary-source gate did not run (rc=$_sss_rc) — proceeding UNGATED." >&2
    fi
fi

if [[ -z "$PHASE" ]]; then
    echo "usage: iteration-close.sh --phase {verify|state-update|learning-gate|productivity-check|recover} [--goal <id>] [--status <s>] [--source <w|a>] [--outcome <deep|routine|d|r>] [--summary <t> | --summary-file <path>] [--tree-updated] [--tree-updated-override] [--artifacts-count <n>] [--encoding-score <0.0-1.0>] [--findings-count <n>]" >&2
    exit 2
fi

AGENT="${MIND_AGENT:-}"
if [[ -z "$AGENT" ]]; then
    echo "iteration-close: MIND_AGENT unset" >&2
    exit 2
fi
AGENT_DIR="$(agent_dir "$AGENT")"
NOW_ISO="$(date +%Y-%m-%dT%H:%M:%S)"
TODAY="$(date +%Y-%m-%d)"

# --------------------------- checkpoint helper ---------------------------
# Refresh the iteration-checkpoint.json with the phase just completed.
# Routes through the single-writer wrapper (g-248-36) — typed-key validation
# + atomic tempfile+rename are owned by loop-state-save.py; this function
# stays a thin caller. The wrapper's `update` subcommand is a no-op when the
# file is absent, so the prior fail-open semantics are preserved (we still
# work outside the orchestrator without a checkpoint to refresh).
_checkpoint_refresh() {
    local phase_name="$1"
    bash "$CORE_ROOT/scripts/loop-state-save.sh" update \
        --set "phase_completed=$phase_name" \
        --set "last_updated=$NOW_ISO" || true
}

# ─── Shared helper: probe whether GOAL_ID is a recurring goal ───────────────
# Reads the goal record from the source aspirations file; echoes "true"/"false".
# Fail-open: any probe error → "false" (treat as non-recurring; the caller's own
# non-recurring path then surfaces the real error). Two call sites (g-115-2848):
# do_verify's IS_RECURRING/complete-by branch, and do_state_update's Phase-6
# sentinel guard (recurring goals get their sentinel from recurring-close.sh, so
# do_state_update must NOT double-write it).
_probe_is_recurring() {
    local _src_file
    if [[ "$SOURCE" == "world" ]]; then
        _src_file="$WORLD_DIR/aspirations.jsonl"
    else
        _src_file="$AGENT_DIR/aspirations.jsonl"
    fi
    GID="$GOAL_ID" SF="$_src_file" python3 -c '
import json, os, sys
with open(os.environ["SF"], "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        asp = json.loads(line)
        for g in asp.get("goals", []):
            if g.get("id") == os.environ["GID"]:
                print("true" if g.get("recurring") else "false")
                sys.exit(0)
print("false")
' 2>/dev/null || echo "false"
}

# ─── Shared helper: repair a pending utilization obligation (g-115-3123) ────
# Two call sites, one implementation:
#   1. do_state_update, immediately BEFORE phase-4-26-gate.sh — the PRODUCER
#      for the flag that gate consumes.
#   2. do_learning_gate, on the "real manifest for this goal" path — the
#      historical site, kept as a backstop for the case where state-update
#      never ran (operator retry, crash-resume straight into learning-gate).
#
# WHY the state-update call site is load-bearing: the loop runs
# verify -> state-update -> learning-gate. The repair used to live ONLY in
# do_learning_gate, one full phase AFTER its consumer, so a manifest with
# utilization_pending=true always reached phase-4-26-gate.sh un-repaired. The
# gap stayed invisible because the intended backstop (utilization-gate.sh) is
# registered as a PreToolUse[Skill] matcher for a Skill the bash hot path no
# longer invokes (15 fires across 12,325 iterations), and because
# phase-4-26-gate.py's own utilization check is inert. Three mutually-masking
# defects; this fixes the producer half. See g-115-3113 / g-115-3123.
#
# Idempotent: no-ops when the manifest is absent, belongs to another goal, is a
# no-retrieval stub, or already has utilization_pending=false — so calling it at
# both sites in one iteration costs one cheap JSON read on the second call.
# Fail-open: always returns 0. Callers run under `set -e`; a feedback blip must
# never abort the phase (the pending flag itself drives the retry).
_repair_utilization_pending() {
    # Body-aware (g-115-6653): on a worker Body the manifest lives at
    # sessions/<sid>/body-retrieval-session.json, so the hand-composed
    # agent-wide path made this repair a no-op for every worker-executed goal.
    local ret_file; ret_file="$(retrieval_session_path "$AGENT_DIR")"
    [[ -f "$ret_file" ]] || return 0
    local pending
    # Env-var names are deliberately RUP_-prefixed, NOT the GID/RET_FILE pair the
    # no-retrieval stub writer below uses: `test_learning_gate_stub_double_invocation.py`
    # locates that writer by regex-matching its `GID="$GOAL_ID" RET_FILE="$ret_file"
    # python3 -c '` prefix, and this block sits EARLIER in the file — sharing the
    # prefix makes re.search extract THIS block instead (observed, g-115-3123).
    pending="$(RUP_GID="$GOAL_ID" RUP_SESSION="$ret_file" python3 -c '
import json, os
try:
    d = json.loads(open(os.environ["RUP_SESSION"], encoding="utf-8").read())
    # Repair only a REAL manifest belonging to THIS goal. A no-retrieval stub
    # (retrieval_performed is False) or a prior goal file is not ours to touch.
    ok = (d.get("goal_id") == os.environ["RUP_GID"]
          and d.get("retrieval_performed") is not False
          and bool(d.get("utilization_pending")))
    print("true" if ok else "false")
except Exception:
    print("false")
' || { echo "[iteration-close] WARN: retrieval-session.json probe failed — utilization repair skipped for $GOAL_ID" >&2; echo "false"; })"
    # No `2>/dev/null` on the probe (fresh-eyes F-001, g-115-3123): the python block
    # already swallows every parse/IO error into a conservative "false", so the only
    # stderr that can reach here is python3 itself failing to start (shim/PATH). That
    # is exactly the signal worth seeing — suppressing it, as the first draft of this
    # helper did, makes a dead interpreter indistinguishable from "nothing to repair".
    [[ "$pending" == "true" ]] || return 0

    # Two-tier safety-net feedback (g-242-06, fix-a for rb-428/g-242-05 cycle):
    # (1) Try --infer --confidence balanced first. Produces
    #     times_inferred_helpful (half-weight counter) when distinctive tokens
    #     match execution diary / guardrail triggers — unblocks utilization_score
    #     movement without requiring LLM attestation in Phase 4.26.
    #     C.2: balanced (min_distinctive=1) replaces conservative (>=2).
    #     Token-overlap inference is the second-stage classifier — its job is to
    #     find candidates the explicit-attest path missed. The fallback-active
    #     backstop in infer_feedback (utilization-feedback.py) is the third stage
    #     that catches deterministic trigger-fires. Conservative was starving
    #     positive signal: 0 helpful across 320 active guardrails (2026-05-09).
    # (2) On exit 4 (schema_version < 2; documented fallback signal), fall back
    #     to --all-unknown (no-op on counters; just records the method so the
    #     pending flag clears). Replaces the older --all-noise fallback whose
    #     blanket times_noise++ was the root cause of the 0/655 helpful audit
    #     2026-05-07: legitimate retrieval-but-unattested nodes accumulated noise
    #     that fed tree.py distill candidates. See utilization-gate.sh header.
    # (3) On any other non-zero: warn + leave pending=true so a later phase or
    #     the next iteration retries.
    local infer_rc
    if bash "$SCRIPT_DIR/utilization-feedback.sh" --goal "$GOAL_ID" --infer --confidence balanced; then
        echo "[iteration-close] retrieval gate ($_CURRENT_PHASE): inferred utilization feedback for $GOAL_ID (balanced)"
    else
        infer_rc=$?
        if [[ $infer_rc -eq 4 ]]; then
            # Schema too old — documented fallback path
            if bash "$SCRIPT_DIR/utilization-feedback.sh" --goal "$GOAL_ID" --all-unknown; then
                echo "[iteration-close] retrieval gate ($_CURRENT_PHASE): --infer schema<2, fell back to --all-unknown for $GOAL_ID"
            else
                echo "[iteration-close] WARN: utilization-feedback --all-unknown fallback failed for $GOAL_ID (pending=true persists)" >&2
            fi
        else
            echo "[iteration-close] WARN: utilization-feedback --infer failed (rc=$infer_rc) for $GOAL_ID (pending=true persists, will retry)" >&2
        fi
    fi
    return 0
}

# --------------------------- phase: verify ---------------------------
do_verify() {
    _CURRENT_PHASE="verify"
    # CRITICAL — DO NOT add a "default --status from disk" fallback here.
    # The caller MUST declare the target status explicitly. This is the
    # single-source-of-truth contract: caller-declared intent, not script-
    # inferred state. Reading the goal's current status from aspirations.jsonl
    # would silently rubber-stamp whatever a different code path set it to,
    # hiding double-writes and stale-state bugs. recurring-close.sh hardcodes
    # --status completed for the same reason — its semantic invariant is
    # "this cycle of the recurring goal completed". For one-shot goals, the
    # LLM caller decides completed | blocked | skipped per outcome. The error
    # message below tells the caller exactly which flag is missing.
    # --outcome joined the required set in g-115-4996. It was honor-system before
    # (the loop digest called it REQUIRED; this check did not enforce it), and
    # omitting it does far more than drop a field: OUTCOME stays empty, so the
    # auto-override at the uncommitted-work gate below — gated on
    # `GOAL_STATUS == completed && OUTCOME == deep` — never fires, the gate stays
    # ARMED for exactly the deep close it was written to let through, returns 400,
    # and `set -euo pipefail` aborts the REST of the sequence (completed_date,
    # outcome_class, the diary breadcrumb, the board post, clear-in-flight). A
    # missing flag that fails HERE costs one retry; the same flag missing three
    # steps later costs a half-closed goal with no error anywhere.
    if [[ -z "$GOAL_ID" || -z "$GOAL_STATUS" || -z "$SOURCE" || -z "$OUTCOME" ]]; then
        local missing=""
        [[ -z "$GOAL_ID" ]]     && missing+=" --goal"
        [[ -z "$GOAL_STATUS" ]] && missing+=" --status"
        [[ -z "$SOURCE" ]]      && missing+=" --source"
        [[ -z "$OUTCOME" ]]     && missing+=" --outcome"
        echo "verify: missing required flag(s):$missing" >&2
        echo "  usage: iteration-close.sh --phase verify --goal <id> --status <completed|blocked|skipped> --source <world|agent> --outcome <deep|routine> [--summary \"...\"]" >&2
        echo "  hint: pass --status completed for goals already marked complete via aspirations-update-goal.sh" >&2
        exit 2
    fi
    echo "[iteration-close] verify: goal=$GOAL_ID status=$GOAL_STATUS source=$SOURCE"

    # ── Pending-deploys ENFORCE gate (SG-b, g-115-2688-b) ───────────────────
    # Refuse CLEAN-SUCCESS closure while a deploy THIS goal pushed is unverified.
    # For a completed goal, resolve every pending {repo,sha} obligation via
    # deploy-verify.sh (BOUNDED so the loop never blocks on slow CI): cleared on
    # ok/no_ci; HIGH Unblock filed + closure flagged not-clean on CI failure;
    # entry kept for re-probe on unverified. Fail-open (script always exits 0);
    # the has-pending fast-path is a single cheap call when nothing is pending
    # (the common case). stderr (the not-clean signal) stays loop-visible; the
    # summary JSON goes to the iteration-close diagnostic log. Placed BEFORE the
    # state mutation below so a bounded CI-wait that autocompacts leaves the goal
    # not-yet-completed (closure re-runs idempotently next iteration).
    if [[ "$GOAL_STATUS" == "completed" ]]; then
        # NOTE: do NOT forward --source "$SOURCE" here. The gate's --source is
        # used ONLY to file the deploy-failure Unblock, which always lands in
        # the WORLD aspiration asp-115 (infra Unblocks are world-scoped); an
        # agent-sourced closing goal would otherwise make the gate try asp-115
        # in the agent queue, where it does not exist, and the filing would
        # fail-open-silently. The gate defaults --source to world (correct).
        bash "$SCRIPT_DIR/pending-deploys-gate.sh" --agent "$AGENT" --goal "$GOAL_ID" \
            >>"$CORE_ROOT/logs/iteration-close-stderr.log" || true
    fi

    # g-115-6932: WORKER-ONLY ALL-SWEEP. The call above is goal-scoped
    # (`--goal $GOAL_ID` -> `list --goal-id`, EXACT equality) and accumulated
    # entries carry an EMPTY goal_id, so it matches none of them, ever. The
    # un-scoped ALL-sweep that does reach them lives in do_productivity_check,
    # which a worker SKIPS (`worker_execute.py should-run-phase
    # productivity-check` -> skip). Both call sites were therefore unreachable
    # from a worker box, so a worker accumulated deploy obligations forever and
    # the HIGH Unblock this gate exists to file for a genuinely FAILED deploy
    # was never filed. The gate read healthy because nothing measured it.
    #
    # MEASURED on cc-08 2026-08-20, a worker box mid-session: a hand-run sweep
    # found SIX accumulated entries, every one displaying `goal=?` (the empty
    # goal_id), including a deploy registered by this same Body ~40 minutes
    # earlier. Five cleared on that first sweep; one deferred on budget.
    #
    # THE COMPLEMENT, stated rather than left implicit (guard-2783): the REDUCER
    # does NOT need this branch and must not run it — it reaches the identical
    # all-sweep every iteration via do_productivity_check, and a second call
    # here would just re-probe the same entries twice per iteration for nothing.
    # Role-conditional inside this function is the established pattern here (see
    # the --no-supersede branch below and the terminal NEXT line), not a new one.
    #
    # OUTSIDE the completed-gate above ON PURPOSE: obligations accumulate
    # regardless of how THIS goal closed, so a worker whose units close blocked
    # or skipped would otherwise still never sweep. Cadence then matches the
    # reducer's — once per close.
    #
    # Safe to call every close: the gate self-bounds (100s loop budget, defers
    # the remainder to the next close and drops nothing), fast-exits when
    # nothing is pending, and always exits 0. Same fail-open contract as both
    # sibling call sites.
    if [[ "${BODY_ROLE:-}" == "worker" ]]; then
        bash "$SCRIPT_DIR/pending-deploys-gate.sh" --agent "$AGENT" \
            >>"$CORE_ROOT/logs/iteration-close-stderr.log" 2>&1 || true
    fi

    # ── Domain-suite gate (g-353-75) ─────────────────────────────────────────
    # Refuse status=completed while the world's domain test suite
    # ($WORLD_PATH/scripts) is red or uncollectable, IF a domain script changed
    # since this goal's claim. The domain half is invisible to the git-based
    # full-suite-recommender (guard-1947) and run-full-suite-after-deep-code.md
    # only NAMES its command — measured 2026-08-29 on a live deployment, two
    # test modules that could not import shipped through worker closes and every
    # later goal in the lane "verified" against a suite that could not collect.
    # guard-399: the instruction needed a gate. Cheap until it fires (a stat
    # walk; the suite runs only when a domain code file is newer than the
    # claim), FAIL-OPEN on its own errors (decision=error, rc 0), and BEFORE
    # the status write so a refusal leaves the goal open — the EXIT trap's
    # verify branch then prints the retry line, carrying --override-domain-suite.
    # Both roles close through here (worker Phase 4a and the reducer's verify),
    # so this is the one place the domain half is enforced. stdout (the JSON
    # verdict) goes to the diagnostic log; the refusal text is stderr and stays
    # loop-visible — do NOT pipe or silence it (guard-3257).
    # Only the gate's OWN verdict refuses: rc 1 is a block; rc 0 is pass/noop/
    # override/error; anything else (usage error, interpreter missing, the
    # script absent in a harness that copies a subset of core/scripts) is a
    # gate fault and fails OPEN with a warning — never a refusal.
    if [[ "$GOAL_STATUS" == "completed" && -f "$SCRIPT_DIR/domain-suite-gate.py" ]]; then
        _dsg_args=(--goal "$GOAL_ID" --source "$SOURCE")
        [[ -n "$OVERRIDE_DOMAIN_SUITE" ]] && _dsg_args+=(--override "$OVERRIDE_DOMAIN_SUITE")
        _dsg_rc=0
        python3 "$SCRIPT_DIR/domain-suite-gate.py" "${_dsg_args[@]}" \
            >>"$CORE_ROOT/logs/iteration-close-stderr.log" || _dsg_rc=$?
        if [[ $_dsg_rc -eq 1 ]]; then
            echo "[iteration-close] ✖ REFUSED — DOMAIN SUITE RED (g-353-75): goal $GOAL_ID stays open. Fix the world test suite (see above), then re-run this close." >&2
            return 1
        elif [[ $_dsg_rc -ne 0 ]]; then
            echo "[iteration-close] WARN domain-suite-gate rc=$_dsg_rc (gate fault, fail-open) — the domain suite was NOT checked for $GOAL_ID" >&2
        fi
    fi

    # g-284-06 Step 0: Ordered-write intent marker. BEFORE any state mutation
    # of aspirations.jsonl or team-state.yaml, record intent in the
    # iteration-checkpoint.json so a crash mid-verify leaves a recoverable
    # breadcrumb. iteration-close.sh --phase recover detects intent_state=complete
    # + aspirations.status=pending and surfaces the split-brain. Only fires for
    # completed status with an --outcome — for blocked/skipped the protocol's
    # transitional invariant (intent→committed) is irrelevant.
    if [[ "$GOAL_STATUS" == "completed" && -n "$OUTCOME" ]]; then
        bash "$CORE_ROOT/scripts/loop-state-save.sh" update \
            --set "intent_state=complete" \
            --set "intent_outcome=$OUTCOME" || true
    fi

    # CRITICAL — recurring goals MUST go through aspirations-complete-by.sh (cmd_complete_by).
    # status=completed is BLOCKED for recurring goals. The LIVE guard is the DAEMON's, at
    # mind_api/src/endpoints/aspirations_write.py ~L2195 (400 invalid_status_transition) —
    # aspirations-update-goal.sh is daemon-only (no Python CLI fallback), so the mirrored
    # aspirations.py cmd_update_goal guard (~L1701, sys.exit(1)) is NOT the path that fires.
    # This comment previously cited "aspirations.py ~line 669", which is wrong twice over:
    # wrong layer (CLI, not daemon) and wrong line (669 is find_recurring_goals, an unrelated
    # helper). Corrected 2026-08-07 (g-115-5090) after the stale pointer cost a reader a
    # detour — the citation is load-bearing precisely because the next person to doubt the
    # fail-open below will follow it.
    # cmd_complete_by atomically bumps lastAchievedAt + achievedCount + currentStreak/longestStreak
    # while keeping status=pending. Removing this branch resurrects the g-001-01 cargo-cult
    # incident (recurring goal re-selects forever at high score because lastAchievedAt
    # never advances). See plan improve-recurring-goals-kind-yao.md.
    local IS_RECURRING="false"
    if [[ "$GOAL_STATUS" == "completed" ]]; then
        # g-115-2848: shared helper (also used by do_state_update's Phase-6
        # sentinel guard). Fail-open: probe error → "false"; the non-recurring
        # path then hits the recurring guard and surfaces the real error.
        #
        # THAT LAST CLAUSE IS VERIFIED, not merely asserted (g-115-5090, 2026-08-07).
        # It is the whole safety argument for fail-opening, and a mis-probe here is
        # reachable: _probe_is_recurring reads the LOCAL aspirations.jsonl, which on an
        # own-cloud box is a read-through cache (guard-980), and a goal ABSENT from that
        # file returns "false" with no error (L453). Chain, each link measured:
        # the daemon refuses with 400 → aspirations-update-goal.sh exits 1 → do_verify is
        # invoked BARE from the dispatch `case` (not in an if/&&/||), so `set -euo pipefail`
        # at L53 is NOT suppressed and the failure ABORTS → the EXIT trap runs
        # _print_recovery_instructions, which prints the retry command.
        # Consequence for anyone tempted to "harden" this into a fail-CLOSED probe or a
        # store-consulting one: the failure is already LOUD and stops the phase. So a
        # mis-probe cannot silently skip the lastAchievedAt stamp — which means it is NOT
        # an explanation for a recurring goal whose journal and outcome_note landed while
        # lastAchievedAt stayed put. That symptom has some other cause; do not close it here.
        IS_RECURRING="$(_probe_is_recurring)"
    fi

    if [[ "$IS_RECURRING" == "true" ]]; then
        bash "$SCRIPT_DIR/aspirations-complete-by.sh" --source "$SOURCE" "$GOAL_ID"

        # -- g-115-6768: WORKER-ONLY recurring-tally advance -------------------
        # The five counters a recurring close must advance -- substantive_runs,
        # substantive_hits, last_substantive_at, consecutive_routine,
        # consecutive_deep -- are written by recurring-close.sh's post-phase
        # heredoc, STRICTLY AFTER all four iteration-close phases. A worker
        # never runs that script (it would drag in three reducer-only phases),
        # so on the worker path those five freeze while complete_by advances
        # achievedCount / currentStreak / lastAchievedAt normally. Nothing
        # errors; the close looks complete and the tally silently stalls.
        #
        # FAIL-UNSAFE DIRECTION, which is why this is a defect and not an
        # omission: substantive_runs is the DENOMINATOR of the lifetime_hit_rate
        # cargo-cult-detector.py keys on, so a denominator missing worker closes
        # INFLATES the rate -- a low-value recurring goal executed mostly by
        # workers looks MORE productive than it is, the exact inverse of what
        # the detector exists to catch.
        #
        # MEASURED 2026-08-29 (alpha, cc-07): 36 of the 80 recurring goals that
        # track substantive_runs (45%) had their most recent close performed by
        # a worker Body. That is a FLOOR, not a partition -- the role stamp added
        # by g-306-204 identifies worker closes positively and writes NOTHING for
        # a reducer, so absent means reducer-OR-unknown.
        #
        # (The sentence above deliberately does not spell that field name out.
        # Its ordering test locates the stamp with a bare body.find() on the
        # bare identifier, so ANY comment containing it matches first and the
        # assertion compares a comment offset against the status write instead
        # of comparing the real stamp. Measured twice while writing this block:
        # once by the explanatory sentence, then again by a parenthetical that
        # named the test FILE -- whose own name carries the identifier.)
        #
        # THE COMPLEMENT, stated rather than left implicit (guard-2783): the
        # REDUCER must NOT run this branch. It reaches the identical arithmetic
        # through recurring-close.sh's own heredoc, and an unguarded call here
        # would DOUBLE-COUNT every reducer close -- recurring-close.sh invokes
        # this very function as its `verify` phase before writing the tally
        # itself. BODY_ROLE is the correct discriminator because
        # bash-agent-inject.py exports it ONLY on the worker fork path.
        # Role-conditional inside do_verify is the established pattern in this
        # file, not a new one (see the g-115-6932 pending-deploys ALL-sweep
        # above and the --no-supersede branch below).
        #
        # A reducer that closes a recurring goal with `--phase verify` directly
        # is the guard-1591 anti-pattern; it does not fire here and still does
        # not advance the tally. That is unchanged, deliberate, and guard-1591
        # remains the rule that governs it.
        #
        # Arithmetic lives in recurring_tally.py so the two paths cannot
        # silently diverge; that module is READ-ONLY (it resolves the store and
        # emits field/value pairs) and the writes stay here, through the same
        # daemon-routed wrapper every other stamp in do_verify uses. Each write
        # is NON-FATAL (g-115-7663): bare, under `set -euo pipefail`, a lock
        # blip on a bookkeeping stamp would abort the close AFTER the status
        # write already landed.
        # RECURRING_TALLY_OWNER is set by recurring-close.sh around this very
        # call, because it writes the same counters after its four phases.
        # Without that half, a worker Body that closed through recurring-close.sh
        # -- which guard-1591 tells it to do -- would advance every counter TWICE.
        if [[ "${BODY_ROLE:-}" == "worker" && -z "${RECURRING_TALLY_OWNER:-}" ]]; then
            local _tf _tv
            while IFS=$'\t' read -r _tf _tv; do
                [[ -n "$_tf" ]] || continue
                bash "$SCRIPT_DIR/aspirations-update-goal.sh" --source "$SOURCE" "$GOAL_ID" "$_tf" "$_tv" \
                    || echo "[iteration-close] WARN recurring-tally: $_tf stamp failed for $GOAL_ID (non-fatal; close already committed)" >&2
            done < <(GID="$GOAL_ID" SRC_FLAG="$SOURCE" OUTCOME="$OUTCOME" \
                         NOW="$(date +%Y-%m-%dT%H:%M:%S)" \
                         python3 "$SCRIPT_DIR/recurring_tally.py" || true)
        fi
    else
        # g-280-09 auto-override: for deep-outcome iterations, iteration-commit.sh
        # fires unconditionally in do_state_update (this same invocation, next
        # phase). In-flight framework code is therefore already scheduled to be
        # committed — the verify-time gate's "dirty=block" check is redundant
        # for this iteration's own edits. Auto-set a uniform reason so the audit
        # ledger captures the auto-pass path distinctly from caller-provided
        # justifications. Caller-provided OVERRIDE_UNCOMMITTED still takes
        # precedence (non-empty check below). Routine outcomes skip this branch
        # because iteration-commit.sh no-ops on routine — the gate retains
        # protective value for routine-with-dirty-code (orphan-code signal).
        if [[ "$GOAL_STATUS" == "completed" && "$OUTCOME" == "deep" && -z "$OVERRIDE_UNCOMMITTED" ]]; then
            if [[ "${BODY_ROLE:-}" == "worker" ]]; then
                # A WORKER Body never reaches do_state_update, so the reason
                # above would be FALSE in its audit ledger row. Its commits
                # travel by worker-loop Phase 3.8 (iteration-push
                # --push-worker-ref, ordered before this close) and the
                # daemon's uncommitted-work gate is body-role aware for the
                # delivery half (g-115-5335); the ledger row must say which
                # path was actually taken (2026-08-16, g-115-6337 review).
                OVERRIDE_UNCOMMITTED="auto: worker Body — HEAD pushed to refs/workers/<agent>/<sid> in worker-loop Phase 3.8 (no state-update phase runs for a worker)"
            else
                OVERRIDE_UNCOMMITTED="auto: iteration-commit.sh scheduled in state-update (g-280-03 wiring)"
            fi
        fi
        # Forward --override-uncommitted only when the caller passed it (or
        # the auto-override above set it) AND status is completed (the only
        # case where the uncommitted-work gate fires). Empty OVERRIDE_UNCOMMITTED
        # means "no override" — we don't pass the flag at all so the gate
        # enforces normally for goals that DO own the dirty files.
        # Build the status-update command with whichever override flags the
        # caller passed (or auto-set above). Both flags are independent —
        # callers may pass either, both, or neither.
        update_cmd=("bash" "$SCRIPT_DIR/aspirations-update-goal.sh" --source "$SOURCE" "$GOAL_ID" status "$GOAL_STATUS")
        if [[ "$GOAL_STATUS" == "completed" && -n "$OVERRIDE_UNCOMMITTED" ]]; then
            update_cmd+=(--override-uncommitted "$OVERRIDE_UNCOMMITTED")
        fi
        if [[ "$GOAL_STATUS" == "completed" && -n "$OVERRIDE_MISSING_ARTIFACT" ]]; then
            update_cmd+=(--override-missing-artifact "$OVERRIDE_MISSING_ARTIFACT")
        fi
        # No auto-override counterpart here, deliberately. The two flags above
        # each have an auto-set path for a case the gate cannot see; residual
        # work has none — an unfiled residual is exactly what the gate exists to
        # catch, so it must stay caller-asserted and audited, never inferred.
        if [[ "$GOAL_STATUS" == "completed" && -n "$OVERRIDE_RESIDUAL" ]]; then
            update_cmd+=(--override-residual "$OVERRIDE_RESIDUAL")
        fi
        "${update_cmd[@]}"
        if [[ "$GOAL_STATUS" == "completed" ]]; then
            # NON-FATAL (g-115-7663). Bare, this aborts the whole close under
            # `set -euo pipefail` AFTER the status write has already landed —
            # see the block above outcome_class for the forensics.
            bash "$SCRIPT_DIR/aspirations-update-goal.sh" --source "$SOURCE" "$GOAL_ID" completed_date "$TODAY" \
                || echo "[iteration-close] ⚠ completed_date stamp failed for $GOAL_ID (non-fatal; status is already committed)" >&2
        fi
    fi

    # g-248-72: persist outcome_class to the goal record at completion time.
    # Sits OUTSIDE the recurring/non-recurring branch so both paths benefit:
    # recurring-close.sh already routes through do_verify, so each cycle's
    # latest outcome_class is captured. cmd_update_goal in aspirations.py has no
    # known-fields allowlist (sets goal[field]=value directly at line 1273), so
    # the new field is recorded without a schema migration.
    #
    # The `-n "$OUTCOME"` guard is now UNREACHABLE-BY-FLAG and kept only as a
    # cheap invariant. It was written as a backward-compatibility no-op for
    # "legacy verify callers" that omitted --outcome; g-115-4996 made --outcome
    # REQUIRED at do_verify's entry check, so such a caller is refused at the
    # top and never reaches here. Left in place because the cost is one test and
    # removing it would make this line depend on an entry check ~150 lines away.
    # NON-FATAL (g-115-7663), and this is the line that MEASURABLY took a close
    # down. Every write from here to the end of do_verify is BOOKKEEPING or
    # NOTIFICATION: the status write above is the only one whose failure means
    # "the close did not happen". Bare, this call runs under `set -euo pipefail`
    # (L65), so a lock blip on a stamp aborts do_verify and the EXIT trap (L3938)
    # prints _print_recovery_instructions — offering a retry that would
    # double-apply the status write and a revert-to-pending that would UNDO a
    # completed goal. Neither remedy matches the actual state.
    #
    # WHAT IT KILLS, in order, all of them already fail-open and none reached:
    # completed_by_role, outcome_note (closure-evidence-write), the execution-diary
    # breadcrumb, the "Completed:" COORDINATION BOARD POST, the team-state
    # in_flight clear, intent_state=committed, and close-defer-invalidation. The
    # board post is the one that matters off-box: it is how the reducer and
    # partner agents learn a goal closed, so its loss is invisible from HERE and
    # surfaces as duplicate work or a stalled handoff on ANOTHER machine.
    #
    # MEASURED on g-326-627 (cc-08, 2026-08-24T14:43:41): status=completed and
    # completed_date=2026-08-24 both landed, outcome_class and completed_by_role
    # are ABSENT, and the board carried no post — i.e. do_verify died exactly
    # here, one write past completed_date. An absent outcome_class on an
    # otherwise-complete close is the durable fingerprint of this failure.
    #
    # Do NOT "fix" this by guarding the status write too: a failed status write
    # means the close genuinely did not happen, and rc=1 is the correct report
    # (that is guard-3256 sequence B, and it must stay loud).
    if [[ -n "$OUTCOME" && "$GOAL_STATUS" == "completed" ]]; then
        bash "$SCRIPT_DIR/aspirations-update-goal.sh" --source "$SOURCE" "$GOAL_ID" outcome_class "$OUTCOME" \
            || echo "[iteration-close] ⚠ outcome_class stamp failed for $GOAL_ID (non-fatal; status is already committed)" >&2
    fi

    # g-306-204: stamp WHICH ROLE closed the goal. Sits beside outcome_class
    # because it shares that stamp's shape exactly (post-status, completed-only,
    # non-fatal, both recurring and non-recurring paths).
    #
    # WHY IT EXISTS: nothing durable and fleet-visible recorded whether a goal
    # was closed by a worker Body or by the reducer, so no audit could compute a
    # worker-vs-reducer rate of anything. The one marker the design named --
    # worker-loop Phase 3.9's `--prefix "[worker-loop] close:"` -- is only ever
    # used in closure-evidence-write.sh's own echo lines and is NEVER written
    # into the record; measured 2026-08-22 across the whole world store (18.4 MB,
    # 2,860 goals, 655 completed, 534 carrying an outcome_note): the marker
    # appears on ZERO goals, and could not have appeared on any. Body manifests
    # are box-local (1 visible here) and worker refs are consumed on merge, so
    # neither is a durable fleet-visible discriminator either.
    #
    # ABSENT IS NOT "reducer" -- bash-agent-inject.py exports BODY_ROLE ONLY on
    # the worker fork path, so the reducer never sets it and an unset value means
    # reducer-OR-unknown. We therefore write NOTHING rather than inventing
    # "reducer", mirroring the completed_by_sid stamp's own rule that an absent
    # value beats a wrong one. Any consumer must treat this as a POSITIVE
    # identification of worker closes and never as a partition of all closes.
    #
    # Non-fatal like the stamps above: status is already committed, so failing
    # here would strand the close in a state the caller cannot read from the rc.
    if [[ -n "${BODY_ROLE:-}" && "$GOAL_STATUS" == "completed" ]]; then
        bash "$SCRIPT_DIR/aspirations-update-goal.sh" --source "$SOURCE" "$GOAL_ID" completed_by_role "$BODY_ROLE" \
            || echo "[iteration-close] ⚠ completed_by_role stamp failed for $GOAL_ID (non-fatal; field stays absent = unknown)" >&2
    fi

    # g-115-5157: land the verify narrative on the GOAL RECORD. Until now
    # --summary reached the execution diary (per-agent, per-box) and a board
    # post (chronological, ages out of every --since window) but never the
    # durable, shared, queryable artifact. Writing an outcome_note was an act of
    # REMEMBERING, separate from the closure path — measured 185 of 644 goals
    # (29%) completed 08-01..08-06 carried one. Routing it here makes evidence a
    # BYPRODUCT of the path the loop already takes.
    #
    # WRITE-IF-ABSENT, NEVER CLOBBER. aspirations-update-goal.sh has no append
    # mode; the write is an overwrite. The 29% who author a note by hand write
    # one BEFORE calling verify, and theirs is the richer artifact — overwriting
    # it with a shorter verify summary would be a worse defect than the one
    # being fixed. So an existing note wins and the skip is announced rather
    # than silent. Idempotent by construction: a re-run of verify (the printed
    # recovery path re-invokes it) finds the note it wrote and declines.
    #
    # NOT status-scoped: a blocked or skipped close's narrative records WHY,
    # which is at least as valuable as a completion's.
    #
    # Guarded on non-empty SUMMARY, so a caller passing no --summary reaches
    # none of this and closes exactly as before (guard-1423, outcome 2).
    #
    # Non-fatal: the goal's status is already committed above, so aborting here
    # would strand the close in a state the caller cannot read from the rc. The
    # failure is announced instead of swallowed.
    # DELEGATED to closure-evidence-write.sh (g-115-5158) so a WORKER Body can
    # produce the same evidence by CALLING the same component. Before this, a
    # worker skipped verify (worker-loop Phase 4 named do_verify Step 3
    # explicitly; since 2026-08-16 Phase 4a CALLS do_verify for the status write,
    # after that producer) and therefore had NO producer at all — its notes were written
    # by hand, so the rate was dispositional rather than mechanical. Arming any
    # enforcement gate on outcome_note would then have refused 100% of worker
    # closures while passing reducer ones. Semantics are unchanged here:
    # write-if-absent, never clobber, guarded on non-empty SUMMARY, non-fatal.
    # g-115-6633: on the WORKER path this call is a BACKFILL, not a first write.
    # worker-loop Phase 3.9 already wrote the rich narrative for THIS occurrence
    # seconds ago, so an existing note is not prior-occurrence residue and the
    # recurring-supersede branch must not replace it with our one-line summary.
    # On the REDUCER path this call IS the first write of the occurrence, so
    # superseding a genuine prior-occurrence note stays correct and the flag is
    # NOT passed. Role-conditional here is consistent with this function, which
    # already branches on BODY_ROLE for its terminal NEXT line (guard-2783: the
    # complement is stated rather than left implicit).
    _ce_flags=()
    if [[ "${BODY_ROLE:-}" == "worker" ]]; then
        _ce_flags+=(--no-supersede)
    fi
    if [[ -n "$SUMMARY" ]]; then
        bash "$SCRIPT_DIR/closure-evidence-write.sh" --goal "$GOAL_ID" \
            --source "$SOURCE" --summary "$SUMMARY" \
            --prefix "[iteration-close] verify:" "${_ce_flags[@]+"${_ce_flags[@]}"}" || true
    fi

    # Execution diary breadcrumb for postcompact recovery.
    # diary append reads ONE JSON object from stdin — no positional args.
    if [[ -n "$SUMMARY" ]]; then
        GID="$GOAL_ID" SUM="$SUMMARY" python3 -c '
import json, os
content = os.environ.get("SUM", "").encode("utf-8", errors="replace").decode("utf-8")
print(json.dumps({
    "entry_type": "finding",
    "goal_id":    os.environ["GID"],
    "content":    "verified: " + content,
}))
' | bash "$SCRIPT_DIR/execution-diary.sh" append || true
    fi

    # Post board claim-release / completion message (world goals only)
    if [[ "$SOURCE" == "world" && "$GOAL_STATUS" == "completed" ]]; then
        local msg="Completed: ${SUMMARY:-$GOAL_ID} [$GOAL_ID]"
        local post_tags="$GOAL_ID"
        # ── Cross-world completion ECHO (g-361-03, goal-completion audit) ──
        # A goal a PEER deployment injected (asp-xw-* sandbox) or filed
        # (--target-aspiration) had no return path: the peer only learned of
        # our completion by reading this board unprompted. The peer DOES read
        # this board and cites our ids back (cross-deployment-channel.md, two
        # measured round-trips), so an explicitly ADDRESSED post IS the
        # delivery mechanism the convention prescribes when peer-board-post is
        # unreachable: BOTH the bare `<agent>` tag (installed base) and
        # `requires_action_by:<agent>@<env-id>` (exact form), plus a request
        # for an acknowledging --reply-to. xw_origin.py folds the four
        # provenance shapes live records carry into one address and prints
        # nothing for a goal with no peer origin — the common case pays one
        # local file scan and posts exactly what it posted before. Fail-open:
        # a provenance lookup must never fail a close.
        local xw_json="" xw_addr="" xw_agent="" xw_env=""
        xw_json="$(python3 "$SCRIPT_DIR/xw_origin.py" --goal "$GOAL_ID" --source "$SOURCE" 2>/dev/null || true)"
        if [[ -n "$xw_json" ]]; then
            read -r xw_addr xw_agent xw_env < <(XWJ="$xw_json" python3 -c '
import json, os
d = json.loads(os.environ["XWJ"])
print(d.get("address") or "-", d.get("agent") or "-", d.get("env") or "-")
' 2>/dev/null || echo "- - -")
            if [[ "$xw_env" != "-" ]]; then
                post_tags="$GOAL_ID,cross-deployment,xw-completion,${xw_env}"
                if [[ "$xw_addr" != "-" ]]; then
                    post_tags="${post_tags},${xw_agent},requires_action_by:${xw_addr}"
                    msg="Completed (cross-world echo to ${xw_addr} — this goal came from your deployment; please --reply-to this post to acknowledge, or re-file if the outcome does not close your side): ${SUMMARY:-$GOAL_ID} [$GOAL_ID]"
                else
                    msg="Completed (cross-world echo to ${xw_env} — origin recorded env-only, no agent to address; a peer reading this board please acknowledge with --reply-to): ${SUMMARY:-$GOAL_ID} [$GOAL_ID]"
                fi
                echo "[iteration-close] cross-world completion echo: $GOAL_ID originated from ${xw_addr:-$xw_env} — Completed: post addressed to the peer (tags: $post_tags)"
            fi
        fi
        # Informational: board-post failure is non-fatal, but surface stderr so
        # network/write issues don't silently disappear.
        echo "$msg" | bash "$SCRIPT_DIR/board-post.sh" --channel coordination --type complete --tags "$post_tags" || true
    fi

    # g-284-06 Step 3: Clear team-state.in_flight HERE (moved from do_state_update).
    # The previous location ran AFTER state-update's other writes, leaving a
    # window where aspirations.status=completed but team-state.in_flight still
    # showed phase=4 — the 3-store split-brain that g-284-02 identified
    # (msg-20260510-183255-zeta-943). Firing in_flight clear immediately after
    # the aspirations.jsonl writes (status, outcome_class) and the
    # board completion post closes that window. Idempotent: clear succeeds
    # whether or not in_flight is currently set, so blocked/skipped paths
    # where the caller already released the claim (Phase 4.0 / CREATE_BLOCKER)
    # are no-ops here.
    #
    # --if-goal SCOPES the clear to the goal this close is for (g-306-161,
    # guard-2474 clause 1). in_flight is keyed by AGENT NAME with no sid, so a
    # reducer and any worker Body of the same agent share ONE row; an
    # unconditional clear here blanks whatever row is present at call time, and
    # a sibling that claimed a DIFFERENT goal in the meantime reads as idle to
    # every partner's selector (guard-2305). The comparison is re-checked inside
    # the row lock, so this is a compare-and-swap, not a snapshot check.
    #
    # The idempotence the paragraph above relies on is PRESERVED, not traded
    # away: an already-released claim leaves no in_flight key at all, and the
    # modifier returns early on key-absence BEFORE the if_goal comparison — so
    # blocked/skipped closes stay exactly the no-ops they were. Scoped and
    # unscoped differ ONLY when the row holds a DIFFERENT goal, which is the
    # case being fixed. $GOAL_ID is guaranteed non-empty here, by TWO
    # independent barriers — measured by execution in
    # core/scripts/tests/test_verify_phase_empty_goal_id.py, not inferred:
    #   1. do_verify's required-arg check (~L487) exits 2 when --goal is absent.
    #   2. even with (1) disarmed, the UNGUARDED "${update_cmd[@]}" above is
    #      reached first; aspirations-update-goal.sh refuses an empty goal id
    #      (rc=1) and `set -euo pipefail` aborts one write short of here.
    # So (1) is what makes the refusal LEGIBLE (exit 2 + a usage line naming
    # the flag) rather than what makes this clear safe. The thing that would
    # actually endanger it is a change to aspirations-update-goal.sh's
    # empty-id handling — barrier 2 is a collaborator's contract, and no
    # input to THIS script can hold it open.
    #
    # What IS given up: the incidental cleanup of a stale row left by a previous
    # iteration whose clear failed. That is deliberate and it is the fail-safe
    # direction (rb-6498) — a wrong clear makes a live agent look idle
    # fleet-wide, while a missed clear self-heals on the NEXT CLAIM: the
    # in-flight setter (team_state_write.in_flight / team-state.py cmd_in_flight)
    # assigns row["in_flight"] outright with no conditional, so the next claim
    # overwrites any stale row. MEASURED 2026-08-03 — an earlier revision of this
    # comment also named "the next stranded-claim sweep" as a second self-heal,
    # which is FALSE: that sweep triggers on a stranded CLAIM, and the residue
    # case here is a normally-RELEASED claim whose row clear was declined, so the
    # sweep never scans it. The claim path alone carries the guarantee.
    #
    # THAT GUARANTEE IS WEAKER THAN "ONE ITERATION", AND THIS COMMENT SAID
    # OTHERWISE UNTIL 2026-08-06 (g-306-219, zeta, cc-02, uname -r
    # 6.8.0-136-generic). It read "a claim happens every iteration. Staleness is
    # therefore bounded by one iteration." Both sentences are false: the in_flight
    # stamp lives inside aspirations-claim.sh (~L239), and that script is invoked
    # ONLY for world-source goals — aspirations-loop-digest.md:255 states it
    # outright, "LOAD-BEARING: agent-queue goals never invoke aspirations-claim.sh".
    # So an agent working a run of AGENT-source goals completes iteration after
    # iteration with no claim, and nothing overwrites the row. The real bound is
    # "until the next WORLD-source claim", which is unbounded in wall-clock and
    # in iteration count.
    #
    # Measured instance: alpha carried in_flight=g-335-855 for 58.6min after that
    # goal completed 03:12:17, while alpha stayed active. THREE closes landed with
    # in_flight.claimed_at frozen at 02:53:37 (a claim rewrites the whole row, so
    # an unchanged claimed_at proves no claim landed): g-335-760 02:55:34
    # agent-self, g-335-855 03:12:17, g-306-120 03:14:50 agent-self. The two
    # agent-self closes were declined by the CAS above — correctly, they name a
    # different goal — and neither could self-heal, because neither claims.
    # current_focus is stamped by the same claim and has no clear at all, so it
    # goes stale with in_flight and stays stale longer.
    #
    # DO NOT "fix" this by adding a stale-row sweep here. Why the 03:12:17 clear
    # (whose CAS would have MATCHED) did not land is still unknown — killed
    # mid-close, or ran and failed into the WARN below, which is swallowed on the
    # closing agent's own box and unreadable from any other. A sweep would clear
    # that evidence every cycle and make the underlying miss permanently
    # undiagnosable (guard-2260). Fix the bound claim first; keep the residue.
    #
    # g-306-233: persist the failure so it is diagnosable from another box.
    #
    # The rc is the ONLY reliable failure signal here — measured, not inferred:
    # team-state-clear-in-flight.sh prints NOTHING on its failure paths (rc=2 ->
    # bare `exit 1`; any other rc -> bare `exit $rc`; only the rc=3 no-daemon path
    # says anything). So an EMPTY capture is the normal failure shape and must
    # never be read as "nothing went wrong".
    #
    # WHY a durable record and not stderr-only — guard-772, whose trigger_condition
    # names this exact shape ("|| echo WARN >&2 around a state-mutating command in
    # a script that can run in a backgrounded iteration-close context"). iteration-
    # close runs backgrounded whenever it exceeds the 2-minute Bash timeout, and the
    # harness bg task file does not capture a nested process's stderr — so the WARN
    # below is invisible even on the CLOSING agent's own box, which is stronger than
    # g-306-219 concluded (it read the gap as cross-box only). The execution diary
    # is per-agent and sync_tier continuity, so a partner can read it: the exact
    # property whose absence made every past instance undiagnosable.
    #
    # Fail-open is DELIBERATELY preserved. guard-139 argues against `|| echo`
    # fallbacks, and is correctly overridden here: this call drives no decision, a
    # team-state blip must not abort a close, and the rc is no longer discarded —
    # it is recorded. Nothing about the fail-safe direction at L656-698 is reversed.
    # KEEP THE INVOCATION ON ITS OWN LINE. test_clear_in_flight_call_site_scoping
    # finds both clear call sites with `ln.strip().startswith("bash ")`, so an
    # inlined `clear_out="$(bash ...)"` drops this site out of that scan — the
    # verify site goes uncounted and its --if-goal scoping stops being checked.
    # Measured, not predicted: the first version of this change was inlined and
    # took both of that file's call-site tests red (g-306-233).
    local clear_rc=0 clear_out=""
    clear_out="$(
        bash "$SCRIPT_DIR/team-state-clear-in-flight.sh" --agent "$AGENT" --if-goal "$GOAL_ID" 2>&1
    )" || clear_rc=$?
    if [[ "$clear_rc" -eq 0 ]]; then
        # Re-emit so instrumenting this call does not silence the success-path
        # line ("in_flight cleared for X" / "left alone ...") that previously
        # reached the terminal directly.
        if [[ -n "$clear_out" ]]; then printf '%s\n' "$clear_out"; fi
    else
        if [[ -n "$clear_out" ]]; then printf '%s\n' "$clear_out" >&2; fi
        echo "[iteration-close] WARN: team-state-clear-in-flight failed for $AGENT (last_active + in_flight clear both lost)" >&2
        AG="$AGENT" GID="$GOAL_ID" RC="$clear_rc" ERR="$clear_out" python3 -c '
import json, os
err = os.environ.get("ERR", "").encode("utf-8", errors="replace").decode("utf-8")
print(json.dumps({
    "entry_type": "failure",
    "goal_id":    os.environ["GID"],
    "content":    ("team-state-clear-in-flight FAILED agent=" + os.environ["AG"]
                   + " goal_id=" + os.environ["GID"]
                   + " rc=" + os.environ["RC"]
                   + " output=" + (err if err else "(none — the clear failure paths print nothing)")
                   + " | in_flight row may now be STALE and last_active was not"
                   + " advanced; readable cross-box via the diary (g-306-233)"),
}))
' | bash "$SCRIPT_DIR/execution-diary.sh" append || true
        # Post-step verification (guard-772). A durable record that silently did
        # not land reproduces the very invisibility this change removes, so the
        # trace is read BACK rather than assumed. A failed read and a missing
        # trace deliberately produce the SAME warning — both mean "this failure
        # is unrecorded", which is the fail-safe direction (guard-1941: the
        # suppressed stderr here never yields a silent absence conclusion).
        if ! bash "$SCRIPT_DIR/execution-diary.sh" read --limit 5 --json 2>/dev/null \
             | grep -q "team-state-clear-in-flight FAILED"; then
            echo "[iteration-close] WARN: clear-failure trace did NOT land in the execution diary for $AGENT/$GOAL_ID — this failure is now UNRECORDED (g-306-233)" >&2
        fi
    fi

    # g-284-06 Step 4: Transition intent_state from complete to committed.
    # All three state stores (iteration-checkpoint phase_completed, aspirations
    # status/outcome_class, team-state in_flight clear) have now landed. The
    # committed marker tells the recovery hook that this iteration's verify
    # finished cleanly — no retry needed.
    if [[ "$GOAL_STATUS" == "completed" && -n "$OUTCOME" ]]; then
        bash "$CORE_ROOT/scripts/loop-state-save.sh" update \
            --set "intent_state=committed" || true
    fi

    # ── Close-time dependent-defer recheck (g-115-2572, rb-3946 — ADVISORY) ──
    # Scans open goals' defer_reason texts for references to the just-closed
    # goal (exact id OR >=2 significant title-token overlaps) and prints one
    # advisory line per hit so the LLM re-probes that defer premise-by-premise
    # instead of waiting out the TTL (observed ~11h frozen on g-336-03/06/07).
    # Surface-only: never mutates, never files; fail-open (`|| true` + the
    # script's own exit-0-on-error contract). completed-status closes only —
    # a blocked/skipped close cannot have satisfied anyone's defer premise.
    if [[ "$GOAL_STATUS" == "completed" ]]; then
        _cdi_title="$(CDI_SRC="$SOURCE" CDI_GOAL="$GOAL_ID" python3 -c "
import json, os, sys
sys.path.insert(0, os.path.join(r'$PROJECT_ROOT', 'core', 'scripts'))
import _rt
try:
    data = _rt.tolerant_decode_aggregate('cdi-title', _rt.aspirations_read(source=os.environ['CDI_SRC'], active=False))
    for asp in (data.get('aspirations') if isinstance(data, dict) else data) or []:
        for g in asp.get('goals', []) or []:
            if g.get('id') == os.environ['CDI_GOAL']:
                print(g.get('title') or '')
                raise SystemExit(0)
except Exception:
    pass
" 2>/dev/null || true)"
        python3 "$CORE_ROOT/scripts/close-defer-invalidation.py" \
            --goal "$GOAL_ID" --title "$_cdi_title" || true
    fi

    # ── Gate D OUTCOME telemetry (DORMANT — gated by GATE_D_ENABLED) ──────────
    # Gate D experiment seam (methodology §4.6 / R7, RATIFIED 2026-06-10). Writes
    # the per-goal OUTCOME record that R5 joins to the Step 5e ASSIGNMENT record
    # on (agent, goal_id). DEFAULT OFF — fires ONLY when GATE_D_ENABLED == "true"
    # (omni-only flag-flip). Append-only, per-agent, single line, to the SAME
    # gate-d-telemetry.jsonl Step 5e writes. Bash-authoritative fields (goal_id,
    # agent, world, outcome_class, blocker_created, timestamp) are always correct;
    # LLM-execution-trace fields (verify_first_pass, verify_escalation_depth,
    # retry_count, wall_clock_seconds) come from optional GATE_D_* env the verify
    # caller MAY set, else null. GATE-INTEGRITY 9.5: agents MUST NOT set
    # GATE_D_ENABLED, and MUST NOT alter this record's shape after omni blesses.
    # Amendment 4 (omni 2026-06-11): gate on gate-d-check.sh, not raw env — the
    # agent allowlist (core/config/gate-d-agents) must gate OUTCOME writes the
    # same way it gates Step 5e ASSIGNMENT writes, else non-pilot agents append
    # orphan OUTCOME records.
    if [[ "$(bash "$PROJECT_ROOT/core/scripts/gate-d-check.sh" 2>/dev/null)" == "on" ]]; then
        local _gd_blocker="false"
        [[ "$GOAL_STATUS" == "blocked" ]] && _gd_blocker="true"
        # python3 (not py -3) is correct inside this .sh — it sources _paths.sh.
        # Values pass via env (guard-165), never interpolated into the source.
        GD_GOAL="$GOAL_ID" GD_AGENT="$AGENT" GD_WORLD="${GATE_D_WORLD:-}" \
        GD_OUTCOME="$OUTCOME" GD_BLOCKER="$_gd_blocker" \
        GD_FIRSTPASS="${GATE_D_VERIFY_FIRST_PASS:-}" \
        GD_ESCDEPTH="${GATE_D_VERIFY_ESCALATION_DEPTH:-}" \
        GD_RETRY="${GATE_D_RETRY_COUNT:-}" GD_WALL="${GATE_D_WALL_CLOCK_SECONDS:-}" \
        GD_TS="$(date +%Y-%m-%dT%H:%M:%S)" GD_FILE="$AGENT_DIR/session/gate-d-telemetry.jsonl" \
        python3 -c '
import json, os
def _opt(name):
    v = os.environ.get(name, "")
    if v == "":
        return None
    try:
        return int(v)
    except ValueError:
        return True if v == "true" else (False if v == "false" else v)
rec = {
    "record_type": "outcome",
    "goal_id": os.environ["GD_GOAL"],
    "agent": os.environ["GD_AGENT"],
    "world": os.environ.get("GD_WORLD") or None,
    "verify_first_pass": _opt("GD_FIRSTPASS"),
    "verify_escalation_depth": _opt("GD_ESCDEPTH"),
    "outcome_class": os.environ.get("GD_OUTCOME") or None,
    "retry_count": _opt("GD_RETRY"),
    "blocker_created": os.environ["GD_BLOCKER"] == "true",
    "wall_clock_seconds": _opt("GD_WALL"),
    "timestamp": os.environ["GD_TS"],
}
with open(os.environ["GD_FILE"], "a", encoding="utf-8") as f:
    f.write(json.dumps(rec) + "\n")
' || echo "[iteration-close] WARN: gate-d outcome telemetry write failed for $GOAL_ID" >&2
    fi
    # ── End Gate D OUTCOME telemetry ──────────────────────────────────────────

    # ── Phase-6 spark imperative for NON-recurring deep closes (g-115-2416) ──
    # recurring-close.sh emits an outcome-aware imperative + the
    # pending_phase_6_spark WM sentinel for recurring deep closes (g-115-977 /
    # g-115-1174). Non-recurring deep closes had NEITHER: Phase 6 sits between
    # verify and state-update purely on LLM memory of digest ordering, and it
    # drifted (observed miss: g-115-2404, journal OBLIGATION LATE 2026-07-16).
    # This phase emits the stdout imperative; the sentinel WRITE lives in
    # do_state_update (g-115-2848 — see below).
    #   - stdout imperative (visible in this turn's tool output) — HERE
    #   - pending_phase_6_spark sentinel — MOVED to do_state_update, which
    #     REQUIRES --outcome. Verify USED to make --outcome optional, so a call
    #     that omitted it left OUTCOME empty, this gate false, and the
    #     g-115-2416 backstop silently no-op'd (observed g-115-2839: a deep
    #     non-recurring verify without --outcome wrote no sentinel; Phase 6 spark
    #     skipped). That optionality is GONE as of g-115-4996 — do_verify now
    #     refuses the call outright — so this gate can no longer be silently
    #     falsified by an absent flag. The move STAYS correct: it is the right
    #     home on its own merits (state-update is where the outcome is
    #     authoritative), and this file should not depend on a barrier ~450
    #     lines away for a write it can site correctly instead.
    #     Worth carrying: this was the SECOND consumer the optional flag
    #     silently disarmed, independent of the uncommitted-work gate that
    #     motivated g-115-4996. One optional flag, two unrelated gates falsified,
    #     both failing SILENTLY and in the permissive direction.
    # Routine outcomes stay silent (skip-rule: spark is deep-only). Recurring
    # closes skip this block — recurring-close.sh writes the sentinel itself
    # with the POST-FLIP outcome, which this phase cannot know.
    if [[ "$GOAL_STATUS" == "completed" && "$OUTCOME" == "deep" && "$IS_RECURRING" != "true" ]]; then
        # In-turn prompt. Since g-115-4996 made --outcome required at do_verify's
        # entry, OUTCOME is always populated here, so this fires on every deep
        # non-recurring close rather than only when the caller remembered the
        # flag. do_state_update's sentinel remains the backstop for the case this
        # stdout line is emitted but not acted on.
        if [[ "${BODY_ROLE:-}" == "worker" ]]; then
            # A WORKER Body reaches this close via worker-loop Phase 4a and
            # its spark obligation is Phase 3.5 spark_capture (replayed by
            # the reducer at generalize-down) — Skill(aspirations-spark) is
            # reducer-only (REDUCER_ONLY_PHASES). Emitting the reducer's
            # imperative here would contradict worker-loop Phase 4c and
            # invite a worker to run a phase it must skip (2026-08-16,
            # g-115-6337 review). Say what the worker's next step IS.
            echo "[iteration-close] NEXT (worker Body): spark for $GOAL_ID was captured in worker-loop Phase 3.5 (spark_capture; the reducer replays it) — do NOT invoke Skill(aspirations-spark). Continue to worker-loop Phase 4b (hand-off row) then Phase 5 (re-enter for the next unit)."
        else
            echo "[iteration-close] NEXT: Phase 6 spark REQUIRED for $GOAL_ID (outcome=deep, non-recurring) — invoke Skill(aspirations-spark) BEFORE the state-update phase. In-turn spark is recorded by spark-fire-dedup; the sentinel self-clears either way."
        fi
    fi
    # ── End Phase-6 spark imperative ──────────────────────────────────────────

    _checkpoint_refresh verify
    # LLM residue at this phase: Q1/Q2/Q3 escalation, output summary generation.
    # See core/config/iteration-close-digest.md § VERIFY.
}

# --------------------------- phase: state-update ---------------------------
do_state_update() {
    _CURRENT_PHASE="state-update"
    [[ -z "$GOAL_ID" || -z "$SOURCE" || -z "$OUTCOME" ]] && {
        echo "state-update: --goal, --source, --outcome required" >&2
        echo "  usage: iteration-close.sh --phase state-update --goal <id> --source <world|agent> --outcome <deep|routine>" >&2
        # THE FOUR OPTIONAL QUALITY FLAGS ARE NAMED HERE BECAUSE THEY ARE ONE-SHOT
        # (g-115-4283). They were listed only in the top-level usage at the head of
        # this file, so an agent that consulted THIS per-phase line -- the careful
        # path -- saw a complete-looking invocation, omitted them, and silently
        # skipped the imp@k snapshot. Their absence is reported only AFTER a
        # successful close, in the "deep close UNMEASURED" LLM-ACTION line, by
        # which point guard-1235 forbids a post-hoc amend and re-running the phase
        # re-bumps the loop-state counters and re-commits. So the measurement is
        # permanently unrecoverable for that goal: a one-shot flag MUST appear in
        # the usage string a caller actually reads.
        echo "         optional (deep closes): [--tree-updated] [--artifacts-count <n>] [--encoding-score <0.0-1.0>] [--findings-count <n>]" >&2
        echo "         omitting them on a deep close SKIPS the imp@k snapshot IRRECOVERABLY -- it cannot be added afterward" >&2
        exit 2;
    }
    echo "[iteration-close] state-update: goal=$GOAL_ID outcome=$OUTCOME"

    # g-115-5001: read the goal record forward before reporting success over it.
    # Unconditional and on the success path — the whole defect is that every
    # existing read sits behind rc!=0. g-115-5573: that read now REFUSES rather
    # than warning, so a phase can no longer report success over an open record.
    # Invoked BARE so `set -euo pipefail` (L65) turns the non-zero return into a
    # loud abort whose EXIT trap prints the verify-first retry command.
    # See _assert_verify_landed for the fail-open ladder.
    _assert_verify_landed "state-update"

    # ── Phase-6 spark sentinel for NON-recurring deep closes (g-115-2848) ──
    # MOVED here from do_verify: do_state_update REQUIRES --outcome (validated
    # above), so OUTCOME is guaranteed present — unlike do_verify where --outcome
    # is optional and an omission silently defeated the g-115-2416 backstop
    # (g-115-2839: a deep non-recurring verify without --outcome wrote no
    # sentinel; Phase 6 spark was skipped). Recurring goals get their sentinel
    # from recurring-close.sh's end-of-script write (POST-FLIP outcome), so guard
    # on !recurring to avoid a double-write. Routine outcomes skip (spark is
    # deep-only).
    #
    # DEDUP (g-115-3351 — the window is ELIMINATED here, not widened again).
    # This sentinel is written AFTER the in-turn spark (Phase 6 runs between
    # verify and state-update), so fired_at < set_at is the NORMAL shape. The
    # read side used to absorb that with a lookback window, and that bound was
    # outgrown FOUR times (~10m08s -> widened to 15; then 15m09s, 16m31s,
    # 24m00s across three agents on three days) because it was bounding the
    # LLM's Phase-6 work, which is UNBOUNDED BY DESIGN. Worse, the failures are
    # left-censored — a gap under the bound dedups correctly and is never
    # reported — so no field data can ever calibrate it.
    # The write site already knows the goal_id, so it simply asks whether the
    # in-turn spark ALREADY fired for this goal and, if so, does not write the
    # sentinel at all. That makes the in-turn path timing-free and leaves the
    # sentinel doing only its real job: covering the bg-timeout path where no
    # in-turn fire happened. Sound because a NON-recurring goal closes exactly
    # once, so any recorded fire is necessarily THIS close's.
    # PRECISION (fresh-eyes F-1, same day): "no timing window" is exact WITHIN an
    # iteration, not unconditionally. spark_fired_session is pruned at
    # DEFAULT_PRUNE_MIN=90, so an entry older than 90min is dropped — but the ONLY
    # thing that prunes is another spark's `record`, and sparks serialize within
    # the loop, so nothing can record between THIS goal's Phase 6 and its Phase 8.
    # The 90min prune is therefore a CROSS-iteration horizon that cannot fire
    # mid-iteration. State it that way rather than "absent": the whole point of
    # this fix is that an over-general claim about a bound is what went wrong.
    # Fail-open: any error in the probe yields "write" — a redundant sentinel
    # costs at most one extra spark, while a wrongly-suppressed one could lose
    # the spark entirely. The read-side guard remains as defense-in-depth for
    # exactly that degraded path (payload carries `producer`, which drops the
    # consumer's lower bound for this producer only).
    local _su_is_recurring
    _su_is_recurring="$(_probe_is_recurring)"
    if [[ "$OUTCOME" == "deep" && "$_su_is_recurring" != "true" ]]; then
        local _sp_expires _sp_setat _sp_payload _sp_gate _sp_verdict _sp_gap
        _sp_expires="$(python3 -c "from datetime import datetime, timedelta; print((datetime.now() + timedelta(minutes=60)).isoformat(timespec='seconds'))" 2>/dev/null || true)"
        _sp_setat="$(python3 -c "from datetime import datetime; print(datetime.now().isoformat(timespec='seconds'))" 2>/dev/null || true)"

        # Write-side gate + UNCENSORED gap measurement in one pass. Prints
        # "write|skip-write[<TAB>gap_seconds]". bash->bash->python is fine here
        # (the rb-225/rb-247 hang is python SPAWNING bash, not this direction).
        _sp_gate="$(bash "$SCRIPT_DIR/wm-read.sh" spark_fired_session --json 2>/dev/null \
                    | python3 "$SCRIPT_DIR/spark-fire-dedup.py" fired "$GOAL_ID" --set-at "$_sp_setat" 2>/dev/null || true)"
        [[ -z "$_sp_gate" ]] && _sp_gate="write"   # fail-open
        _sp_verdict="${_sp_gate%%$'\t'*}"
        _sp_gap=""
        [[ "$_sp_gate" == *$'\t'* ]] && _sp_gap="${_sp_gate##*$'\t'}"

        # Telemetry: log the gap for EVERY non-recurring deep close, including
        # the ones that dedup correctly. Logging only the near-misses would keep
        # the sample censored at the bound; logging every gap is what removes the
        # censoring and yields the real distribution (g-115-3351's third
        # confirmation). gap_seconds is null when no in-turn fire was recorded.
        SG_GOAL="$GOAL_ID" SG_AGENT="${MIND_AGENT:-unknown}" SG_GAP="$_sp_gap" \
        SG_VERDICT="$_sp_verdict" SG_SETAT="$_sp_setat" \
        SG_FILE="$AGENT_DIR/session/spark-gap-telemetry.jsonl" \
        python3 -c '
import json, os
gap = os.environ.get("SG_GAP", "")
rec = {
    "goal_id":      os.environ["SG_GOAL"],
    "agent":        os.environ["SG_AGENT"],
    "gap_seconds":  (float(gap) if gap else None),
    "sentinel_written": os.environ["SG_VERDICT"] != "skip-write",
    "set_at":       os.environ.get("SG_SETAT") or None,
}
path = os.environ["SG_FILE"]
with open(path, "a", encoding="utf-8") as f:
    f.write(json.dumps(rec) + "\n")
# ROTATION (guard-583): an append-only telemetry file MUST have a rotation
# policy in place, not just a .gitignore entry -- the canonical failure
# (.bash-inject-misses.jsonl) had the ignore rule and still grew unbounded.
# Keep the newest KEEP lines once the file exceeds CAP. The distribution this
# feeds is a recent-behavior question, so old rows carry no analytic value.
# SINGLE-WRITER CONTRACT (fresh-eyes F-2, msg-20260728-193700 / g-115-4096):
# this readlines-then-rewrite is UNLOCKED and therefore correct ONLY while
# this site remains the sole writer of the file. That holds today: the file
# is per-agent (session dir), this is the only write site in the repo
# (grep spark-gap-telemetry, 2026-07-30), and do_state_update runs serially
# inside the single bound runner loop (runner-identity gate). If you add a
# SECOND writer (recurring-close, a sweep, another phase), route BOTH through
# a _fileops locked helper first -- a concurrent append during this rewrite
# window is silently dropped, which censors the very distribution the
# telemetry exists to measure. NOTE this comment lives INSIDE a bash
# single-quoted python3 -c block: no apostrophes here, ever.
CAP, KEEP = 2000, 1000
try:
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    if len(lines) > CAP:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines[-KEEP:])
except OSError:
    pass
' 2>/dev/null || true

        if [[ "$_sp_verdict" == "skip-write" ]]; then
            echo "[iteration-close] pending_phase_6_spark: NOT written for $GOAL_ID — in-turn Phase-6 spark already recorded${_sp_gap:+ (gap ${_sp_gap}s)}. Write-site dedup — no timing window within the iteration (g-115-3351)."
        elif [[ -n "$_sp_expires" ]]; then
            # python3 (not py -3) is correct inside this .sh — it sources _paths.sh.
            # Values pass via env (guard-165), never interpolated into the source.
            _sp_payload="$(GID="$GOAL_ID" OUT="$OUTCOME" SRC="$SOURCE" SUM="${SUMMARY:-}" EXP="$_sp_expires" SETAT="$_sp_setat" python3 -c '
import json, os
print(json.dumps({
    "goal_id":    os.environ["GID"],
    "outcome":    os.environ["OUT"],
    "source":     os.environ["SRC"],
    "summary":    os.environ.get("SUM", ""),
    "expires_at": os.environ["EXP"],
    "set_at":     os.environ.get("SETAT", ""),
    # g-115-3351: names WHICH producer wrote this sentinel, so the consumer can
    # drop its lower bound for this path only. recurring-close.sh writes no
    # producer field and keeps the bounded behavior, which is correct there.
    "producer":   "nonrecurring-state-update",
}))
' 2>/dev/null || true)"
            if [[ -n "$_sp_payload" ]]; then
                echo "$_sp_payload" | bash "$SCRIPT_DIR/wm-set.sh" pending_phase_6_spark >/dev/null 2>&1 \
                    || echo "[iteration-close] WARN: pending_phase_6_spark sentinel write failed (non-fatal — verify stdout imperative remains)" >&2
            else
                echo "[iteration-close] WARN: could not build pending_phase_6_spark payload (non-fatal)" >&2
            fi
        else
            echo "[iteration-close] WARN: could not compute expires_at for pending_phase_6_spark (non-fatal)" >&2
        fi
    fi
    # ── End Phase-6 spark sentinel ──

    # ── Phase 4.25 PER-GOAL experience check for NON-recurring deep closes ──
    # (g-115-4661.) The per-goal remedy already existed — it was wired to the one
    # path that needed it least. recurring-close.sh runs it keyed on the specific
    # goal_id; this path ran ONLY experience-staleness-check.sh (in
    # do_productivity_check below), which is STORE-level: it warns when the newest
    # entry of ANY kind exceeds 12h and has no goal_id join at all. So a
    # non-recurring deep close with no record is structurally invisible to it, by
    # that check's own contract — measured live on cc-03 with an experience.jsonl
    # ~1h fresh (canary correctly silent) while three deep goals had closed that
    # day with no record. Coverage: 16-32% here vs 95% on the recurring lane.
    #
    # Same helper, same 30-min window, same goal_id/source_goal dual match, same
    # payload shape Phase 0-pre2 consumes. The store-level check is NOT changed —
    # it is correct for what it measures and stays as the long-horizon backstop.
    #
    # Gated exactly like the spark sentinel above and for the same reason: this
    # phase REQUIRES --outcome (validated at function entry), whereas do_verify's
    # is optional and an omission silently no-ops the write (g-115-2839). Reuses
    # the $_su_is_recurring probe already computed for that block — recurring
    # goals get this from recurring-close.sh, so firing here too would double-write.
    #
    # `_winpath` per the file-local rule for every `python3 <file-arg>` call.
    # Degradation VISIBLE, never a bare `|| true`: the helper always exits 0 and
    # reports its own skips on stderr, so a non-zero rc here means it was never
    # reached (missing file, interpreter failure) — precisely the case the helper
    # cannot report on itself. stderr is deliberately UNREDIRECTED so the sentinel
    # line reaches the loop LLM in-turn, the same call-site reasoning as
    # iteration-push.sh above.
    if [[ "$OUTCOME" == "deep" && "$_su_is_recurring" != "true" ]]; then
        python3 "$(_winpath "$SCRIPT_DIR/per-goal-experience-check.py")" \
            --goal-id "$GOAL_ID" \
            --trigger "nonrecurring-state-update-deep-no-recent-entry" \
            || echo "[iteration-close] WARN: per-goal experience check did not run for $GOAL_ID (rc=$?) — Phase 4.25 enforcement SKIPPED this close; the 12h store-level backstop still applies" >&2
    fi
    # ── End Phase 4.25 per-goal experience check ──

    # ── exp_capture drain: RETIRED here (g-306-280) — do not re-add ──
    # ⚠ THE PREMISE BELOW IS FALSE — measured g-306-203, 2026-08-11. The
    # "concurrent worker Body had already shipped" claim was never true:
    # worker_retrospective.py has NO `experience` RUN_LANE and no reference to
    # exp_capture in ANY of its four revisions (RUN_LANES is ("team_state",
    # "journal", "findings", "impk"); `git show <rev>:...` returns 0 for both
    # `experience` and `exp_capture` at every rev). So this site's drain was
    # retired as a duplicate of something that does not exist, and exp_capture
    # has had ZERO consumers since — sitting at its cap of 20, where the next
    # worker capture becomes the first to FIFO-evict the oldest.
    #
    # Still do NOT re-add it HERE: the placement argument below is independent
    # of the false ownership claim and remains correct. The remedy is to BUILD
    # the `experience` lane in worker_retrospective.py, which is where both this
    # tombstone and body-merge.sh already say it lives. See body-merge.sh (~L25)
    # for the same correction and the reason not to revert g-306-280.
    #
    # ── RESOLVED 2026-08-15 (measured, alpha worker cc-07). THE REMEDY LANDED,
    # so the two paragraphs above are HISTORY and no longer describe live state.
    # worker_retrospective.py RUN_LANES is now ("team_state", "journal",
    # "findings", "experience", "impk") with `_lane_experience` (~L525) and
    # EXP_SLOT = "exp_capture" (L138). exp_capture HAS a consumer; it is NOT at
    # cap-with-no-drain; the deferral target this tombstone points at exists, so
    # "do not re-add it HERE" is now settled rather than provisional. Read the
    # module before re-deriving the lane's status from this comment — the text
    # above is kept for the placement reasoning, which is still right. Corrected
    # in body-merge.sh (~L41) in the same change.
    #
    # CLOSED 2026-08-22 (g-306-327) — encoding_capture HAS a consumer now, and it
    # landed at the SAME site the exp_capture lane did, not here.
    # worker_retrospective.py RUN_LANES includes "encoding" (L133); ENC_SLOT =
    # "encoding_capture" (L164); `_lane_encoding` (L666) dispatched at L788;
    # `load_enc_captures` (L496) called at L933/L947. All four capture lanes now
    # have drains, so "do not re-add a drain HERE" is settled for encoding_capture
    # on the same reasoning it was settled for exp_capture above. Twins corrected
    # in the same change: wm.py (~L513), wm_write.py (~L113), body-merge.sh (~L57).
    # ORIGINAL (retained; its census discipline is still right):
    #   "STILL OPEN — encoding_capture, the LAST orphaned lane of the four, has no
    #    consumer anywhere ... Owner: g-306-201 (HIGH, pending), half-shipped."
    #
    # ORIGINAL (retained verbatim; its first paragraph is the false one):
    # A drain stood at this site (g-306-199) and was a DUPLICATE: a concurrent
    # worker Body had already shipped the same capability as worker_retrospective.py's
    # `experience` RUN_LANE. Theirs wins on idempotency (goal-keyed
    # `retrospective_encoded` on the DURABLE SHARED goal record, vs this site's
    # per-box ephemeral WM slot) and on bounding anchor count.
    #
    # It is NOT re-constructible here, which is why this is a tombstone and not a
    # relocation note: the surviving lane is MERGE-scoped (argparse requires
    # --goal-ids or --from-merge-summary) and `merged_goal_ids` is computed inside
    # body-merge.py. do_state_update has no merge summary to pass. The lane is now
    # chained from core/scripts/body-merge.sh — still bash-owned, so guard-399's
    # "WHO executes it" test is satisfied without an LLM-elected SKILL.md line.

    # g-115-453: session YAML lint (advisory). Catches malformed YAML in
    # <agent>/session/*.yaml at write-time rather than days later when a
    # downstream consumer (fresh-eyes-cadence-check, blocker-recheck, etc.)
    # silently swallows the YAMLError and fails open. Sister to guard-487
    # (suppression-gate fail-CLOSED on parse error) — that's the consumer
    # defense; this is the writer-side observability. Always exits 0; errors
    # surface via stderr + <agent>/session/yaml-lint-errors.jsonl. Does NOT
    # block the state-update — running while another tool mid-edits a
    # session YAML would yield false positives. The `|| true` guarantees the
    # state-update path keeps moving even if the lint script itself errors.
    bash "$SCRIPT_DIR/session-yaml-lint.sh" >/dev/null 2>&1 || true

    # g-242-10: Phase 4.26 explicit-feedback gate (rb-472 / guard-415 writer-layer).
    # Refuse to mark the goal complete when retrieval ran but produced zero
    # positive signal (utilization_method=all_noise, or method=infer with
    # helpful=0). Override path: pass --no-retrieval-applicable "<reason>" to
    # this script — the gate logs the override to
    # world/phase-4-26-overrides.jsonl and proceeds. Verdicts that pass
    # silently: retrieval_performed=false, empty population, method=manual,
    # method=all_helpful, method=infer with helpful>0, stale-session-id
    # mismatch (fail-open). See core/scripts/phase-4-26-gate.py docstring.
    #
    # PRODUCER FIRST (g-115-3123): run the utilization repair immediately BEFORE
    # the gate that consumes its output. The repair previously lived only in
    # do_learning_gate — one full phase LATER — so every manifest reached this
    # gate with utilization_pending still true and no method recorded. Ordering
    # is the whole fix: same helper, called where its consumer can see the result.
    _repair_utilization_pending
    local gate_args=(--goal "$GOAL_ID")
    if [[ -n "$NO_RETRIEVAL_APPLICABLE" ]]; then
        gate_args+=(--no-retrieval-applicable "$NO_RETRIEVAL_APPLICABLE")
    fi
    if ! bash "$SCRIPT_DIR/phase-4-26-gate.sh" "${gate_args[@]}"; then
        echo "[iteration-close] BLOCKED: Phase 4.26 gate refuses state-update for $GOAL_ID." >&2
        echo "[iteration-close] To proceed: rerun with --no-retrieval-applicable \"<reason>\" OR run utilization-feedback.sh manually with explicit --helpful items." >&2
        exit 1
    fi

    # Bookkeeping: meta last_updated, session_count already handled elsewhere.
    bash "$SCRIPT_DIR/aspirations-meta-update.sh" --source "$SOURCE" last_updated "$TODAY"

    # Working memory: append goal completion to session-level counter.
    # Emits BOTH asp_id and recurring in one tab-separated line — goal-selector.py
    # reads `recurring` from this record to compute recurring_saturation (see
    # goal-selector.py `recurring_count = sum(... s.get("recurring", False))`).
    # A hardcoded default here silently defeats that penalty, so look up the
    # real flag from the same aspiration read.
    # Same no-shell-interpolation rule as _checkpoint_refresh: values flow via
    # environment, Python source is single-quoted so bash does no expansion.
    # Tier 1: read the combined compact index (fast-path, cached).
    # Tier 2: read source JSONL directly — bypasses subprocess.run which
    # on Windows (a) hangs on large stdout (g-001-125, iter 52) and
    # (b) doesn't propagate MIND_AGENT to child bash (g-001-127, iter 56).
    # Source JSONL is newline-delimited JSON, each line is one aspiration.
    local compact_file="$AGENT_DIR/session/aspirations-compact.json"
    local agent_jsonl="$AGENT_DIR/aspirations.jsonl"
    local world_jsonl="$WORLD_DIR/aspirations.jsonl"
    local lookup
    # CORE_SCRIPTS uses $CORE_ROOT/scripts (converted to Windows path by _platform.sh)
    # rather than $SCRIPT_DIR (set via BASH_SOURCE pwd — stays in MSYS /c/... format
    # on Git Bash, which Python treats as relative-to-drive-root and silently fails
    # to resolve). Without the converted path, `from _work_class import resolve`
    # raises ModuleNotFoundError → python exits nonzero → the `||` fallback fires →
    # all 4 fields empty → category defaults to "uncategorized". The live manifest
    # of this bug was 9 consecutive goals written as "uncategorized" despite their
    # compact records carrying explicit categories (g-115-175 finding, 2026-04-24).
    lookup="$(COMPACT="$compact_file" AGENT_J="$agent_jsonl" WORLD_J="$world_jsonl" SRC="$SOURCE" GID="$GOAL_ID" CORE_SCRIPTS="$CORE_ROOT/scripts" python3 -c '
import json, os, sys
gid = os.environ["GID"]
src = os.environ["SRC"]

# rb-431 / guard-367: when goal.work_class is empty, fall back to
# _work_class.resolve(category) so recurring-heavy work classes are not
# silently excluded from session_completions. Downstream scripts
# (self-drift-gate.py, goal-selector.py criterion 7e) filter on
# work_class presence — an empty field becomes systematic bias if the
# writer does not resolve it. Single-source-of-truth: the same resolver
# the scorer already uses (goal-selector.py delegates to _work_class).
_core_scripts = os.environ.get("CORE_SCRIPTS", "")
if _core_scripts and _core_scripts not in sys.path:
    sys.path.insert(0, _core_scripts)
from _work_class import resolve as _resolve_wc

def _wc_for(goal):
    explicit = goal.get("work_class", "") or ""
    if explicit:
        return explicit
    # Fallback via category resolver. If the category maps to a tracked
    # class (product/framework/hygiene/research), populate. If it resolves
    # to "unclassified" (unmapped or missing category), return empty
    # string — matching the pre-patch behavior so self-drift-gate does not
    # absorb unmapped entries into its denominator and skew tracked-class
    # observed fractions downward. rb-431 documents the original bias
    # (missing work_class); leaving unmapped categories empty preserves
    # the current docstring-vs-code inconsistency in self-drift-gate
    # without this patch silently widening it.
    resolved = _resolve_wc(goal.get("category"))
    return "" if resolved == "unclassified" else resolved

def probe_compact(path):
    if not path or not os.path.exists(path):
        return None
    try:
        data = json.load(open(path, encoding="utf-8"))
    except Exception:
        return None
    for a in data:
        for g in a.get("goals", []):
            if g.get("id") == gid:
                return (a.get("id", ""), bool(g.get("recurring")), _wc_for(g), g.get("category", "uncategorized") or "uncategorized")
    return None

def probe_jsonl(path):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    asp = json.loads(line)
                except Exception:
                    continue
                for g in asp.get("goals", []):
                    if g.get("id") == gid:
                        return (asp.get("id", ""), bool(g.get("recurring")), _wc_for(g), g.get("category", "uncategorized") or "uncategorized")
    except Exception:
        return None
    return None

# Tier 1: compact cache (covers previous-iteration goals)
hit = probe_compact(os.environ.get("COMPACT", ""))
# Tier 2: source JSONL (covers same-iteration newly-filed goals)
if hit is None:
    source_jsonl = os.environ.get("AGENT_J" if src == "agent" else "WORLD_J", "")
    hit = probe_jsonl(source_jsonl)

if hit is None:
    print("\t\t\t")
else:
    asp_id, rec, wc, cat = hit
    # Field order asp_id,recurring,category,work_class — category BEFORE
    # work_class so an empty work_class lands TRAILING. A bash read with a tab
    # IFS collapses empty MIDDLE fields on MSYS but preserves trailing. Reorder
    # avoids the collapse. g-115-175 root cause (2026-04-24).
    # Do NOT write the ANSI-C tab quote in this comment. It sits inside a
    # single-quoted python block, so its two quote marks close and REOPEN the
    # bash string; an EVEN count still passes bash -n, which is why the prior
    # wording survived undetected (guard-504, g-115-3565).
    print(asp_id + "\t" + ("true" if rec else "false") + "\t" + cat + "\t" + wc)
' || { echo "[iteration-close] WARN: aspiration lookup for $GOAL_ID failed — asp_id/recurring/work_class/category unavailable, append will be skipped" >&2; echo $'\t\t\t'; })"
    # Split 4 tab-separated fields (asp_id, recurring, category, work_class).
    # Field order: category BEFORE work_class because work_class is the only
    # field that may be empty. Bash IFS=$'\t' read collapses middle empties
    # on MSYS so empty must TRAIL. g-115-175 root cause (2026-04-24).
    # work_class may be empty (pre-backfill goals, new categories not yet in
    # the mapping) — criterion 7e excludes empty work_class from both the
    # distribution and the bonus, so an empty string is a valid fail-open.
    local asp_id recurring category work_class
    IFS=$'\t' read -r asp_id recurring category work_class <<< "$lookup"
    [[ -z "$category" ]] && category="uncategorized"
    # NO silent recurring="false" default. The python probe (probe_compact /
    # probe_jsonl above) always emits either both fields populated or both
    # empty via `print("\t")`. If recurring arrives empty while asp_id is
    # populated, the data is malformed — treat it as a lookup failure rather
    # than silently poisoning goal-selector.py's recurring_saturation scoring.

    # CORRECTNESS-CRITICAL: goal-selector.py reads these WM slots to score the next
    # iteration. Silent wm-append failure means the completion is invisible to the
    # selector — recurring_saturation + streak_momentum go stale. Surface errors
    # loudly; keep `|| true` so one failure doesn't abort remaining bookkeeping.
    if [[ -n "$asp_id" && -n "$recurring" ]]; then
        # work_class is optional. Scorer criterion 7e treats missing as
        # "unclassified" (excluded from computation) — no failure mode from
        # an empty value. Resolved from goal.work_class in the probe above.
        if [[ -n "$work_class" ]]; then
            echo "{\"goal_id\":\"$GOAL_ID\",\"aspiration_id\":\"$asp_id\",\"recurring\":$recurring,\"work_class\":\"$work_class\"}" | \
                bash "$SCRIPT_DIR/wm-append.sh" goals_completed_this_session || echo "[iteration-close] WARN: wm-append goals_completed_this_session failed for $GOAL_ID" >&2
        else
            echo "{\"goal_id\":\"$GOAL_ID\",\"aspiration_id\":\"$asp_id\",\"recurring\":$recurring}" | \
                bash "$SCRIPT_DIR/wm-append.sh" goals_completed_this_session || echo "[iteration-close] WARN: wm-append goals_completed_this_session failed for $GOAL_ID" >&2
        fi
        echo "\"$asp_id\"" | bash "$SCRIPT_DIR/wm-set.sh" aspiration_touched_last || echo "[iteration-close] WARN: wm-set aspiration_touched_last failed for $asp_id" >&2

        # g-283-06: loop_state cross-session counter bump (post-g-283-04 mirror retirement).
        # The retired LLM-side mirror at LOOP_CONTINUE used to write
        # loop_state.goals_completed and loop_state.productive_goals; without
        # this bump, productivity-stop-gate.sh's `goals_completed = loop_state.get(...)`
        # silently reads a frozen counter and the stop gate never fires.
        # Recurring-loop-state-mutate.py is the SSOT for goals_completed_this_session
        # (different field, _this_session suffix); this writer covers the cross-session
        # field both recurring and non-recurring paths need to advance.
        # Cross-references: g-283-04 (the regression), g-283-03 (shape-invariance test
        # that passed despite this gap — counter-advance now pinned in
        # test_loop_state_counter_advance.py).
        # cygpath: same pattern as recurring-precondition-sweep.py invocation below —
        # convert SCRIPT_DIR to Windows path for python3 on Windows.
        # g-115-664: pass --goal-id so the helper can refuse a double-bump on
        # state-update retry (bravo session 66 saw rc=127 → retry → double-bump).
        # >>> RELOCATED (zeta s80 freeze-fix): the loop_state bump is now an
        # UNCONDITIONAL call AFTER this block's `fi` - it must advance on every
        # state-update close, not only when the aspiration lookup above succeeds.
        # See the `[[ -n "$GOAL_ID" ]]` bump just past the block close.

        # g-001-217: sessions_active counter increment (Phase 8.1 implementation).
        # The aspirations-loop-digest.md:154 pseudocode "asp.sessions_active += 1"
        # had no bash writer for months — all 17 active aspirations stuck at 0.
        # Maturity gate (aspirations.py:2031) and stalled-detection (precheck-eval.py:399)
        # were systematically wrong. Track first-time-per-session per agent in
        # <agent>/session/aspirations-incremented-session-{N}.txt; bash-owned for
        # SSOT (rb-686 / rb-428 LLM-residue → bash promotion pattern).
        # Fail-open at every layer — never block iteration-close.
        local sa_session_count
        sa_session_count="$(bash "$SCRIPT_DIR/aspirations-read.sh" --meta 2>/dev/null \
            | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("session_count",0))
except: print(0)' 2>/dev/null || echo 0)"
        local sa_track="$AGENT_DIR/session/aspirations-incremented-session-${sa_session_count}.txt"
        if ! grep -qx "$asp_id" "$sa_track" 2>/dev/null; then
            mkdir -p "$AGENT_DIR/session" 2>/dev/null || true
            echo "$asp_id" >> "$sa_track"
            # Use $CORE_ROOT/scripts (Windows-converted path) not $SCRIPT_DIR
            # which stays in MSYS /c/... format and confuses Python on Windows.
            # Same pattern the cmd_state_update python invocation above uses.
            ASP_ID="$asp_id" SOURCE="$SOURCE" python3 "$CORE_ROOT/scripts/aspirations-increment-sessions-active.py" \
                || echo "[iteration-close] WARN: sessions_active increment failed for $asp_id (fail-open)" >&2
        fi
    else
        # Lookup-failed case: either the aspirations-read couldn't locate GOAL_ID
        # (deleted between execution and state-update, or typo in caller), OR
        # the record was malformed and the recurring flag came back empty. WM
        # appends would be meaningless or actively wrong, but we must not
        # silently swallow the mismatch — recurring_saturation & streak_momentum
        # go stale.
        echo "[iteration-close] WARN: lookup for goal $GOAL_ID in $SOURCE aspirations returned asp_id='$asp_id' recurring='$recurring' — goals_completed_this_session + aspiration_touched_last skipped" >&2
    fi

    # LOOP-STATE FREEZE FIX (zeta s80, 2026-06-01): the cross-session loop_state
    # counter bump (g-283-06) runs here UNCONDITIONALLY, decoupled from the
    # aspiration-lookup gate above. It was previously nested inside
    # `[[ -n "$asp_id" && -n "$recurring" ]]`, so whenever that lookup returned
    # hit=None (goal not in COMPACT or source JSONL), the entire block - including
    # this bump - silently skipped and loop_state.goals_completed froze. Observed
    # signature: slot_meta.loop_state stuck at update_count=1 for 3+ days while
    # goals kept closing; productivity-stop-gate read a frozen counter; session
    # counters reset on every compaction because the checkpoint captured the null.
    # The bump only needs $OUTCOME + $GOAL_ID (--goal is required for state-update)
    # and --goal-id gives idempotency (g-115-664), so an unconditional placement
    # cannot double-bump on retry. The wm-append (goals_completed_this_session)
    # and sessions_active writers genuinely need asp_id and correctly stay inside
    # the lookup-gated block above.
    if [[ -n "$GOAL_ID" ]]; then
        local _lsbc
        _lsbc="$(_winpath "$SCRIPT_DIR/loop-state-bump-counters.py")"
        # g-115-1785: pass --recurring false ONLY for a confirmed non-recurring
        # goal so loop-state-bump-counters.py ALSO owns the non-recurring Block
        # A/B/C/D streak mutation (the streaks previously had a recurring bash
        # writer but no non-recurring one — the digest's LLM-manual path drifted
        # on interrupted closes). Recurring goals ("true") get streaks from
        # recurring-loop-state-mutate.py via recurring-close.sh; passing the flag
        # for them would DOUBLE-apply. Unknown ("" — the aspiration lookup above
        # failed) → omit the flag → skip streaks (fail-safe: no corruption, the
        # streak just doesn't advance this once, exactly as pre-g-115-1785).
        # $recurring is the local set by the lookup at the top of this function.
        local _rec_flag=()
        [[ "${recurring:-}" == "false" ]] && _rec_flag=(--recurring false)
        python3 "$_lsbc" --outcome "$OUTCOME" --goal-id "$GOAL_ID" "${_rec_flag[@]}" \
            || echo "[iteration-close] WARN: loop-state-bump-counters failed for $GOAL_ID (fail-open; productivity-gate may stay stale this iteration)" >&2
        # g-115-1470: the bump always exits 0 (fail-open at every layer), so rc
        # CANNOT detect a silent no-op (stale-lock-steal / WM-write failure under
        # the OneDrive+daemon background latency of guard-685 / g-115-1349). When
        # do_state_update BACKGROUNDS on a deep close, the bump's stderr WARN is
        # dropped from the task-output file, so the undercount is invisible and
        # biases productivity-stop-gate math. Re-read the idempotency list: when
        # GOAL_ID is confidently absent the bump did not take -> record it DURABLY
        # (a queryable ledger that survives the lost stderr) and re-fire ONCE
        # foreground. The re-fire is idempotent (g-115-664: counted_goals_this_session
        # membership gate per rb-1823), so a spurious re-fire after a torn verify
        # read is a harmless no-op.
        if ! python3 "$_lsbc" --verify-counted "$GOAL_ID" 2>/dev/null; then
            printf '{"ts":"%s","goal_id":"%s","outcome":"%s","event":"bump_noop_detected","action":"refire"}\n' \
                "$NOW_ISO" "$GOAL_ID" "$OUTCOME" \
                >> "$AGENT_DIR/session/loop-state-bump-failures.jsonl" 2>/dev/null || true
            python3 "$_lsbc" --outcome "$OUTCOME" --goal-id "$GOAL_ID" "${_rec_flag[@]}" \
                || echo "[iteration-close] WARN: loop-state-bump re-fire failed for $GOAL_ID" >&2
        fi
    fi
    echo "\"$SOURCE\"" | bash "$SCRIPT_DIR/wm-set.sh" current_goal_source || echo "[iteration-close] WARN: wm-set current_goal_source failed" >&2

    # CORRECTNESS-CRITICAL: team state drives multi-agent coordination. Silent
    # write failure means other agents see stale state. Surface, don't abort.
    local key_finding="${SUMMARY:-completed}"
    # Escape backslashes FIRST, then double-quotes, then collapse newlines —
    # otherwise the L455 heredoc JSON breaks on any Windows path or regex
    # pattern in $SUMMARY (a literal `\` would terminate string parsing or
    # land mid-escape). Order matters: backslashes must be doubled BEFORE
    # quote-escape, otherwise the freshly-inserted `\"` from quote-escape
    # would be mangled by a later backslash pass. (g-115-384 / bravo-fec-
    # iter-close-backslash F-003.)
    key_finding="$(printf '%s' "$key_finding" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr '\n' ' ')"
    # BOUND, stated deliberately (g-115-5365 scope item 1). `completed_by` below
    # is "$AGENT" — the CALLER of this close, not necessarily the executor. On
    # the Mind/Body split a WORKER executes and a REDUCER closes, so those two
    # differ by design; on a sweep or bulk close they differ because the closer never
    # touched the goal. Nothing pops this field, so the value persists.
    # `executed_by` is deliberately NOT carried here — see the matching note at
    # the daemon writer (aspirations_write.py, complete-by team-state
    # cross-write). The goal record is the authority and carries both fields;
    # a reader wanting the executor JOINS on goal_id rather than trusting a
    # second copy that only one of the two writers could populate.
    bash "$SCRIPT_DIR/team-state-update.sh" \
        --field recent_completions --operation append \
        --value "{\"goal_id\":\"$GOAL_ID\",\"completed_by\":\"$AGENT\",\"completed_at\":\"$NOW_ISO\",\"key_finding\":\"$key_finding\"}" || echo "[iteration-close] WARN: team-state-update recent_completions failed for $GOAL_ID" >&2
    # g-284-06: team-state-clear-in-flight MOVED to do_verify Step 3. The
    # canonical writers of agent_status.<self>.last_active are still:
    # cmd_in_flight (claim), cmd_clear_in_flight (now invoked from do_verify
    # + release/skip paths), aspirations/SKILL.md Phase -0.5 (per-iteration
    # heartbeat), and start/SKILL.md autonomous (session-start seed). DO NOT
    # re-add a clear here — duplicating it in state-update reopens the
    # 3-store split-brain window between the verify writes and this point.

    # Journal entry — delegated to journal-append.sh (g-248-35, rb-428 family).
    # The wrapper handles markdown templating, citation scan, and journal-index
    # merge/add fallback as a single bash-enforced unit. See journal-append.sh
    # for the section-header contract and the active_context session resolution.
    #
    # Retrieval influence (G10 / R12): aspirations-execute Phase 4 Step 5c
    # writes a one-line articulation to working-memory slot
    # `retrieval_influence_last`. wm-read returns plain text — the literal
    # token "null" means the slot is unset. Treat that as empty so no
    # journal line is emitted. Single source of truth: the slot value IS
    # the influence line (no JSON layer, no parsing).
    _retrieval_influence="$(bash "$SCRIPT_DIR/wm-read.sh" retrieval_influence_last 2>/dev/null || echo "")"
    [[ "$_retrieval_influence" == "null" ]] && _retrieval_influence=""
    # --work-class (g-317-15): pass the already-resolved work_class so
    # journal-append.sh can derive the FW-5 value-framing presentation label.
    # Additive + fail-open: empty work_class -> flag omitted -> the writer
    # falls to the outcome_class's `unclassified` framing. No existing field
    # or control-flow touched (hot-script discipline per the goal note).
    bash "$SCRIPT_DIR/journal-append.sh" \
        --goal "$GOAL_ID" \
        --outcome-class "$OUTCOME" \
        ${SUMMARY:+--summary "$SUMMARY"} \
        ${_retrieval_influence:+--retrieval-influence "$_retrieval_influence"} \
        ${work_class:+--work-class "$work_class"} \
        || echo "[iteration-close] WARN: journal-append failed for $GOAL_ID (fail-open; iteration continues)" >&2
    # Reset the slot to null so a Phase 4 that articulates "no influence"
    # for the NEXT iteration produces an explicit signal, not a stale
    # carry-over from this iteration.
    echo 'null' | bash "$SCRIPT_DIR/wm-set.sh" retrieval_influence_last >/dev/null 2>&1 || true

    # g-280-03: iteration-commit wiring. Wraps post-execution.md Step 2
    # commit ceremony for the Mind repo (PROJECT_ROOT). Skipped on routine
    # outcomes by the script's own routine-no-op gate. Fail-open: a commit
    # failure (sensitive-pattern-only changes, dirty working tree owned by
    # partner agent, etc.) MUST NOT block state-update.
    # Title resolution: look up goal title from compact-or-jsonl using the
    # same Tier-1/Tier-2 pattern as the recurring/work_class probe above.
    # Falls back to GOAL_ID when title lookup fails — iteration-commit.sh's
    # type auto-derivation still runs on the literal goal-id prefix (defaults
    # to chore), commit message remains informative.
    if [[ "$OUTCOME" == "deep" ]]; then
        local _title_lookup
        _title_lookup="$(COMPACT="$compact_file" AGENT_J="$agent_jsonl" WORLD_J="$world_jsonl" SRC="$SOURCE" GID="$GOAL_ID" python3 -c '
import json, os
gid = os.environ["GID"]
src = os.environ["SRC"]
def probe_compact(p):
    if not p or not os.path.exists(p):
        return None
    try:
        for a in json.load(open(p, encoding="utf-8")):
            for g in a.get("goals", []):
                if g.get("id") == gid:
                    return g.get("title", "")
    except Exception:
        return None
    return None
def probe_jsonl(p):
    if not p or not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    asp = json.loads(line)
                except Exception:
                    continue
                for g in asp.get("goals", []):
                    if g.get("id") == gid:
                        return g.get("title", "")
    except Exception:
        return None
    return None
title = probe_compact(os.environ.get("COMPACT", ""))
if not title:
    sj = os.environ.get("AGENT_J" if src == "agent" else "WORLD_J", "")
    title = probe_jsonl(sj)
print(title or "")
' 2>/dev/null || echo "")"
        # Empty title fallback: use GOAL_ID itself. iteration-commit.sh
        # validates non-empty; an empty string would exit 1 noisily.
        local _commit_title="${_title_lookup:-$GOAL_ID}"
        local _commit_output
        _commit_output="$(bash "$SCRIPT_DIR/iteration-commit.sh" \
            --goal-id "$GOAL_ID" \
            --title "$_commit_title" \
            --outcome "$OUTCOME" \
            --repo "$PROJECT_ROOT" 2>&1 || true)"
        # Always log — useful for "did the commit happen?" forensics even
        # when iteration-commit no-ops. fail-open via the `|| true` above.
        echo "[iteration-close] iteration-commit: $_commit_output"

        # g-115-4252: surface attribution DROPS at the close boundary.
        # The drop WARNs live inside $_commit_output, which is echoed as ONE
        # merged multi-line blob under a single prefix — and is lost entirely
        # when this phase call backgrounds past the 2-minute Bash timeout. A
        # dropped file is then silently absent from the commit, and the only
        # diagnostic left afterwards is a grep of the committer's
        # session/uncommitted-edits.jsonl — which is INVERTED BY CONSTRUCTION:
        # iteration-commit.sh's post-commit clear (its g-115-697 block) prunes
        # rows whose path WAS committed and deliberately preserves rows whose
        # path was NOT. So a retained file greps to 0 rows and a dropped file
        # greps to 1, whatever the exemption predicate actually did. That
        # inversion was reproduced against the production clear in g-115-4252,
        # after it had already produced one wrong root-cause ("the predicate
        # does not match its own message"). Two channels, because one is not
        # enough: stdout for the in-turn reader, execution-diary for survival
        # across backgrounding and autocompact (the SessionStart hook surfaces
        # recent diary entries). Fail-open throughout — never block the close.
        local _attr_drops
        _attr_drops="$(printf '%s\n' "$_commit_output" \
            | grep -E 'filtered \((concurrent-partner|partner-uncommitted-log)\):' || true)"
        if [[ -n "$_attr_drops" ]]; then
            local _attr_n
            _attr_n="$(printf '%s\n' "$_attr_drops" | grep -c . || true)"
            echo "[iteration-close] ATTRIBUTION DROP: $_attr_n neutral-path file(s) were filtered OUT of this commit as partner-authored:"
            printf '%s\n' "$_attr_drops"
            echo "[iteration-close] If any of those are YOURS, do NOT diagnose by grepping uncommitted-edits.jsonl — the post-commit clear inverts it (dropped=present, retained=absent). Re-run iteration-commit.sh in-turn and read its exemption/drop lines directly."
            GID="$GOAL_ID" DROPS="$_attr_drops" N="$_attr_n" python3 -c '
import json, os
print(json.dumps({
    "entry_type": "finding",
    "goal_id":    os.environ["GID"],
    "content":    "ATTRIBUTION DROP at close: " + os.environ["N"]
                  + " neutral-path file(s) filtered out of the commit as partner-authored. "
                  + os.environ["DROPS"].replace("\n", " | ")
                  + " -- do NOT diagnose via a post-hoc grep of uncommitted-edits.jsonl; the post-commit clear inverts it (dropped=present, retained=absent). g-115-4252",
}))
' | bash "$SCRIPT_DIR/execution-diary.sh" append || true
        fi

        # g-115-1178: extract commit_sha from iteration-commit's JSON output so
        # the post-state-update gate (Step 8.78 below) scopes its fresh-eyes
        # file-detection to exactly the files THIS commit landed, instead of
        # re-deriving from a working tree that may carry partner WIP at neutral
        # paths (the stranded-partner false-positive class, g-115-1154). The
        # function-scoped local is read by the gate call further down.
        # _commit_output is 2>&1-merged (INFO/warning lines + a trailing JSON
        # line); per guard-559 parse ONLY a line that is itself valid JSON —
        # never json.load the whole blob (the INFO lines raise JSONDecodeError
        # 'Extra data'). Take the last line that parses with a non-empty
        # commit_sha. Empty on no-op/parse-failure → gate falls back to
        # working-tree scope.
        local _commit_sha
        _commit_sha="$(printf '%s\n' "$_commit_output" | python3 -c '
import json, sys
sha = ""
for line in sys.stdin:
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        d = json.loads(line)
    except Exception:
        continue
    s = (d.get("commit_sha") or "").strip()
    if s:
        sha = s
print(sha)
' 2>/dev/null || true)"

        # g-115-746: extend the commit ceremony beyond PROJECT_ROOT to sibling
        # product repos under AGENT_WRITE_PATH (resolved from <agent>/local-
        # paths.conf). The pre-existing --repo "$PROJECT_ROOT" call covers the
        # Mind repo only; the helper covers Ayoai-Environment-Server, Ayoai-
        # Roblox-Integration, and any other product repo placed under the
        # configured write root. Closes the post-execution.md Step 2 gap that
        # produced the 2026-05-14 g-250-85 uncommitted-product-code incident
        # (LOGIT zero-clamp fix sat 3 days until g-115-744 recovery).
        # Fail-open: missing AGENT_WRITE_PATH or a per-repo commit failure
        # MUST NOT block state-update; the helper logs each result and
        # returns 0.
        # shellcheck source=_cross_repo_commit.sh
        source "$SCRIPT_DIR/_cross_repo_commit.sh"
        cross_repo_commit_product \
            --goal-id "$GOAL_ID" \
            --title "$_commit_title" \
            --outcome "$OUTCOME" || true
    fi

    # Write outcome_class to iteration-checkpoint so later readers
    # (obligation-audit.py at do_learning_gate + abbreviated-obligation-audit.sh
    # in aspirations-learning-gate Phase 9.5d) can decide which obligations
    # apply on THIS iteration. bravo FE-001 (2026-04-24): readers polled
    # checkpoint.get("outcome_class") since rb-428 but no writer ever set
    # it, so obligation-audit._validate short-circuited every routine claim
    # with "claim says routine but checkpoint says None". SCHEMA owns the
    # key in loop-state-save.py; writer is wired here. Fail-open via
    # `|| true` matches existing iteration-close convention — audit is
    # non-fatal observability, must never block state-update.
    bash "$CORE_ROOT/scripts/loop-state-save.sh" update \
        --set "outcome_class=$OUTCOME" || true

    _checkpoint_refresh state_update

    # Step 8.78 Post-State-Update Gate (g-248-17, rb-428 pattern).
    # Script-without-caller drift fix: the gate itself (post-state-update-gate.sh)
    # was extracted from aspirations-state-update/SKILL.md:600 but its caller
    # never ported into iteration-close.sh — gate went unfired for 7 days
    # while core_count=22 / loc=473 exceeded thresholds. Per guard-365: when
    # a bash wrapper consolidates LLM-orchestrated steps, every LLM-only
    # step needs explicit wiring. Dispatch is LLM-only (Skill calls can't
    # run from bash), so this writes a WM signal that the next LLM turn
    # reads. Digest § STATE-UPDATE item references the signal.
    # Fail-open: gate errors never block state-update.
    if [[ "$OUTCOME" == "deep" ]]; then
        local gate_json
        # Preserve stderr to log instead of silencing — gate-failed and
        # gate-no-fire were indistinguishable when 2>/dev/null swallowed
        # tracebacks/permission errors. Same fix shape as the audit-pass
        # call below (g-240-70 lineage); fail-open behavior unchanged
        # because `|| echo '{"fired":false}'` still fires on non-zero
        # exit. (g-240-79, fresh-eyes finding from g-001-04 iter-14.)
        mkdir -p "$CORE_ROOT/logs"
        # COMMIT_SHA (g-115-1178): scope the gate to the files iteration-commit
        # just committed. Empty when iteration-commit no-op'd or parse failed →
        # gate falls back to working-tree scope (backward-compatible).
        # GOAL_ID (g-115-2030): lets the gate union in mid-goal commits stamped
        # "(goal-id)" by iteration-commit — the close-time sha alone missed
        # Phase-4 commits (e.g. daemon-restart commits) entirely.
        gate_json=$(COMMIT_SHA="${_commit_sha:-}" GOAL_ID="${GOAL_ID:-}" bash "$SCRIPT_DIR/post-state-update-gate.sh" deep 2>>"$CORE_ROOT/logs/iteration-close-stderr.log" || echo '{"fired":false}')
        local fired
        fired=$(echo "$gate_json" | python3 -c "import json,sys; d=json.load(sys.stdin); print('true' if d.get('fired') else 'false')" 2>/dev/null || echo false)
        if [[ "$fired" == "true" ]]; then
            # Write WM signal the LLM residue checklist consumes. Storing the full
            # gate JSON preserves the files list + reason for the dispatcher.
            # Stamp set_at (g-115-1553) so the stale-sentinel canary's Investigate
            # diagnostic can report when the sentinel was armed vs when the
            # consumer last dispatched. The canary's FIRING logic keys on the
            # consumer's fresh_eyes_last_dispatch advancing (not on set_at) — but
            # set_at gives the investigator the armed-at anchor. Fail-open: if the
            # stamp injection errors, write the un-stamped gate_json unchanged.
            local gate_json_stamped
            gate_json_stamped=$(echo "$gate_json" | python3 -c "import json,sys,datetime; d=json.load(sys.stdin); d['set_at']=datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S'); sys.stdout.write(json.dumps(d))" 2>/dev/null || echo "$gate_json")
            # MERGE, do not overwrite (g-115-4244; found by echo, board
            # msg-20260730-113233-echo-5163 F-003). This write used to be an
            # unconditional wm-set on a slot that holds ONE payload, so two deep
            # closes back-to-back — the normal cadence of a productive session —
            # silently CANCELLED the first close's review obligation: the sentinel
            # reads as satisfied once the second file set is reviewed, and nothing
            # reported the loss. Measured: a close set core_count=10 (commit
            # 4ef80c13d) and was overwritten by core_count=3 before consumption;
            # those 10 files were never reviewed. Refusing the overwrite instead
            # would just lose the NEW set — both obligations are real, so both
            # file sets have to survive. The helper unions BY IDENTITY and
            # RECOMPUTES the derived counts (rb-3399: never carry a stale count
            # past a union); note core_count is NOT len(files), since the gate
            # caps files at 20 while core_count is the true count.
            # Fail-open at every step: an unreadable slot, a helper error, or a
            # non-zero exit all fall back to writing gate_json_stamped unchanged,
            # which is exactly the previous behavior — this can never make the
            # sentinel worse than it was.
            local _fe_existing _fe_merged
            _fe_existing=$(bash "$SCRIPT_DIR/wm-read.sh" fresh_eyes_dispatch_pending --json 2>/dev/null || echo null)
            _fe_merged=$(printf '%s' "$gate_json_stamped" | FRESH_EYES_EXISTING="$_fe_existing" python3 "$SCRIPT_DIR/fresh-eyes-sentinel-merge.py" 2>/dev/null) || _fe_merged=""
            [ -n "$_fe_merged" ] || _fe_merged="$gate_json_stamped"
            echo "$_fe_merged" | bash "$SCRIPT_DIR/wm-set.sh" fresh_eyes_dispatch_pending >/dev/null 2>&1 || true
            # DISPATCH line — LLM reads this in-turn and invokes /fresh-eyes-code.
            # Read the MERGED payload, not gate_json: after the merge above the
            # sentinel can hold more files than THIS close produced, and a banner
            # sourced from gate_json would under-report the real obligation (say
            # "3 core files" while 13 await review). The banner must describe what
            # the consumer will actually find in the slot.
            local core_count reason
            core_count=$(echo "$_fe_merged" | python3 -c "import json,sys; print(json.load(sys.stdin).get('core_count', '?'))" 2>/dev/null || echo "?")
            reason=$(echo "$_fe_merged" | python3 -c "import json,sys; print(json.load(sys.stdin).get('reason', ''))" 2>/dev/null || echo "")
            echo "[iteration-close] DISPATCH: /fresh-eyes-code required — ${core_count} core files ($reason). See WM.fresh_eyes_dispatch_pending for full file list." >&2
        fi
    fi

    # Step 8.79a Branch-landed advisory (g-115-3838). ADVISORY, never blocking.
    #
    # guard-1548 has documented "a completed status is not evidence the
    # deliverable reached main" for a while: retrieval_count 31, times_helpful 0
    # — retrieved often, reaching the CLOSING moment never. Three product goals
    # closed done on 2026-07-29 with their work sitting in green mergeable
    # branches nobody had landed (two for five hours, one for six days), and a
    # terminal goal is invisible to every blocker/defer/blocked-signal sweep by
    # construction, so no sweep could have found them. The gap was enforcement
    # at close, not knowledge.
    #
    # REUSES the existing tier-2 predicate rather than reimplementing ancestry:
    # completed-not-committed-sweep.py already decides "did everything reach the
    # default branch?" via `git branch -r --contains` (the branch-enumeration
    # form the goal preferred over PR-listing, which is blind to a branch pushed
    # without one) and already separates stranded_open_pr from the weaker
    # stranded_no_pr. --goal + --no-fetch (g-115-3838) are what make it callable
    # for ONE goal at close time.
    #
    # --min-age-minutes 0 is REQUIRED: the goal just closed, so it is far younger
    # than the 30-min push-throttle guard and would otherwise be filtered out
    # before any check ran — a silent no-op that would look like a clean pass.
    # --no-fetch is what makes this affordable: measured cc-05, the fetch does
    # not scale with --goal (57 repos regardless) and costs ~24s vs ~1s without.
    # It is sound HERE specifically because this box just pushed, so local
    # origin/* refs already reflect it; the scheduled 24h sweep must keep
    # fetching to catch OTHER boxes' pushes (g-115-2660).
    #
    # ADVISORY by deliberate choice: a hard refusal would wedge the loop whenever
    # review is legitimately outstanding, and every sibling in this family is
    # advisory-with-banner. Fail-open on every path — a probe error must never
    # affect goal closure.
    if [[ -n "${GOAL_ID:-}" ]]; then
        local _landed_json _stranded_n _benign_n _held_n _held_detail _unclassified
        _landed_json=$(python3 "$SCRIPT_DIR/completed-not-committed-sweep.py" \
            --goal "$GOAL_ID" --min-age-minutes 0 --no-fetch --output json \
            2>>"$CORE_ROOT/logs/iteration-close-stderr.log" || echo '{}')
        # BOTH lists, not just `stranded` (fresh-eyes, same day as the original
        # commit). The producer PARTITIONS stranded_all by reason at
        # completed-not-committed-sweep.py:1176-1177 — `stranded` holds ONLY
        # reason=="stranded_open_pr", and `stranded_no_pr` is a DISJOINT sibling
        # key. Reading `stranded` alone was blind to every branch pushed WITHOUT
        # a pull request, which is both the harder case to notice and the exact
        # shape of the six-day incident that motivated this goal. The goal's own
        # description warned about precisely this blindness — it preferred branch
        # enumeration over PR-listing because "the request-listing form is blind
        # to a branch pushed without one" — and the first cut avoided it at the
        # DETECTION layer, then reintroduced it one layer up at consumption.
        # Fingerprint that gave it away: the printer below carries a
        # 'no open pull request found' branch that was UNREACHABLE, because
        # everything in `stranded` has a PR by construction. A handler for a case
        # the list cannot contain is evidence the author believed it was the
        # union. guard-1802: diff the consumer's predicate against the producer's
        # population and measure what it EXCLUDES — a subset predicate and a
        # genuinely clean queue emit the identical all-clear.
        # THIRD partition, g-115-6060: `benign_squash_merged` is read here and
        # deliberately EXCLUDED from the warning count rather than unioned in.
        # It is landed work — a squash-merge rewrites the sha, so the goal's own
        # commit is off-default permanently even though the deliverable shipped.
        # Unioning it would resurrect, one layer up at consumption, the exact
        # false positive that was just removed at detection — the same
        # detection-fixed / consumption-reintroduced mistake the comment block
        # above records happening the first time. Excluding it is a DECISION,
        # not blindness, which is why the key is referenced in code and not only
        # in this comment: the parity test strips comments precisely so a
        # documented-but-unread key cannot pass for a handled one.
        _benign_n=$(echo "$_landed_json" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('benign_squash_merged') or []))" 2>/dev/null || echo 0)
        # FOURTH partition, g-115-7881. stranded_deploy_held was not referenced
        # here at all, so work parked behind an ACTIVE deploy hold produced an
        # advisory byte-identical to the one a genuinely clean queue produces —
        # the same guard-1802 subset blindness this block already records being
        # introduced, and fixed, twice before.
        #
        # IT IS BENIGN, AND THE PRODUCER SAYS SO TWICE: classify_stranded reads
        # "held is True -> DECISIVE hold. Reclassify benign; never swallow a
        # decisive signal into the flagged verdict", and the producer's own
        # reporter prints "parked behind an ACTIVE deploy hold ... Not stranded
        # — no Investigate filed". So it is NOTED with its holders and kept OUT
        # of _stranded_n, exactly like benign_squash_merged. Summing it in would
        # tell the closer to LAND work whose entire point is that merging it
        # fires the deploy the hold exists to prevent — a false positive that
        # actively instructs the harmful action.
        #
        # (The analysis deposited on this goal argued the opposite — union it in,
        # since the advisory already sums the report-only stranded_no_pr. That
        # conflates two axes: report-only means "do not FILE a goal"; benign
        # means "nothing is wrong". stranded_no_pr is real-but-weak, deploy-held
        # is not stranded at all.)
        _held_n=$(echo "$_landed_json" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('stranded_deploy_held') or []))" 2>/dev/null || echo 0)
        _held_detail=$(echo "$_landed_json" | python3 -c "import json,sys; d=json.load(sys.stdin); print('; '.join('%s PR #%s held by %s' % (e.get('goal_id','?'), (e.get('pull_request') or {}).get('number'), ', '.join(e.get('deploy_holders') or []) or 'unnamed') for e in (d.get('stranded_deploy_held') or [])))" 2>/dev/null || echo '')
        # DERIVED, not a hardcoded subset: cross-check the four keys classified
        # above against the producer's own stranded_all_partitions list, so a
        # FIFTH partition surfaces at runtime instead of silently dropping out.
        # Empty when the producer predates that field — the coupling test stays
        # the primary guarantee; this is defence in depth.
        _unclassified=$(echo "$_landed_json" | python3 -c "import json,sys; d=json.load(sys.stdin); known={'stranded','stranded_no_pr','benign_squash_merged','stranded_deploy_held'}; print(','.join(k for k in (d.get('stranded_all_partitions') or []) if k not in known))" 2>/dev/null || echo '')
        _stranded_n=$(echo "$_landed_json" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('stranded') or []) + len(d.get('stranded_no_pr') or []))" 2>/dev/null || echo 0)
        if [[ "${_benign_n:-0}" != "0" ]]; then
            echo "[iteration-close] NOTE: ${GOAL_ID} has ${_benign_n} commit(s) that landed via squash-merge — off the default branch by sha, on it by content. Not stranded; no action needed (g-115-6060)." >&2
        fi
        if [[ "${_stranded_n:-0}" != "0" ]]; then
            echo "[iteration-close] ADVISORY: ${GOAL_ID} closed but its commits are NOT on the remote default branch — the work is on a branch nobody has landed." >&2
            echo "$_landed_json" | python3 -c "
import json,sys
d=json.load(sys.stdin)
for e in list(d.get('stranded') or []) + list(d.get('stranded_no_pr') or []):
    pr=e.get('pull_request') or {}
    loc=('PR #%s %s' % (pr.get('number'), pr.get('url'))) if pr.get('number') else 'no open pull request found'
    print('[iteration-close]   %s: %s (%s)' % (e.get('goal_id','?'), e.get('reason','?'), loc))
" 2>/dev/null >&2 || true
            echo "[iteration-close]   Land it, or record why it is intentionally unlanded. Advisory only — closure is unaffected (guard-1548, g-115-3838)." >&2
        fi
        if [[ "${_held_n:-0}" != "0" ]]; then
            echo "[iteration-close] NOTE: ${GOAL_ID} has ${_held_n} commit(s) on a pull request parked behind an ACTIVE deploy hold — parked, not stranded. Do NOT land it: merging would fire the deploy the hold exists to prevent (g-115-7881)." >&2
            [[ -n "${_held_detail:-}" ]] && echo "[iteration-close]   ${_held_detail}" >&2
        fi
        if [[ -n "${_unclassified:-}" ]]; then
            echo "[iteration-close] NOTE: completed-not-committed-sweep.py partitions stranded_all into key(s) this advisory does not classify: ${_unclassified}. Classify each as warn or benign in Step 8.79a — an unclassified partition is invisible here (guard-1802, g-115-7881)." >&2
        fi
    fi

    # Step 8.79 Compounding-knowledge metric emission (g-303-35, design Section 6).
    # SHIPS DORMANT: compounding-events.py emit SELF-GATES on
    # compounding_metric.enabled (default OFF in aspirations.yaml) -- when the flag
    # is OFF this no-ops and writes nothing (one fast python invocation in the
    # already-backgrounded deep-close path). When ON it JOINs the retrieval
    # manifest with this close's commit-artifact signal (the _commit_sha above) to
    # record load-bearing retrieval events. Strict HIGH-only (Section 7): with no
    # explicit --cited citation list the wiring records every retrieved entry as a
    # non-load-bearing DENOMINATOR event (the load_bearing_rate denominator);
    # load-bearing numerator credit requires an explicit citation that a future
    # enhancement supplies. python3 (NOT py -3) + $CORE_ROOT (Windows-form via
    # _platform.sh, no cygpath) per this file's L49 invariant and the
    # do_state_update sibling calls (L689/L1009/L1458). Fail-open three ways: the
    # `|| true`, emit()'s never-raises contract, and its disabled/error reason
    # returns -- emission MUST NEVER break the state-update path. Deep-only: the
    # commit artifact it JOINs against exists only on deep closes (logs/ created
    # by the gate block above on this same OUTCOME==deep path).
    if [[ "$OUTCOME" == "deep" ]]; then
        python3 "$CORE_ROOT/scripts/compounding-events.py" emit \
            --goal "$GOAL_ID" \
            --artifact-produced commit \
            --artifact-ref "${_commit_sha:-}" \
            --artifact-write-time "$(date +%Y-%m-%dT%H:%M:%S)" \
            --manifest "$(retrieval_session_path "$AGENT_DIR")" \
            >/dev/null 2>>"$CORE_ROOT/logs/iteration-close-stderr.log" || true
    fi

    # Step 8.8-8.10 Scripted Audit Pass: velocity + backpressure + temporal-credit +
    # relative-advantage. Restores improvement-velocity.yaml updates that were
    # silently omitted when the LLM-executed pseudocode was consolidated into this
    # script (rb-428 bash-consolidation-drift family, g-115-173). Left the tracker
    # blind for 5.9 days before the hole was detected. Runs for both routine and
    # deep — state-update-audit.py handles outcome-class dispatch internally.
    # Fail-open: a script error never blocks state-update.
    # Stdout is silenced (script prints status info callers don't need) but stderr
    # is preserved — previously `>/dev/null 2>&1` destroyed the diagnostic when
    # the call failed, leaving operators with only the WARN line and no reason.
    # The original fix (iter 31, rb-428 family) resolved silent-drift by adding
    # this call; g-240-70 restores the diagnostic so a re-silent failure is
    # recoverable without re-running with strace. bravo fresh-eyes finding
    # msg-bravo-309 (2026-04-24 iter 37) documented the swallow.
    #
    # g-115-228 (rb-428 twin): the previous incarnation of this call passed only
    # --goal/--outcome-class/--category. compute_learning_value (state-update-audit.py:69)
    # requires 4 quality inputs that argparse silently defaulted to 0/false →
    # learning_value=0.0 across 206/206 goals (week 17). Forward the flags when
    # caller observed them; absent flags preserve pre-fix defaults (no regression
    # for legacy callers; opt-in restoration of velocity signal for new callers
    # that follow iteration-close-digest.md § STATE-UPDATE).
    # --tree-updated auto-detect + validation MUST run BEFORE state-update-audit.
    # state-update-audit's compute_learning_value uses --tree-updated as a quality
    # input; running validation only before tree-encoding-drift-gate would let
    # state-update-audit credit a false claim toward learning_value (same drift,
    # different counter). One auto-detect + validate pass; both consumers below
    # read the validated TREE_UPDATED.
    #
    # Auto-detect (g-273-20, alpha session-60): if --tree-updated was NOT
    # passed by the caller, probe iteration-checkpoint.json:selected_at
    # against tree .md mtimes. When the LLM edited tree but forgot to pass
    # --tree-updated (LLM-residue), this auto-promotes TREE_UPDATED=true so
    # the gate short-circuits as it should. Observed iter-17/19/20 fired
    # force_tree_encoding=true three times in a row despite g-273-18 doing
    # legitimate encoding — every state-update call omitted the flag.
    # Fail-open: missing checkpoint, parse errors, OS errors all leave
    # TREE_UPDATED unchanged.
    if [[ "$TREE_UPDATED" != "true" ]] && [[ -f "$AGENT_DIR/session/iteration-checkpoint.json" ]]; then
        SELECTED_AT=$(python3 -c "import json,sys; d=json.load(open(r'$AGENT_DIR/session/iteration-checkpoint.json',encoding='utf-8')); print(d.get('selected_at',''))" 2>/dev/null || true)
        if [[ -n "$SELECTED_AT" ]]; then
            if python3 "$CORE_ROOT/scripts/tree-edit-since.py" "$SELECTED_AT" >/dev/null 2>&1; then
                TREE_UPDATED="true"
                echo "[iteration-close] auto-detected --tree-updated (tree edited since iteration anchor $SELECTED_AT)"
            fi
        fi
    fi
    # Validation (g-115-464): when --tree-updated was passed (explicitly OR by
    # auto-detect) AND --tree-updated-override is NOT set, probe tree-edit-since.py
    # against the iteration anchor. On mismatch (no tree edit detected since the
    # anchor), warn loudly and IGNORE the flag — set TREE_UPDATED=false.
    # tree-encoding-drift-gate is the SINGLE WRITER for goals_since_last_tree_update;
    # state-update-audit consumes --tree-updated as a learning_value quality input.
    # Resetting either on a non-tree edit silently corrupts the signal.
    # Observed bravo iter 12 (g-115-429): --tree-updated --artifacts-count 1 passed
    # for an 8-line _fileops.py edit (no tree change) — counter reset to 0 despite
    # zero tree work. Agent under context pressure may pass --tree-updated
    # reflexively meaning "updated something" rather than "touched the tree" —
    # fail-loud catches this drift. The auto-detect path already passed the same
    # probe so re-running here is a cheap re-confirmation; the meaningful catch is
    # the explicit --tree-updated-without-tree-edit case.
    # Override: --tree-updated-override bypasses validation for rare cases where
    # the caller knows tree was updated but mtime probing won't see it (e.g.,
    # programmatic update path that doesn't touch mtimes).
    if [[ "$TREE_UPDATED" == "true" ]] && [[ "$TREE_UPDATED_OVERRIDE" != "true" ]] && [[ -f "$AGENT_DIR/session/iteration-checkpoint.json" ]]; then
        VAL_SELECTED_AT=$(python3 -c "import json,sys; d=json.load(open(r'$AGENT_DIR/session/iteration-checkpoint.json',encoding='utf-8')); print(d.get('selected_at',''))" 2>/dev/null || true)
        if [[ -n "$VAL_SELECTED_AT" ]]; then
            if ! python3 "$CORE_ROOT/scripts/tree-edit-since.py" "$VAL_SELECTED_AT" >/dev/null 2>&1; then
                echo "[iteration-close] WARN: --tree-updated passed but no tree-file change detected since $VAL_SELECTED_AT — IGNORING flag (use --tree-updated-override to force; tree-encoding-drift-gate counter will increment normally and learning_value will not credit tree-encoding work)" >&2
                TREE_UPDATED=""
            fi
        fi
    fi

    # --findings-count auto-derive (g-115-3844). guard-399: a "the LLM must
    # pass X at step N" instruction needs a bash baseline or it does not fire.
    # guard-1235's own recovery advice is to ABSORB the loss, which is correct
    # given the old wiring and wrong as a permanent state — the miss is silent
    # and recurs by construction. --tree-updated has had a baseline since
    # g-273-20 (the auto-detect ~40 lines above); the other three quality flags
    # are pure pass-through (parsed L638-640, forwarded L266-268) and are
    # exactly the three that go silently missing.
    #
    # Findings are the derivable one. execution-diary.py already records
    # entry_type=finding with a goal_id and an auto-stamped timestamp (it
    # stamps one when the caller omits it, execution-diary.py:332-334 — 52 of
    # 53 bravo findings carry one; the single exception is a legacy line-1
    # record), and its `read` subcommand already filters by --goal and
    # --since. So this counts the WRAPPER's own JSON rather than hand-parsing
    # the JSONL (guard-2298). --limit defaults to None (no truncation);
    # verified against the raw store, 15 entries / 14 findings both ways.
    #
    # THREE DELIBERATE RESTRICTIONS, each load-bearing:
    #  1. Only when the caller passed nothing. An explicit --findings-count is
    #     an observation and always beats a derivation.
    #  2. Only a count > 0 is adopted. Passing `--findings-count 0` would flip
    #     this close out of the deliberate velocity_unmeasured_skip (g-115-2441
    #     — skip rather than record a false 0.0) into a MEASURED close whose
    #     quality inputs are all zero. That is strictly worse than absent.
    #  3. Fail-open. Missing checkpoint, unparseable anchor, diary read error,
    #     or a non-numeric result all leave FINDINGS_COUNT untouched, so the
    #     worst case is exactly today's behaviour.
    #
    # --artifacts-count and --encoding-score are NOT derived here.
    # --artifacts-count needs the commit enumeration that lives in another
    # phase; --encoding-score is a judgment rating with no bash baseline
    # available at all — it is guard-399's "optional enrichment on top of the
    # bash baseline", i.e. the one flag for which a banner IS the right remedy.
    if [[ -z "$FINDINGS_COUNT" ]] && [[ -f "$AGENT_DIR/session/iteration-checkpoint.json" ]]; then
        FC_ANCHOR=$(python3 -c "import json,sys; d=json.load(open(r'$AGENT_DIR/session/iteration-checkpoint.json',encoding='utf-8')); print(d.get('selected_at',''))" 2>/dev/null || true)
        if [[ -n "$FC_ANCHOR" ]]; then
            FC_DERIVED=$(python3 "$SCRIPT_DIR/execution-diary.py" read --goal "$GOAL_ID" --since "$FC_ANCHOR" --json 2>/dev/null \
                | python3 -c "import json,sys; print(sum(1 for e in (json.load(sys.stdin) or []) if e.get('entry_type')=='finding'))" 2>/dev/null || true)
            if [[ "$FC_DERIVED" =~ ^[0-9]+$ ]] && [[ "$FC_DERIVED" -gt 0 ]]; then
                FINDINGS_COUNT="$FC_DERIVED"
                echo "[iteration-close] auto-derived --findings-count $FC_DERIVED (execution-diary entry_type=finding for $GOAL_ID since iteration anchor $FC_ANCHOR)"
            fi
        fi
    fi
    local audit_args=(
        run-all
        --goal "$GOAL_ID"
        --outcome-class "$OUTCOME"
        --category "$category"
    )
    [[ "$TREE_UPDATED" == "true" ]] && audit_args+=(--tree-updated)
    [[ -n "$ARTIFACTS_COUNT" ]] && audit_args+=(--artifacts-count "$ARTIFACTS_COUNT")
    [[ -n "$ENCODING_SCORE" ]]  && audit_args+=(--encoding-score "$ENCODING_SCORE")
    [[ -n "$FINDINGS_COUNT" ]]  && audit_args+=(--findings-count "$FINDINGS_COUNT")
    # Exit-code contract (state-update-audit.py:484, header line 23):
    # 0=clean, 1=FLAGS RAISED (audit ran fully; snapshot recorded; advisory
    # signal on stdout), 2=input error. The previous form treated ANY nonzero
    # as failure and swallowed stdout — every flagged close (rollbacks_applied,
    # dead_ends_registered, …) lost its advisory signal and misreported
    # "snapshot not recorded" (g-115-1945: 2+ consecutive false WARNs on
    # zeta/cc-02 closes; standalone repro passed because state had 0 flags).
    local audit_out audit_rc audit_flags
    # set -e-safe rc capture (g-115-1945 follow-up): `var=$(cmd) ; rc=$?` dies
    # at the assignment under `set -euo pipefail` (L53) before rc=$? runs —
    # every FLAG-RAISING close (audit rc=1 by design) killed state-update here,
    # skipping the metric/drift gates below. `|| audit_rc=$?` captures without
    # tripping errexit.
    audit_rc=0
    audit_out="$(bash "$SCRIPT_DIR/state-update-audit.sh" "${audit_args[@]}")" || audit_rc=$?
    if [[ $audit_rc -eq 1 ]]; then
        audit_flags="$(printf '%s' "$audit_out" | python3 -c 'import json,sys; print(",".join(json.load(sys.stdin).get("flags",[])))' 2>/dev/null || echo "unparsable")"
        echo "[iteration-close] state-update-audit flags (advisory, audit ran + snapshot recorded): ${audit_flags}" >&2
    elif [[ $audit_rc -ne 0 ]]; then
        echo "[iteration-close] WARN: state-update-audit.sh failed rc=${audit_rc} (non-fatal — velocity/backpressure snapshot not recorded for $GOAL_ID)" >&2
    fi
    # g-115-2441: unmeasured-skip is rc=0 (the audit completed — it
    # deliberately skipped the snapshot), so it never reaches the rc=1
    # advisory above. Surface it explicitly or unflagged closers never learn
    # why their deep closes stopped appearing in the velocity series.
    if [[ "$audit_out" == *velocity_unmeasured_skipped* ]]; then
        echo "[iteration-close] LLM-ACTION: deep close UNMEASURED — no § STATE-UPDATE quality flags passed; imp@k snapshot SKIPPED instead of recording a false 0.0 (g-115-2441)." >&2
        # g-115-3480: "pass the flags" is not an available action by the time this
        # renders — the phase has already journalled, committed and cleared
        # in_flight, and re-running --phase state-update to supply them would
        # append a SECOND journal entry and a second iteration commit. Name the
        # recovery that IS available, or the reader is told what they should have
        # done at the one moment they can no longer do it and records nothing.
        echo "  RECOVER NOW — do NOT re-run --phase state-update (it would double-journal + double-commit). Run the velocity subcommand alone:" >&2
        echo "    bash core/scripts/state-update-audit.sh velocity --goal $GOAL_ID --category $category --tree-updated --artifacts-count <n> --encoding-score <0.0-1.0> --findings-count <n>" >&2
        echo "  Safe post-hoc BECAUSE the unmeasured path wrote nothing: state-update-audit.py cmd_velocity early-returns on not-measured, before meta-impk is ever invoked. So this call records the goal's FIRST and only snapshot." >&2
        echo "  Do NOT run it after a MEASURED close. g-115-4542 (2026-08-09) added a --close-key (goal_id:selected_at, derived inside state-update-audit.py) so a repeat snapshot IS suppressed rather than double-weighted -- but ONLY while the iteration checkpoint is live. do_productivity_check() deletes that checkpoint, after which _close_key fails open to empty, the flag is omitted, and a second entry double-weights this goal across the 5/10/20 rolling averages. Run it ONLY when you saw this advisory." >&2
    fi

    # Step 8.79 Post-State-Update METRIC Gate (g-115-724, rb-917 content-gate
    # sibling). Counter-gate sibling to post-state-update-gate.sh (above) and
    # tree-encoding-drift-gate.sh (below) catch "LLM skipped the encoding step
    # entirely" by counting occurrences. This content-gate catches "LLM did
    # the encoding step on the wrong content" by regex-scanning the
    # outcome_note + verify summary for numeric/ratio findings.
    #
    # Fires on deep outcomes when 2+ distinct numeric findings appear in the
    # SCANNED INPUT and tree-edit-since.py reports no tree edit since
    # iteration-checkpoint.json:selected_at. Writes force_metric_encoding_pending
    # WM sentinel; aspirations-precheck Phase 0-pre4 consumes it.
    #
    # WHICH input is scanned has a precedence, set at the _metric_input
    # assignment below: $SUMMARY when a caller passed --summary, else the goal
    # record outcome_note. On the loop path $SUMMARY is always empty, so the
    # record outcome_note is what is scanned on every normal closure. Name the
    # scanned input by that precedence, not by one branch of it (g-115-5104).
    #
    # Canonical incident (g-115-707): alpha closed g-250-78 with measurable
    # production metrics (jose 1.8x 690->1245, RichmondKey 2x, BT failures 0
    # vs 69 baseline) in outcome_note prose — verification:null, no bash gate
    # inspected the content, encoding lagged ~50 min until manual catch.
    #
    # Fail-open: gate errors never block state-update.
    if [[ "$OUTCOME" == "deep" ]]; then
        local metric_gate_json
        mkdir -p "$CORE_ROOT/logs"
        # SUMMARY may be empty when caller didn't pass --summary. Gate handles
        # empty outcome_note as no-op (below-threshold). Pass via stdin to
        # avoid argv quoting issues on multi-line prose.
        #
        # g-115-5157: $SUMMARY IS EMPTY HERE ON EVERY NORMAL CLOSURE, and until
        # 2026-08-08 that was the whole input. state-update is a SEPARATE
        # invocation from verify, and measured across .claude/skills + core/scripts,
        # ZERO state-update call sites pass --summary (verify has one). So this
        # gate — whose entire purpose is scanning verify narratives for numeric
        # findings — was handed "" on the loop path and no-oped every time,
        # reporting "empty outcome_note (gate has no content to scan)". Its own
        # reason string names its input outcome_note; the wiring passed a
        # variable this phase can never hold.
        #
        # Verified by execution, not inference (both arms, one production-domain
        # category, 2026-08-08): "" -> {"fired": false, "distinct_count": 0,
        # "reason": "empty outcome_note..."}; real numeric prose -> {"fired":
        # true, "distinct_count": 3} with candidates and a candidate node. The
        # gate works; it was never fed. NOTE a meta-work category (e.g.
        # framework-architecture) short-circuits BEFORE the content scan, so a
        # probe run under one returns identical output for both arms and has no
        # discriminating power — use a production-domain category to re-measure.
        #
        # Fall back to the record's outcome_note, which survives across phase
        # invocations and which do_verify now populates. $SUMMARY still wins when
        # present, so a caller that does pass it is unaffected.
        local _metric_input="${SUMMARY:-}"
        local _metric_src="the --summary verify narrative"
        if [[ -z "$_metric_input" ]]; then
            _metric_input="$(_probe_goal_outcome_note)"
            _metric_src="the goal record outcome_note"
        fi
        metric_gate_json=$(printf '%s' "$_metric_input" | \
            bash "$SCRIPT_DIR/post-state-update-metric-gate.sh" \
                "$OUTCOME" "$GOAL_ID" "$category" "-" \
                2>>"$CORE_ROOT/logs/iteration-close-stderr.log" \
                || echo '{"fired":false,"reason":"gate-error"}')
        local metric_fired
        metric_fired=$(echo "$metric_gate_json" | python3 -c "import json,sys; d=json.load(sys.stdin); print('true' if d.get('fired') else 'false')" 2>/dev/null || echo false)
        if [[ "$metric_fired" == "true" ]]; then
            # Write WM signal the precheck Phase 0-pre4 consumes. Storing the
            # full gate JSON preserves candidates + candidate_node_key/file.
            echo "$metric_gate_json" | bash "$SCRIPT_DIR/wm-set.sh" force_metric_encoding_pending >/dev/null 2>&1 || true
            local distinct_count metric_reason
            distinct_count=$(echo "$metric_gate_json" | python3 -c "import json,sys; print(json.load(sys.stdin).get('distinct_count', '?'))" 2>/dev/null || echo "?")
            metric_reason=$(echo "$metric_gate_json" | python3 -c "import json,sys; print(json.load(sys.stdin).get('reason', ''))" 2>/dev/null || echo "")
            echo "[iteration-close] METRIC-ENCODING: $distinct_count distinct numeric findings detected in $_metric_src ($metric_reason). force_metric_encoding_pending set — precheck Phase 0-pre4 will dispatch next iteration." >&2
        fi
    fi

    # Step 8.79a Shipped-claim STORE-CONTENT check (g-115-7299; g-326-585
    # incident). Third member of the artifact family, and the only one that
    # reads CONTENT. Its two siblings both pass cleanly on the incident:
    #   - goal-completion-artifact-gate (at cmd_update_goal status=completed)
    #     scans title + description, tests Path.exists() on the LOCAL disk, and
    #     its ARTIFACT_PATH_RE roots exclude world/scripts;
    #   - the metric gate above scans the same note but only for NUMBERS.
    # g-326-585 closed `completed` claiming a `--direct` mode and a
    # probe_direct() had been added to zakpod1-pp-aging-probe.py. The STORE's
    # world/scripts/zakpod1-pp-aging-probe.py (24,976 B, byte-identical to the
    # local mirror) contains neither, and its own docstring argues the opposite
    # design ("Direct-port is unreachable off-pod, full stop"). A downstream
    # acceptance goal (g-326-586) was filed against a mode that does not exist.
    #
    # WHY HERE AND NOT AT CLOSE TIME. The note is frequently absent when the
    # status flips — measured 2026-08-22: 5 of 21 completed goals sampled
    # carried a 0-byte outcome_note, and g-326-469's own note records
    # "Completed by alpha 2026-08-19T23:14:05 with an EMPTY outcome_note",
    # filled in by a second agent the next day. A close-time note gate is
    # inert on that population. do_state_update is the later phase that
    # already re-reads the record's CURRENT note (g-115-5157), so the note is
    # most likely populated here.
    #
    # NOT gated on outcome_class or category (unlike the metric gate): a
    # shipped claim is just as wrong in a routine close, and the categories
    # the metric gate excludes are exactly where framework artifacts land.
    #
    # REPORT-ONLY. The goal is already closed, so blocking is unavailable;
    # and paraphrase in closing prose makes a hard refusal the wrong
    # instrument. The finding goes to stderr (read by the closing agent in
    # this turn's tool output — the live consumer) and to
    # world/shipped-claim-mismatches.jsonl (the durable trail).
    # Fail-open: gate errors never block state-update.
    local _sc_note
    _sc_note="${SUMMARY:-}"
    if [[ -z "$_sc_note" ]]; then
        _sc_note="$(_probe_goal_outcome_note)"
    fi
    if [[ -n "$_sc_note" ]]; then
        local sc_json sc_fired
        sc_json=$(printf '%s' "$_sc_note" | \
            bash "$SCRIPT_DIR/shipped-claim-store-check.sh" --goal "$GOAL_ID" \
                2>>"$CORE_ROOT/logs/iteration-close-stderr.log" \
                || true)
        [[ -z "$sc_json" ]] && sc_json='{"fired":false,"reason":"gate-error"}'
        sc_fired=$(echo "$sc_json" | python3 -c "import json,sys; print('true' if json.load(sys.stdin).get('fired') else 'false')" 2>/dev/null || echo false)
        if [[ "$sc_fired" == "true" ]]; then
            local sc_detail
            sc_detail=$(echo "$sc_json" | python3 -c "
import json,sys
d=json.load(sys.stdin)
r=d.get('resolved') or {}
print('; '.join(
    '%s (%s, %d B): %s' % (m.get('artifact'), r.get(m.get('artifact')) or 'unresolved',
                           m.get('content_bytes', 0), ', '.join(m.get('missing') or []))
    for m in (d.get('mismatches') or [])))
" 2>/dev/null || echo "")
            echo "[iteration-close] SHIPPED-CLAIM MISMATCH: $GOAL_ID's outcome_note claims symbol(s) that occur ZERO times in the STORE copy — $sc_detail. The artifact exists; its CONTENT does not carry the claim. Re-read the store copy before trusting this closure or any goal filed downstream of it (g-326-585 class). Logged to world/shipped-claim-mismatches.jsonl." >&2
        fi
    fi

    # Step 8.79b Domain post-close pipeline-freshness hook (rb-428
    # sentinel family). Pattern B domain-overlay seam (domain-overlay-pattern.md,
    # mirrors aspirations-precheck's signal-refresh hook): CORE stays domain-
    # agnostic — it only provides the seam + the sentinel write. The DOMAIN
    # supplies the classifier at $WORLD_DIR/scripts/pipeline-reconcile-gate.sh.
    # If that script is absent (fresh world, or a domain with no external
    # pipeline), this is a one-test no-op. The gate decides "pipeline-affecting"
    # and names the consumer skill in its JSON; aspirations-precheck consumes the
    # pipeline_reconcile_pending sentinel on the NEXT iteration (deferred-consume
    # shape survives autocompact via WM, keeps this close path cheap). Reversibility
    # + the domain vocabulary both live in the gate (PIPELINE_HOOK_ENABLED=0
    # disables sentinel writes). Fires for BOTH outcomes — a routine pipeline goal
    # can still imply an un-recorded row; the reconcile is idempotent. Fail-open:
    # any gate error is swallowed and never blocks state-update.
    if [[ -n "${WORLD_DIR:-}" && -f "$WORLD_DIR/scripts/pipeline-reconcile-gate.sh" ]]; then
        local pipe_gate_json pipe_fired
        pipe_gate_json=$(WORLD_DIR="$WORLD_DIR" AGENT_DIR="$AGENT_DIR" \
            bash "$WORLD_DIR/scripts/pipeline-reconcile-gate.sh" "$GOAL_ID" "$SOURCE" "$category" \
            2>>"$CORE_ROOT/logs/iteration-close-stderr.log" || echo '{"fired":false,"reason":"gate-error"}')
        pipe_fired=$(echo "$pipe_gate_json" | python3 -c "import json,sys; print('true' if json.load(sys.stdin).get('fired') else 'false')" 2>/dev/null || echo false)
        if [[ "$pipe_fired" == "true" ]]; then
            echo "$pipe_gate_json" | bash "$SCRIPT_DIR/wm-set.sh" pipeline_reconcile_pending >/dev/null 2>&1 || true
            local pipe_reason
            pipe_reason=$(echo "$pipe_gate_json" | python3 -c "import json,sys; print(json.load(sys.stdin).get('reason',''))" 2>/dev/null || echo "")
            echo "[iteration-close] PIPELINE-RECONCILE: $GOAL_ID pipeline-affecting ($pipe_reason) — pipeline_reconcile_pending set; precheck Phase 0-pre5 reconciles next iteration." >&2
        fi
    fi

    # Phase 8.0.5/8.0.6 bash-enforcement (g-248-75, rb-428 family).
    # Tree-encoding drift sentinel was LLM-residue: 'IF goals_since_last_tree_update
    # >= 3: wm-set force_tree_encoding=true; reset.' Observed iter-101 alpha
    # session 58 (2026-04-25): counter=8 but sentinel=False — silently dropped
    # across iterations. Same pattern as rb-428 (LLM-step-lost when bash safety
    # net fires unconditionally). Fix: bash is now single writer for the counter.
    # On threshold cross, sets force_tree_encoding=true sentinel (consumed by
    # aspirations-state-update Step 8) AND resets counter to 0. Threshold knob:
    # core/config/aspirations.yaml:tree_encoding_drift_threshold (default 3).
    # Fail-open: read/write errors never block state-update.
    #
    # --tree-updated pass-through (g-115-282, iter-141 felt-sense Lane 4):
    # When the caller signals --tree-updated, the gate short-circuits to
    # counter=0 without setting the sentinel. The original heuristic 'IF
    # tree_encoded: reset ELSE +=1' was abandoned at g-248-75 because no
    # concrete tree-encoded signal existed; the --tree-updated flag now
    # provides exactly that signal. Without this pass-through, the counter
    # increments every state-update regardless of whether the iteration
    # actually did tree encoding work, producing the over-fire pattern where
    # /tree maintain dispatches even when tree was just updated.
    #
    # Auto-detect + validation already ran above (before state-update-audit) so
    # TREE_UPDATED here reflects the true tree-edit state — see g-273-20 (auto-
    # detect) and g-115-464 (validation) for context.
    GATE_ARGS=()
    [[ "$TREE_UPDATED" == "true" ]] && GATE_ARGS+=(--tree-updated)
    bash "$SCRIPT_DIR/tree-encoding-drift-gate.sh" "${GATE_ARGS[@]}" \
        || echo "[iteration-close] WARN: tree-encoding-drift-gate failed (non-fatal; sentinel not updated this iteration)" >&2

    # ─── force_tree_encoding bypass-consumer (non-recurring hot path) ───
    # The drift gate (above) sets force_tree_encoding="true" on threshold cross.
    # Its INTENDED consumer is aspirations-state-update SKILL.md Step 8 (LLM
    # path) — which the hot path (this script: /aspirations loop, hand-rolled
    # closes) BYPASSES. recurring-close.sh added a drain for the RECURRING path
    # (its own block, after run_phase state-update); THIS is the symmetric drain
    # for the NON-recurring hot path. Without it the sentinel goes stuck on
    # non-recurring closes — stale-sentinel-canary then fires a (failing)
    # Investigate. Same log+clear semantics as recurring-close.sh:
    # force_tree_maintain (the paired sentinel, consumed by precheck Phase 0-pre)
    # backstops global tree maintenance; the per-goal encoding override is only
    # honored on the SKILL.md path. Draining at this shared choke point covers
    # BOTH close paths (recurring-close's own drain then finds it already null
    # and no-ops — a benign redundant backup). Unconditional on outcome: the
    # drift gate counts every goal, so the sentinel can be set on routine
    # closes too.
    FTE_VAL="$(bash "$SCRIPT_DIR/wm-read.sh" force_tree_encoding 2>/dev/null || echo null)"
    if [[ "$FTE_VAL" == '"true"' || "$FTE_VAL" == 'true' ]]; then
        FTE_NOW="$(date +%Y-%m-%dT%H:%M:%S)"
        printf '{"date":"%s","entry_type":"observation","goal_id":"%s","content":"iteration-close force_tree_encoding bypass-consume (non-recurring hot path): sentinel set by tree-encoding-drift-gate during state-update; SKILL.md Step 8 consumer not on this path. force_tree_maintain backstops global tree maintenance; sentinel cleared so it does not go stuck."}' \
            "$FTE_NOW" "$GOAL_ID" \
            | bash "$SCRIPT_DIR/journal-add.sh" >/dev/null 2>&1 \
            || echo "[iteration-close] WARN: force_tree_encoding bypass-journal-append failed (non-fatal)" >&2
        echo '"false"' | bash "$SCRIPT_DIR/wm-set.sh" force_tree_encoding >/dev/null 2>&1 \
            || echo "[iteration-close] WARN: force_tree_encoding clear failed (non-fatal — next iteration re-clears)" >&2
        echo "[iteration-close] force_tree_encoding bypass-consumed for $GOAL_ID (cleared; per-goal encoding override n/a on hot path)" >&2
    fi
    # ─── end force_tree_encoding bypass-consumer ───

    # LLM residue at this phase (deep outcomes only):
    # Step 8a-c precision extraction + Key Insights compression,
    # Step c.5 curator gate scoring, Step 8e decision rules, Step 8f consistency scan,
    # Step 8.11 execution feedback rating.
    # Step 8.78 dispatch: if WM.fresh_eyes_dispatch_pending is set, invoke /fresh-eyes-code.
    # See core/config/iteration-close-digest.md § STATE-UPDATE.

    # ----------------------------------------------------------------------
    # Hot-path outcome-observation hook (g-115-747, parallel to Step 8.12 cold-path).
    # Fires on deep outcomes only — mirrors the SKIP rule in aspirations-state-update
    # Step 8.12. Convention-file gate makes this fail-open on fresh agents (no
    # convention = no fire).
    #
    # Per g-115-742 investigation: outcome-metrics.yaml went 20+ days stale because
    # the cold path (Step 8.12 inside aspirations-state-update SKILL.md) only fires
    # when the LLM invokes that sub-skill directly. EVERY iteration-close.sh
    # do_state_update call BYPASSES Step 8.12 — including the canonical hot-path
    # flow exercised by /aspirations loop and short-circuit paths (hand-rolled
    # Phase 4, recurring-close.sh, etc.). This block ensures the hook fires on
    # every deep close regardless of the state-update invocation path.
    #
    # See core/config/iteration-close-digest.md § STATE-UPDATE residue 8 for the
    # parallel digest entry. Bash-only — no LLM residue required.
    # ROUTED THROUGH THE AUDITED WRAPPER (g-115-4879). This call used to invoke
    # the collector DIRECTLY, which made it a SHADOW CALL PATH: the collector
    # ran (so outcome-metrics.yaml stayed fresh and the slot looked perfectly
    # healthy) while core/scripts/outcome-observation-run.sh — the wrapper whose
    # entire job is to append one audit entry PER INVOCATION — never executed.
    # The convention promises that telemetry explicitly ("replaces the original
    # silent-swallow with auditable telemetry"), and it was structurally never
    # produced: core/logs/outcome-observation-runs.jsonl was ABSENT on cc-05
    # (measured g-115-4557) and independently on cc-07, on both boxes while
    # core/logs/ was live and writable.
    #
    # That is the dangerous shape: a healthy OUTPUT concealing a dead hook, with
    # the audit layer that would expose the divergence being exactly what the
    # shadow path skipped. The wrapper's only other mechanized caller is
    # state-update Step 4.5, whose gate is permanently false on any box that is
    # not the collecting one (the second defect this goal names), so there was
    # no path left that could produce an entry.
    #
    # Drop-in: the wrapper runs the SAME collector with the same no-arg
    # invocation (outcome-observation-run.sh:45), adds the audit entry, and
    # exits 0 by contract. `2>/dev/null` and the `|| echo WARN` are deliberately
    # GONE rather than kept — the wrapper now emits that warning itself (so the
    # signal is preserved for every caller, not just this one), and under its
    # exit-0 contract a `||` branch here would be dead code that reads as live.
    if [[ "$OUTCOME" == "deep" ]] && [[ -f "$WORLD_DIR/conventions/outcome-observation.md" ]]; then
        bash core/scripts/outcome-observation-run.sh "$GOAL_ID" "$OUTCOME"
    fi

    # ----------------------------------------------------------------------
    # g-115-1043 outcome 3: post-state-update aspirations.jsonl parse-canary.
    # Defense-in-depth backstop for the _fileops.py outcome 1+2 layers — runs
    # AFTER every state-update on both world and agent queues. Independent of
    # outcome_class (corruption is corruption). On parse failure: restores
    # from .history, files an Investigate goal, alerts via stderr.
    #
    # Fail-open at every layer — a canary bug must never block state-update.
    # Catches corruption that slipped past the _atomic_write_with_fallback
    # validation (e.g., external corruption, manual edits, hooks).
    local _canary_targets=()
    [[ -f "$WORLD_DIR/aspirations.jsonl" ]] && _canary_targets+=("$WORLD_DIR/aspirations.jsonl")
    [[ -f "$AGENT_DIR/aspirations.jsonl" ]] && _canary_targets+=("$AGENT_DIR/aspirations.jsonl")
    for _canary_target in "${_canary_targets[@]}"; do
        CANARY_TARGET="$_canary_target" CORE_SCRIPTS="$CORE_ROOT/scripts" \
            python3 -c '
import os, sys, json
target = os.environ["CANARY_TARGET"]
sys.path.insert(0, os.environ["CORE_SCRIPTS"])
try:
    from _fileops import _parse_jsonl_skip_corrupt, _find_latest_history_snapshot
    import shutil
    items, _errs, total = _parse_jsonl_skip_corrupt(target)
    if total > 0 and len(items) == 0:
        snap = _find_latest_history_snapshot(target)
        if snap:
            shutil.copy(target, target + ".canary-corrupt")
            shutil.copy(str(snap), target)
            print(f"[iteration-close] CANARY RESTORED: {target} ({total} lines, 0 parseable) -> restored from {snap.name}; corrupt saved as .canary-corrupt", file=sys.stderr)
            sys.exit(2)
        else:
            print(f"[iteration-close] CANARY CRITICAL: {target} corrupt AND no .history snapshot", file=sys.stderr)
            sys.exit(2)
except SystemExit:
    raise
except Exception as e:
    print(f"[iteration-close] canary-parse error for {target}: {e!r} (non-fatal)", file=sys.stderr)
' 2>>"$CORE_ROOT/logs/iteration-close-stderr.log"
        local _canary_rc=$?
        if [[ $_canary_rc -eq 2 ]]; then
            # Canary fired — file an Investigate goal so the corruption is on the queue.
            # aspirations-add-goal.sh takes the goal body via STDIN as JSON, NOT
            # as CLI flags (--title/--priority/etc. are explicitly rejected).
            # Fail-open: if goal filing itself fails, log and continue (never block state-update).
            local _canary_basename="$(basename "$_canary_target")"
            local _canary_payload
            _canary_payload="$(CANARY_BASENAME="$_canary_basename" CANARY_TARGET="$_canary_target" CANARY_NOW="$NOW_ISO" CANARY_GOAL_ID="$GOAL_ID" \
                python3 -c '
import json, os
# Env reads hoisted to locals so no subscript appears inside an f-string
# EXPRESSION. Two hazards removed at once (guard-504 remedy a): a backslash is
# not permitted in an f-string expression, and inside a single-quoted bash
# string the double quotes need no escaping in the first place. The escaped
# form made this whole block a SyntaxError, so the corruption alarm filed
# nothing for as long as it existed (g-115-3565).
name = os.environ["CANARY_BASENAME"]
now = os.environ["CANARY_NOW"]
target = os.environ["CANARY_TARGET"]
goal = os.environ["CANARY_GOAL_ID"]
print(json.dumps({
    "title": f"Investigate: aspirations.jsonl canary fired on {name} ({now})",
    "priority": "HIGH",
    "participants": ["agent"],
    "category": "framework-architecture",
    # `investigate:` because canary-fired is NOT in the origin-signal gate
    # ALLOWED_PREFIXES. TWO independent reasons, found separately and converging
    # on the same fix (g-115-3565 alpha / g-115-3575 bravo, merged 2026-07-28):
    #   1. MEASURED: the COLON form is hard-refused. Piping a payload carrying
    #      "canary-fired:<basename>:<ts>" through the real consumer returns
    #      {"error": "origin_signal_blocked"} -- so the alarm could not file at
    #      all, even after its SyntaxError was repaired.
    #   2. Even where a payload is admitted, an unregistered prefix gets
    #      Layer-D auto-derived from the TITLE, which truncates the timestamp to
    #      the month -- two firings on one file in the same month collapse to one
    #      key and the duplication gate can swallow the second alarm.
    # A registered prefix fixes both: the payload is admitted AND keeps its
    # full-precision key, so every firing stays a distinct goal (never deduped --
    # deliberate; each corruption event needs its own record).
    "origin_signal": f"investigate:canary-fired-{name}-{now}",
    "description": (
        f"iteration-close.sh state-update canary detected corruption in "
        f"{target} after state-update for goal "
        f"{goal}. File was restored from latest "
        f".history snapshot in place; corrupted version preserved as "
        f".canary-corrupt sidecar. Investigate root cause — _fileops.py "
        f"outcomes 1+2 should have caught this earlier; canary firing "
        f"means corruption slipped through some path that bypasses the "
        f"writer-layer validation."
    ),
}))
')"
            # Surface the consumer's REASON, not just "failed". The prior form
            # sent stderr to /dev/null, so the origin-signal refusal above was
            # invisible for the whole life of this branch: the alarm reported a
            # generic non-fatal WARN and nobody could tell a gate refusal from a
            # dead daemon. Fail-open is still fail-open -- it just says why now.
            local _canary_err _canary_et
            # g-115-4166: resolve per deployment. Resolved inside this branch,
            # which only runs when a canary actually fires, so the (hot) clean
            # iteration-close path never pays for the subprocess.
            _canary_et="$(bash "$SCRIPT_DIR/escalation-target.sh")" || _canary_et="asp-115 world"
            _canary_err="$(printf '%s' "$_canary_payload" \
                | bash "$SCRIPT_DIR/aspirations-add-goal.sh" \
                      --source "${_canary_et##* }" --aspiration "${_canary_et%% *}" 2>&1 >/dev/null)" \
                || echo "[iteration-close] WARN: canary-Investigate goal-file failed for $_canary_target (non-fatal): ${_canary_err}" >&2
        fi
    done
}

# --------------------------- phase: learning-gate ---------------------------
do_learning_gate() {
    _CURRENT_PHASE="learning-gate"
    [[ -z "$GOAL_ID" || -z "$SOURCE" || -z "$OUTCOME" ]] && {
        echo "learning-gate: --goal, --source, --outcome required" >&2
        echo "  usage: iteration-close.sh --phase learning-gate --goal <id> --source <world|agent> --outcome <deep|routine>" >&2
        exit 2;
    }
    echo "[iteration-close] learning-gate: goal=$GOAL_ID outcome=$OUTCOME"

    # g-115-5001: same forward read as state-update. BOTH phases are wired, not
    # just the first: g-115-4718 showed learning-gate reporting success over an
    # open record independently, and a closer may run learning-gate alone.
    # g-115-5573: now a refusal, same as state-update — see _assert_verify_landed.
    _assert_verify_landed "learning-gate"

    # Retrieval-performed tracking (g-001-132). retrieve.py --goal auto-writes
    # retrieval-session.json when intelligent retrieval fires (Phase 4). A bare
    # `retrieve.sh --category` consult (the code-review-protocol step-4 pre-apply
    # consult passes no --goal) ALSO writes this file via retrieve.py's g-115-137
    # in-flight-goal inference WHENEVER agent_status.<agent>.in_flight is set — so
    # the step-4 consult is NOT invisible to the pre-apply-consult drift gate
    # below; it sets perf=true too. The narrow residual where a consulted
    # framework-deep close still reads perf=false needs BOTH no Phase-4 --goal
    # retrieval AND in_flight unset at consult time, and is fail-safe (worst case:
    # one forced, harmless consult next precheck). g-115-2662 verified this
    # empirically (in_flight=null -> inference returns None -> no write). Recurring
    # / routine goals skip that path entirely, so the PRIOR goal's file persists
    # with utilization_pending=false and both gates silently short-circuit —
    # leaving "did this goal retrieve?" unobservable and allowing orphan
    # accumulation to hide. Fix: at this point the file either (a) was written
    # for THIS goal and fires the existing feedback flow, or (b) is missing or
    # stale (refers to a prior goal) and gets overwritten with a no-retrieval
    # stub. Either way a per-iteration retrieval-summary line is emitted — grep
    # over N iterations gives retrieval_using_ratio.
    # Body-aware (g-115-6653). This site is the one that did ACTIVE harm on a
    # worker: finding the agent-wide file absent/stale, it overwrote it with a
    # no-retrieval STUB while the real manifest sat unread in sessions/<sid>/ —
    # so the learning gate recorded retrieval_performed=false for every
    # worker-executed goal, and the ratio it feeds was measuring the wrong file.
    local ret_file; ret_file="$(retrieval_session_path "$AGENT_DIR")"
    local current_file_goal=""
    local current_file_stub="stub"
    local perf="false"   # g-115-2201: retrieval-performed flag for the pre-apply-consult drift gate below
    if [[ -f "$ret_file" ]]; then
        # g-115-2454 stub-detect probe: emits "goal_id<TAB>stub|real". Only the
        # no-retrieval stub written below carries retrieval_performed:false (the
        # daemon-written real manifest omits the field), so a SECOND
        # learning-gate run for the same goal (operator retry, recovery re-run)
        # must NOT read its own prior stub as performed — goal_id match alone
        # reported a false performed=true and wrongly reset the
        # pre-apply-consult miss streak (lenient-direction error).
        local probe_out
        probe_out="$(python3 -c "
import json
try:
    d = json.loads(open(r'$ret_file', encoding='utf-8').read())
    kind = 'stub' if d.get('retrieval_performed') is False else 'real'
    print((d.get('goal_id') or '') + '\t' + kind)
except Exception:
    print('')
" 2>/dev/null || echo "")"
        current_file_goal="${probe_out%%$'\t'*}"
        current_file_stub="${probe_out##*$'\t'}"
    fi

    if [[ "$current_file_goal" != "$GOAL_ID" || "$current_file_stub" == "stub" ]]; then
        # g-115-5887 acceptance 6: NAME the foreign goal_id BEFORE the overwrite
        # below destroys it. current_file_goal was read (L2624), tested (L2628)
        # and then silently discarded — 3 occurrences in this whole file, none
        # of them a print — so a MIS-STAMPED manifest was indistinguishable from
        # a genuine no-consult. The close reported only performed=false, which
        # reads as an accusation against the agent, while the one value that
        # identifies the real cause was thrown away. Measured 2026-08-13
        # (alpha/cc-04): g-115-5216 carried 2 goal-tied SUBJECT+MECHANISM consult
        # rows in world/retrieval-trace.jsonl and still closed performed=false,
        # climbing the drift streak to 2 and forcing a redundant consult.
        # Emitted BEFORE the write so a crash mid-write still leaves the
        # discriminator in the log. Diagnostic only — no behavior change.
        if [[ -n "$current_file_goal" && "$current_file_goal" != "$GOAL_ID" ]]; then
            echo "[iteration-close] retrieval-manifest MISMATCH: on-disk manifest belongs to goal '$current_file_goal', this close is '$GOAL_ID' (kind=$current_file_stub) — overwriting with a no-retrieval stub. A mismatch is NOT evidence the agent skipped its consult; cross-check world/retrieval-trace.jsonl for goal-tied rows before reading performed=false as a miss (g-115-5887)." >&2
        fi
        # Missing or stale file — write no-retrieval stub so this iteration is
        # detectable in retrospective analysis. utilization_pending=false means
        # neither gate runs feedback (correct — nothing was retrieved to score).
        # Pass GOAL_ID + ret_file via env vars (matching the safe pattern at
        # L221-228 above) so no shell interpolation happens inside the Python
        # heredoc — fresh-eyes finding bravo-fec-iter-close-injection F-002,
        # fixed in g-115-383.
        GID="$GOAL_ID" RET_FILE="$ret_file" python3 -c '
import json, os, pathlib
p = pathlib.Path(os.environ["RET_FILE"])
p.parent.mkdir(parents=True, exist_ok=True)
stub = {
    "schema_version": 2,
    "goal_id": os.environ["GID"],
    "retrieval_performed": False,
    "tree_nodes_loaded": [],
    "supplementary_items": [],
    "tree_nodes_detail": [],
    "supplementary_detail": [],
    "counts": {"tree_nodes": 0, "reasoning_bank": 0, "meta_lessons": 0,
               "guardrails": 0, "pattern_signatures": 0, "experiences": 0},
    "utilization_pending": False,
    "utilization_completed_at": None,
}
tmp = str(p) + ".tmp"
open(tmp,"w",encoding="utf-8").write(json.dumps(stub, indent=2))
os.replace(tmp, str(p))
' 2>/dev/null || echo "[iteration-close] WARN: retrieval stub write failed for $GOAL_ID" >&2
        echo "[iteration-close] retrieval-summary: performed=false goal=$GOAL_ID (no-retrieval stub)"
        # g-115-3282: hold a goal to a retrieval step its OWN description
        # mandates (origin: g-335-279 wrote such a step into g-335-09 with no
        # enforcement behind it — guard-399's "instruction without a gate
        # drifts"). Fires ONLY on this no-retrieval branch, so the ~1.2s goal
        # read is never paid by a close that did retrieve. The checker fires
        # only when the description names THIS goal via --goal; the obvious
        # substring predicate was measured at 90.5% false positives over 6,155
        # goals and rejected (guard-1430) — see the module docstring.
        # ADVISORY: the checker always exits 0, and `|| true` keeps a failed
        # query or a dead daemon from failing a close (guard-1562).
        # --query-json is REQUIRED here, not optional (fresh-eyes F-1, g-115-3282).
        # Without it the checker treats this pipe's JSON ARRAY as raw description
        # text and matches the literal against the WHOLE BLOB — title, outcome_note
        # and all — which is the exact narration false-positive this gate exists to
        # avoid. Measured: a goal carrying the tokens only in its TITLE fires
        # without the flag and stays correctly silent with it. It agreed with the
        # correct path on the first case tried, which is how it survived a
        # hand-run end-to-end check (guard-347: verify every flag of a wrapper
        # invocation; guard-920: the tested shape must BE the production shape —
        # the unit tests all passed --query-json while this call site did not).
        bash "$SCRIPT_DIR/aspirations-query.sh" --goal-field id "$GOAL_ID" --full \
            | python3 "$SCRIPT_DIR/mandated-retrieval-check.py" \
                  --goal-id "$GOAL_ID" --session-file "$ret_file" --query-json || true
    else
        perf="true"   # g-115-2201: retrieval fired for this goal
        # Retrieval was performed for this goal — run safety-net feedback if
        # pending. BACKSTOP call site (g-115-3123): do_state_update already ran
        # this immediately before phase-4-26-gate.sh, so on the normal loop path
        # the helper finds pending=false and no-ops. It still fires when
        # state-update never ran (operator retry, crash-resume straight into
        # learning-gate) or when the repair there failed and left pending=true.
        _repair_utilization_pending
        echo "[iteration-close] retrieval-summary: performed=true goal=$GOAL_ID"
    fi

    # ── Pre-apply-consult DRIFT gate (g-115-2201) ─────────────────────────
    # Consume the retrieval-performed flag ($perf) computed above. On N
    # consecutive framework-deep closes with performed=false, set the
    # force_pre_apply_consult sentinel so the next aspirations-precheck forces a
    # retrieve.sh consult — code-review-protocol step 4 drifted to a 100% miss
    # rate on framework deep goals (g-115-2194/2195/2179, 2026-07-14) and an
    # advisory cannot fix a 3/3 miss. The pure, unit-tested helper owns the
    # decision (incl. work_class resolution); this block owns only the WM
    # read/write, mirroring the force_tree_maintain template above. Python
    # script path via $CORE_ROOT (NOT $SCRIPT_DIR — MSYS path resolution on
    # Windows, same reason as the obligation-audit call below); wm-*.sh via
    # $SCRIPT_DIR (bash-invoked). Routine closes are transparent to the streak,
    # so the whole block is guarded by OUTCOME==deep — the common path is one
    # bash comparison. Fail-open everywhere; a gate error never wedges the close.
    if [[ "$OUTCOME" == "deep" ]]; then
        local _pac_streak _pac_decision _pac_new _pac_set
        _pac_streak="$(bash "$SCRIPT_DIR/wm-read.sh" pre_apply_consult_miss_streak 2>/dev/null | tr -dc '0-9')"
        [ -z "$_pac_streak" ] && _pac_streak=0
        # g-115-2655: framework-files-edited signal. Only a framework-deep close
        # that ACTUALLY edited a framework file (core/.claude/CLAUDE.md/mind_api)
        # is a real pre-apply-consult miss — a read-only framework diagnostic
        # (tree scan, gate audit) edits none and must stay transparent to the
        # streak, else a run of diagnostics climbs it on false pretenses and
        # re-fires the gate every iteration. iteration-commit stamps the goal-id
        # into each commit message, so grep-by-goal-id finds THIS goal's
        # commit(s) regardless of HEAD position or state-update backgrounding.
        # --since bounds the walk cheaply (the goal's commit is minutes old).
        # Fail-SAFE to "true" on any git error (preserve the real-drift catch);
        # only a clean git success that shows no framework file flips to "false".
        # world/conventions/ is intentionally out of scope (external/gitignored,
        # never committed, domain-classified not framework).
        local _fw_edited="true" _fw_files
        if _fw_files="$(git -C "$PROJECT_ROOT" log --fixed-strings --grep="$GOAL_ID" \
                --since='2 days ago' -5 --name-only --format= 2>/dev/null)"; then
            if [[ -z "$_fw_files" ]]; then
                _fw_edited="false"   # no commit mentions this goal — no framework file committed
            elif printf '%s\n' "$_fw_files" | grep -qE '^(core/|\.claude/|CLAUDE\.md|mind_api/)'; then
                _fw_edited="true"
            else
                _fw_edited="false"   # commit(s) exist but touch no framework file
            fi
        fi
        _pac_decision="$(python3 "$CORE_ROOT/scripts/pre-apply-consult-drift-gate.py" \
            --goal "$GOAL_ID" --source "$SOURCE" --outcome "$OUTCOME" \
            --performed "$perf" --streak "$_pac_streak" \
            --framework-edited "$_fw_edited" 2>/dev/null || echo '{}')"
        _pac_new="$(printf '%s' "$_pac_decision" | python3 -c 'import sys,json
try: print(int(json.load(sys.stdin).get("new_streak",0)))
except Exception: print(0)' 2>/dev/null || echo 0)"
        _pac_set="$(printf '%s' "$_pac_decision" | python3 -c 'import sys,json
try: print("true" if json.load(sys.stdin).get("set_sentinel") else "false")
except Exception: print("false")' 2>/dev/null || echo false)"
        printf '%s' "$_pac_new" | bash "$SCRIPT_DIR/wm-set.sh" pre_apply_consult_miss_streak >/dev/null 2>&1 || true
        if [[ "$_pac_set" == "true" ]]; then
            local _pac_now
            _pac_now="$(date +%Y-%m-%dT%H:%M:%S)"
            printf '{"triggered_at":"%s","goal_id":"%s","streak":%s,"source":"framework-deep-consult-drift"}' \
                "$_pac_now" "$GOAL_ID" "$_pac_new" \
                | bash "$SCRIPT_DIR/wm-set.sh" force_pre_apply_consult >/dev/null 2>&1 || true
            echo "[iteration-close] LLM-ACTION: pre-apply-consult drift ($_pac_new consecutive framework-deep closes, retrieval performed=false) — next precheck MUST run retrieve.sh (code-review-protocol step 4) before goal selection" >&2
        fi
    fi
    # ── end pre-apply-consult drift gate ──────────────────────────────────

    # Check REFLECTABLE unreflected hypotheses — signal via exit; LLM sees stderr.
    # g-115-6173: the g-115-5358 widening made the raw --unreflected length the
    # full never-reflected BACKLOG (measured 2026-08-14: 383, of which 378 are
    # structurally unreflectable — UNRESOLVABLE/EXPIRED/no-outcome), so len()
    # here fired this nudge on every close forever. Count via the shared
    # _reflectable filter (outcome CONFIRMED/CORRECTED) so a firing means
    # "reflection work actually exists".
    local unreflected
    unreflected="$(bash "$SCRIPT_DIR/pipeline-read.sh" --unreflected | SCRIPTS_DIR="$SCRIPT_DIR" python3 -c "
import json, os, sys
sys.path.insert(0, os.environ['SCRIPTS_DIR'])
try:
    import _reflectable
    d = json.load(sys.stdin)
    print(_reflectable.count_reflectable(d) if isinstance(d, list) else 0)
except Exception:
    print(0)
" || { echo "[iteration-close] WARN: pipeline-read --unreflected failed — defaulting to 0 (LLM may miss unreflected hypothesis count)" >&2; echo "0"; })"
    if [[ "$unreflected" -gt 0 ]]; then
        echo "[iteration-close] LLM-ACTION: $unreflected reflectable unreflected hypothesis/es — digest § LEARNING-GATE item 3" >&2
    fi

    # Tree growth check — report candidate count if any
    local decompose_count
    decompose_count="$(bash "$SCRIPT_DIR/tree-read.sh" --decompose-candidates | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(len(d) if isinstance(d, list) else 0)
except Exception:
    print(0)
" || { echo "[iteration-close] WARN: tree-read --decompose-candidates failed — defaulting to 0 (decompose surfacing disabled this iteration)" >&2; echo "0"; })"
    if [[ "$decompose_count" -gt 0 ]]; then
        echo "[iteration-close] INFO: $decompose_count tree-node decompose candidates"
    fi

    # g-115-81: critical-debt signal. When debt > tree_debt_check.debt_threshold*3
    # (currently 120 — tree_debt_check.debt_threshold=40 since g-001-187/rb-290;
    # fallback below kept at 120 to match), set force_tree_maintain WM signal so the next iteration's
    # precheck can auto-invoke /tree maintain --backlog. Without this, the advisory
    # INFO line above gets abbreviated under context pressure and backlog grows
    # unchecked (session 48→50: 85→151 candidates, +77%). Threshold formula matches
    # aspirations-loop-digest.md Phase 8.7 "--backlog when debt > threshold*3".
    local debt_threshold_triple
    debt_threshold_triple="$(python3 -c "
import yaml
with open('core/config/tree.yaml','r',encoding='utf-8') as f:
    cfg = yaml.safe_load(f)
print(int(cfg['tree_debt_check']['debt_threshold'] * 3))
" 2>/dev/null || echo "120")"
    if [[ "$decompose_count" -gt "$debt_threshold_triple" ]]; then
        # g-115-82: cooldown throttling. Without this, the signal re-fires every
        # iteration even when /tree maintain --backlog was just invoked — starving
        # goal progress at 150+ candidate backlogs (rb-280). Reads last fire time
        # from WM and skips re-setting within cooldown_hours.
        local cooldown_hours
        cooldown_hours="$(python3 -c "
import yaml
with open('core/config/tree.yaml','r',encoding='utf-8') as f:
    cfg = yaml.safe_load(f)
print(cfg.get('tree_debt_check',{}).get('force_maintain_cooldown_hours', 1.0))
" 2>/dev/null || echo "1.0")"
        # Read persistent last-fire timestamp from dedicated slot.
        # Previously read from force_tree_maintain.triggered_at, but precheck
        # clears that slot when it consumes the signal — so the cooldown check
        # had no anchor across iterations and the signal re-fired every
        # iteration (observed at iter 47 of session 50 post-compact).
        # Dedicated slot survives signal consumption.
        local last_fire
        last_fire="$(bash "$SCRIPT_DIR/wm-read.sh" force_tree_maintain_last_fire 2>/dev/null \
            | python3 -c "
import sys
line = sys.stdin.read().strip()
# wm-read returns bare YAML/JSON value; accept either ISO string or quoted
print(line.strip().strip('\"').strip(\"'\") if line and line != 'null' else '')
" 2>/dev/null || echo "")"
        local should_fire="1"
        if [[ -n "$last_fire" ]]; then
            # Pass last_fire + cooldown_hours via env vars (matching the safe
            # pattern at L221-228, L661 above) so no shell interpolation
            # happens inside the Python heredoc — fresh-eyes finding
            # bravo-769, fixed in g-115-388.
            should_fire="$(LAST_FIRE="$last_fire" COOLDOWN="$cooldown_hours" python3 -c '
import os
from datetime import datetime
try:
    last = datetime.strptime(os.environ["LAST_FIRE"], "%Y-%m-%dT%H:%M:%S")
    elapsed_hours = (datetime.now() - last).total_seconds() / 3600
    print("1" if elapsed_hours >= float(os.environ["COOLDOWN"]) else "0")
except Exception:
    print("1")
' 2>/dev/null || echo "1")"
        fi
        if [[ "$should_fire" == "1" ]]; then
            echo "[iteration-close] LLM-ACTION: tree debt critical ($decompose_count > $debt_threshold_triple) — next iteration MUST invoke /tree maintain --backlog before selecting a goal" >&2
            local now_ts
            now_ts="$(date +%Y-%m-%dT%H:%M:%S)"
            printf '{"triggered_at":"%s","decompose_count":%s,"threshold":%s}' \
                "$now_ts" "$decompose_count" "$debt_threshold_triple" \
                | bash "$SCRIPT_DIR/wm-set.sh" force_tree_maintain >/dev/null 2>&1 \
                || echo "[iteration-close] WARN: wm-set force_tree_maintain failed — signal not persisted" >&2
            # Separate persistent slot so cooldown check survives signal consumption
            # by precheck (which clears force_tree_maintain but not this slot).
            printf '"%s"' "$now_ts" \
                | bash "$SCRIPT_DIR/wm-set.sh" force_tree_maintain_last_fire >/dev/null 2>&1 \
                || echo "[iteration-close] WARN: wm-set force_tree_maintain_last_fire failed — cooldown broken" >&2
        else
            echo "[iteration-close] INFO: tree debt critical ($decompose_count > $debt_threshold_triple) but force_tree_maintain signal on cooldown (<$cooldown_hours""h since last fire) — skipping re-emit"
        fi
    fi

    # Phase 9.5d: Obligation abbreviation audit (g-243-06 port).
    # Scans this iteration's journal section for `OBLIGATION ABBREVIATED:` claims
    # and validates each against the runtime schema + authoritative state. Non-blocking.
    # The audit was dead code in aspirations-learning-gate/SKILL.md (not on hot path).
    # Path resolution: $CORE_ROOT/scripts (Windows form via _platform.sh) NOT
    # $SCRIPT_DIR (raw MSYS /c/... — Python treats as relative-to-drive-root and
    # silently fails to resolve). Same fix pattern as L298 CORE_SCRIPTS env var.
    # stderr → iteration-close-stderr.log (was 2>/dev/null which silently swallowed
    # tracebacks). Same observability pattern as L519-520 post-state-update-gate
    # call (g-240-79 lineage). g-115-387 fix; companion to g-115-385 path bug.
    mkdir -p "$CORE_ROOT/logs"
    python3 "$CORE_ROOT/scripts/obligation-audit.py" --goal-id "$GOAL_ID" 2>>"$CORE_ROOT/logs/iteration-close-stderr.log" \
        || echo "[iteration-close] WARN: obligation-audit failed — non-fatal" >&2

    # Phase 9.5e: Tree-accuracy sync (g-001-137, rb-292).
    # /review-hypotheses SKILL.md Tree Update Protocol Step 2 prescribes
    # per-node accuracy updates but is LLM-invoked — observed drift: node-level
    # accuracy stayed null across 85 archived + 12 resolved records (g-001-136
    # root-cause finding). Mirrors g-001-132: convert LLM-discretionary step
    # into bash-gated obligation. Only fires on deep outcomes — routine goals
    # don't change accuracy data so running on them wastes I/O.
    # Idempotent: the script diffs current vs computed and skips no-op writes.
    # --quiet suppresses output on zero-change runs (most iterations).
    if [[ "$OUTCOME" == "deep" ]]; then
        bash "$SCRIPT_DIR/tree-accuracy-sync.sh" --quiet 2>/dev/null \
            || echo "[iteration-close] WARN: tree-accuracy-sync failed — non-fatal" >&2
    fi

    _checkpoint_refresh learning_gate
    # LLM residue at this phase:
    #   - Meta-learning signal ("did learning suggest a better procedure?")
    #   - Forced encoding if no tree update for deep outcome (curator residue)
    # See core/config/iteration-close-digest.md § LEARNING-GATE.
}

# --------------------------- phase: productivity-check ---------------------------
do_productivity_check() {
    _CURRENT_PHASE="productivity-check"
    # Invokes the productivity stop gate, which may set stop-requested.
    # Runs AFTER learning-gate phase; productive_goals counter is already updated.
    echo "[iteration-close] productivity-check"

    # Durability-critical push FIRST (g-115-2915, felt-sense finding alpha ~goal 294):
    # iteration-push runs BEFORE the ~16-script maintenance battery below, not after it.
    # The push is the one durability-critical, cross-fleet-visible step; gating it behind
    # ~16 best-effort maintenance scripts meant that on a busy fleet the battery wall-clock
    # (each own-cloud S3 RMW can be 3.8-10.1s — see the gate-firings-flush note below)
    # approached/exceeded the 2-min Bash timeout, SIGTERM-killing the terminal Bash before
    # the push completed and stranding local commits (observed: 2 timeouts, 4 commits
    # stranded, manual push to recover; git fetch itself was 0.6s so network was NOT the
    # cause — cumulative battery latency was). Pushing first guarantees the commits land
    # regardless of battery slowness. Bonus: iteration-push.sh's merge REFUSES a dirty
    # working tree (never overwrites); running it before the battery writes its ~6 files
    # means the merge sees a cleaner tree and defers less often. do_state_update (an
    # earlier phase) already committed this iteration's work, so the push has all it needs
    # and NO dependency on the battery below. fail-soft, rate-limited (g-115-1734, USER
    # DIRECTIVE Zachary 2026-07-02): batches (pushes only when origin is behind by >= N
    # commits OR oldest unpushed >= T min), skips when .git/index.lock is held (guard-853),
    # never force-pushes, fail-open — a push failure logs to stderr and NEVER aborts
    # productivity-check or blocks loop continuation.
    # DELIBERATELY UNREDIRECTED (g-115-4484). iteration-push.sh's log() writes
    # every line to stderr — which this call site inherits, so the alarms reach
    # the loop LLM in-turn — AND tees each line to $GITDIR/iteration-push.log,
    # so persistence is already covered at the SOURCE, for every call site and
    # every line. A capture-then-emit tee was added here first and then removed:
    # it duplicated that persistence into a second file, giving a reader two
    # places to look for one stream. Do NOT re-add a plain `2>>log` either — the
    # shape the two staleness checks below use — because it would silence the
    # stranded-depth and integrate-defer alarms, which exist to be READ IN-TURN.
    { bash "$SCRIPT_DIR/iteration-push.sh" --repo "$PROJECT_ROOT"; } || true
    # Load-bearing capture priority-merge lane (g-306-293). THE NAMED CADENCE for
    # the fast lane: this phase runs once per reducer iteration, where the full
    # generalize_down runs only at aspirations-consolidate Step -1.
    #
    # Why it is worth a call here at all: generalize_down enumerates ONLY
    # closed-pending-merge Bodies, so an ACTIVE worker's captures are invisible
    # to the reducer no matter how often consolidation runs. Measured 2026-08-15
    # (alpha, cc-08): one Body 21 units deep held 237 capture entries no reducer
    # could see. This pass reads ACTIVE Bodies too and copies forward only the
    # entries the worker flagged `load_bearing`.
    #
    # Placed immediately after the push and BEFORE the maintenance battery on
    # purpose: it writes the reducer WM, so anything downstream that reads WM
    # this iteration sees the merged captures rather than next iteration's copy.
    # It never marks a Body merged and deletes nothing — generalize_down stays
    # the sole owner of Body lifecycle, so the worst case here is a no-op.
    # Idempotent by content hash, so the later full merge does not double-apply.
    # `|| true` + the script's own unconditional exit 0: an accelerator sitting
    # in front of a merge that will happen anyway must never fail the close.
    # stdout is DELIBERATELY UNREDIRECTED — the one-line summary is the
    # telemetry the completion bar asks for and it exists to be READ IN-TURN
    # (same reasoning as the push above); the per-run JSONL under
    # agents/<agent>/session/capture-fast-lane.jsonl carries the trend.
    # `python3` (not `py -3`) is correct inside this .sh — it sources _paths.sh —
    # and the path routes through `_winpath` per the file-local rule for every
    # `python3 <file-arg>` call (the shim needs C:/ paths on Windows).
    { python3 "$(_winpath "$SCRIPT_DIR/capture_fast_lane.py")"; } \
        2>>"$CORE_ROOT/logs/iteration-close-stderr.log" || true
    # Phase 4.25 drift compliance check (g-248-16, rb-428). Warn-only — surfaces
    # when experience.jsonl is stale because the LLM-only experience-add.sh
    # call has drifted out of the hot path. Never blocks iteration.
    # `|| true`: script emits its own stderr WARN on staleness and returns non-zero
    # intentionally as a signal; a bash invocation failure (missing script, etc.)
    # is also tolerated because the alternative — aborting productivity-check —
    # is worse than a missed canary tick. Silent-fallback justified.
    # stderr → iteration-close-stderr.log (g-001-255 — was unredirected, so
    # WARN lines went to default stderr and never persisted; same observability
    # pattern as L519-520 post-state-update-gate redirect).
    bash "$SCRIPT_DIR/experience-staleness-check.sh" 2>>"$CORE_ROOT/logs/iteration-close-stderr.log" || true
    # Phase 8e drift backstop (rb-428 follow-up). Warn-only — surfaces when
    # decision-rules-append.sh has NOT been invoked for N hours, indicating
    # Step 8e has drifted out of the hot path. Parallel to Phase 4.25 check.
    # `|| true` justified same as above — script surfaces its own staleness WARN.
    # stderr → iteration-close-stderr.log (g-001-255 — same justification as L875).
    bash "$SCRIPT_DIR/decision-rules-staleness.sh" 2>>"$CORE_ROOT/logs/iteration-close-stderr.log" || true
    # Precheck-phase drift backstop (rb-428 class). recurring-precondition-sweep
    # is also invoked by aspirations-precheck SKILL.md:544 (LLM-dispatched);
    # running it here as well ensures the bash path keeps the overdue_ratio
    # inflation fixed even if the LLM drops the precheck call. Script is
    # idempotent and cheap (scans recurring goals, advances lastAchievedAt
    # only when a structured precondition fails). Always exits 0.
    # cygpath: python3 via the shim routes to Windows Python, which cannot
    # open /c/... paths — so convert $SCRIPT_DIR via the guarded `_winpath`
    # helper (cygpath on Windows, POSIX passthrough on Linux/macOS). The same
    # helper any future python3 <file-arg> invocation in this file must use.
    # `|| true` justified: script documented as "Always exits 0" (L730) — the
    # `|| true` is belt-and-suspenders against python-level exceptions that would
    # otherwise abort productivity-check. Silent tolerance here is fine because
    # the script itself is the signal path, not its exit code.
    python3 "$(_winpath "$SCRIPT_DIR/recurring-precondition-sweep.py")" || true
    # Evolution-stub expiry sweep (F2, 2026-05-15). The historical
    # [AUTO-FILLED] auto-completion mechanism that guard-544 assumed would
    # finalize abandoned awaiting_completion stubs was never implemented, so
    # stubs in the five evolution streams (self/program/skill/rule/script)
    # accumulated indefinitely (38 found stale on the 2026-05-15 audit).
    # evolution-stub-expiry.py transitions stubs older than 24h to the
    # schema-defined `expired` terminal status (rationale recorded as
    # unsupplied — never a fabricated [AUTO-FILLED] string). Wired here, not
    # as a recurring goal in the shared aspirations queue, for the same
    # reason recurring-precondition-sweep above is: it is idempotent, cheap
    # (scans 5 JSONL streams, no-op when nothing is stale), needs no LLM
    # judgment, and must not contend on the heavily-serialized aspirations
    # write-lock. cygpath: same Windows-Python file-arg constraint as the
    # sweep above. Output: this script (unlike the sweep above) always emits
    # a one-line summary; redirect BOTH streams to the iteration-close
    # diagnostic log so the sweep trail is observable without polluting
    # productivity-check stdout (same sink as agent-watchdog --tick below).
    # `|| true` justified: a missed sweep tick is fully recovered by the
    # next iteration (idempotent + 24h threshold), so it must never abort
    # productivity-check. See guard-544 and
    # world/conventions/self-program-evolution.md "Stub Expiry".
    python3 "$(_winpath "$SCRIPT_DIR/evolution-stub-expiry.py")" --threshold-hours 24 \
        >>"$CORE_ROOT/logs/iteration-close-stderr.log" 2>&1 || true
    # Evolution-stub PENDING check (g-115-2180, 2026-07-14) — the missing PROMPT
    # half of the same lifecycle. The expiry sweep ABOVE is the honest fallback
    # ("rationale never supplied"); nothing ever PROMPTED the LLM to supply it.
    # Measured: of 65 MATERIAL Self edits fleet-wide only 11 (17%) ever reached
    # the user; 22 EXPIRED unnotified — the agent's identity changed and the user
    # was never told, silently breaking the 2026-04-22 "notify after, revert if
    # wrong" bargain that guard-380 encodes. This sets a `force_evolution_finalize`
    # WM sentinel while the stub is STILL FINALIZABLE; aspirations-precheck Phase
    # 0-pre2.5 forces the completion before goal selection. Same rb-428 sentinel
    # shape as tree-debt / experience-archival / fresh-eyes-code / metric-encoding
    # — every sibling obligation already had a forcing consumer; this one did not.
    #
    # Ordered AFTER the expiry sweep deliberately: a stub still `awaiting_completion`
    # once expiry has run is guaranteed < 24h old and therefore genuinely
    # finalizable, so the sentinel can never name a stub that evolution-complete.sh
    # would refuse (it hard-errors on status != awaiting_completion).
    #
    # Scoped to the self+program streams ONLY. script-evolution has 152 pending /
    # 1992 expired vs 23 final (99% expiry) — widening this gate there would fire
    # every iteration forever and train the agent to ignore the sentinel. That
    # backlog is filed separately.
    #
    # Same stderr sink + `|| true` contract as the sweeps around it: a missed tick
    # is fully recovered next iteration (idempotent, re-fires until finalized), so
    # it must never abort productivity-check.
    bash "$SCRIPT_DIR/evolution-stub-pending-check.sh" --threshold-minutes 20 \
        >>"$CORE_ROOT/logs/iteration-close-stderr.log" 2>&1 || true
    # Execution-diary trim (g-333-03, asp-333 A1): bound the otherwise-unbounded
    # execution-diary.jsonl. It is appended every phase and full-scanned by
    # presence-tick (on EVERY tool call), postcompact-restore, and skill_discovery.
    # cmd_trim existed (execution-diary.py:261) with ZERO callers; wired here among
    # the idempotent once-per-iteration maintenance sweeps. Cheap: cmd_trim
    # early-returns without rewriting when nothing is older than 8h. Fail-open: the
    # diary is observability — a missed trim is recovered next iteration. `|| true`
    # + stderr redirect mirror the sibling sweeps; uses the .sh wrapper (proven at
    # L370) rather than direct python3+cygpath.
    bash "$SCRIPT_DIR/execution-diary.sh" trim --hours 8 \
        >>"$CORE_ROOT/logs/iteration-close-stderr.log" 2>&1 || true
    # Gate-firings spool flush (g-115-2405): under own-cloud, _gate_log.log()
    # appends each firing to a machine-local spool instead of paying a
    # whole-object S3 RMW per record (measured 3.8-10.1s each at ~40MB store).
    # This tick drains the spool into meta/gate-firings.jsonl with ONE batched
    # locked RMW. Self-gating: quiet no-op when the spool is empty; internal
    # 300s min-interval bounds S3 churn to ~1 RMW / 5min / box (burst override
    # at 200 spooled records). Idempotent + duplicate-safe (dedup by serialized
    # line — the same identity merge_append_only_jsonl unions by), so `|| true`
    # is safe: a missed tick just leaves the spool for the next iteration.
    python3 "$(_winpath "$SCRIPT_DIR/gate-firings-flush.py")" \
        >>"$CORE_ROOT/logs/iteration-close-stderr.log" 2>&1 || true
    # Citation-credit sweep (g-115-6948): converts commit-message rb-/guard-
    # citations (measured 84/day fleet-wide vs 1-7 explicit helpful events/day)
    # into times_inferred_helpful increments — the mechanical consultation-
    # credit lane. Self-gating: internal 3600s min-interval marker makes this a
    # stat-check no-op between harvests; fleet-shared ledger in meta/ dedups
    # across boxes; daemon preflight + claim-first ledgering bound outage cost
    # to zero (preflight) or one commit's credits (mid-sweep death). Same
    # idempotent/cheap/fail-open contract as the sibling sweeps.
    python3 "$(_winpath "$SCRIPT_DIR/citation-credit-sweep.py")" --quiet \
        >>"$CORE_ROOT/logs/iteration-close-stderr.log" 2>&1 || true
    # Pending-deploys re-probe sweep (SG-b, g-115-2688-b). The do_verify gate
    # resolves the CLOSING goal's deploy obligations; this all-sweep re-probes
    # every REMAINING entry (from earlier closures left unverified) so an entry
    # whose CI has since concluded is auto-cleared (ok) or surfaced as a HIGH
    # Unblock (failed) mid-session — not left to linger until the SG-c stop-hook.
    # Same idempotent/cheap/fail-open contract as the sweeps above: has-pending
    # fast-exits when nothing is pending (the common case), the CI poll is
    # bounded, and the script always exits 0. `|| true` + full stderr redirect
    # mirror the sibling sweeps.
    bash "$SCRIPT_DIR/pending-deploys-gate.sh" --agent "$AGENT" \
        >>"$CORE_ROOT/logs/iteration-close-stderr.log" 2>&1 || true
    # CRITICAL: productivity-stop-gate.sh is the ONLY authorized caller of
    # stop-requested outside /stop (per .claude/rules/stop-hook-compliance.md
    # productivity-gate exception). If the gate crashes, silently skipping means
    # long-running sessions can drift past their productivity floor invisibly.
    # Surface the failure so the operator knows the gate did not run this
    # iteration — without aborting productivity-check (fail-open on non-critical
    # follow-ups). bravo g-240-70 / fresh-eyes msg-bravo-312 (2026-04-24 iter 37).
    bash "$SCRIPT_DIR/productivity-stop-gate.sh" \
        || echo "[iteration-close] WARN: productivity-stop-gate.sh failed — productivity threshold not evaluated this iteration" >&2

    # Health-snapshot (health-ledger subsystem; core/config/conventions/health-ledger.md).
    # Runs AFTER productivity-stop-gate.sh so THIS iteration's productivity snapshot
    # exists to read — the 4 health signals are reused from it, not recomputed — and
    # BEFORE the ITERATION COMPLETE imperative so it never delays loop continuation.
    # Fire-and-forget / fail-open: a telemetry miss must never abort productivity-check.
    # Phase 1 (collect-only): appends one record to agents/<agent>/health/<date>.jsonl;
    # detection + revert activate in later phases per health_regression.mode. Direct
    # python (no .sh wrapper) matches the sibling helper-python pattern below
    # (agent-watchdog.py / stale-sentinel-canary.py). cygpath: Windows-Python file-arg.
    python3 "$(_winpath "$SCRIPT_DIR/health-ledger-append.py")" \
        2>>"$CORE_ROOT/logs/iteration-close-stderr.log" || true

    # Agent watchdog tick — periodic session observability probes. Replaces the
    # detached daemon model (which died-on-parent-exit on Git Bash for Windows
    # due to flaky nohup+disown semantics). Reads prev state from
    # <agent>/session/watchdog-prev-state.json, runs each probe.check() (running-
    # sid, heartbeat, background-job, stop-hook-block), emits transitions to
    # core/logs/watchdog-<agent>.jsonl, and saves new state. Cross-platform via
    # pure file I/O — no daemonization needed. Fail-open: any error surfaces on
    # stderr (routed to iteration-close-stderr.log) but never aborts the phase.
    # See core/scripts/agent-watchdog.py docstring + --tick mode.
    python3 "$(_winpath "$SCRIPT_DIR/agent-watchdog.py")" --tick \
        2>>"$CORE_ROOT/logs/iteration-close-stderr.log" || true

    # Worker-ref consumption lane (g-306-283) — the wired executable caller for
    # worker-ref-consume.sh, which had ZERO call sites from its landing
    # (g-306-264, 2026-08-06) until 2026-08-13: workers pushed carrier refs and
    # nothing ever SHOWED them to the reducer, so completed framework work
    # stranded invisibly while main re-implemented it from scratch — measured
    # re-do with quality regression (g-115-5945 N6), backlog regrowth ~35
    # real-work commits/day (N3). This phase is reducer-only by the worker
    # phase split, so the report runs exactly where consumption decisions live.
    # Throttled on a marker mtime (iteration-push FETCH_HEAD idiom) because the
    # report fetches refs/workers/* from origin, and timeout-bounded like the
    # GROUND producer below for the same reason. --check prints the per-ref
    # report plus a LOUD banner when an outstanding TIP breaches depth/age
    # thresholds. Advisory and fail-open; it deliberately files NO goals — the
    # ACTING lane is the recurring consume/disposition world goal (g-306-283
    # shape b), so this hook is the between-runs visibility half.
    WR_MARKER="$PROJECT_ROOT/.git/worker-ref-report.last"
    WR_INTERVAL_MIN="${WORKER_REF_REPORT_INTERVAL_MIN:-60}"
    WR_DUE=1
    if [[ "$WR_INTERVAL_MIN" -gt 0 && -f "$WR_MARKER" ]] 2>/dev/null; then
        WR_CT="$(date -r "$WR_MARKER" +%s 2>/dev/null || echo "")"
        case "$WR_CT" in
            ''|*[!0-9]*) ;;   # unreadable mtime -> run anyway
            *) WR_AGE_MIN=$(( ($(date +%s) - WR_CT) / 60 ))
               [[ "$WR_AGE_MIN" -ge 0 && "$WR_AGE_MIN" -lt "$WR_INTERVAL_MIN" ]] && WR_DUE=0;;
        esac
    fi
    if [[ "$WR_DUE" -eq 1 ]]; then
        # Touch-first is deliberate thrash protection (a hanging check must not
        # retry every iteration) — but it means a FAILED check has already
        # advanced its own cadence, so the failure needs a DURABLE record: the
        # WARN below goes to stderr, which is invisible whenever this script
        # runs backgrounded (guard-772). F-001 of g-306-287: emit a gate-log
        # row (worker-ref-report-check, fail_open) so a dark visibility path
        # is detectable in meta/gate-firings.jsonl rather than merely
        # un-narrated. Clean passes log nothing (own-cloud append cost — same
        # posture as the pre-commit suite's BLOCK-only telemetry).
        touch "$WR_MARKER" 2>/dev/null || true
        timeout 45 bash "$SCRIPT_DIR/worker-ref-consume.sh" --check \
            2>>"$CORE_ROOT/logs/iteration-close-stderr.log" \
            || { echo "[iteration-close] WARN: worker-ref-consume --check did not complete (non-fatal; stranded-ref visibility not refreshed this iteration)" >&2
                 bash "$SCRIPT_DIR/gate-log.sh" worker-ref-report-check fail_open \
                   --caller "iteration-close:worker-ref-report" \
                   --gate-error "worker-ref-consume --check rc!=0 or 45s timeout; cadence marker already advanced, next attempt in ${WR_INTERVAL_MIN}min" \
                   >/dev/null 2>&1 || true; }
    fi

    # GROUND producer (g-335-830) — Pattern B domain-overlay seam, same shape as
    # the pipeline-reconcile-gate and outcome-metrics hooks above: CORE stays
    # domain-agnostic and fires the script only if a domain supplies one, so a
    # fresh world with no such script simply never fires.
    #
    # Publishes a <=200-char first-person line describing THIS agent's real
    # state to any live session, so an in-world character asked "what have you
    # been doing?" answers from disk instead of inventing. Placed here because
    # a close is the moment the agent actually HAS something true to report.
    #
    # Fire-and-forget / fail-open, and `timeout`-bounded because this and the
    # worker-ref lane above are the two hooks in this phase that touch the
    # network — a hung remote must not become the loop's iteration time. The
    # script itself exits 0 on every benign no-op (no live server, no key,
    # nothing to report), so the WARN below fires only for a real delivery
    # failure.
    if [[ -n "${WORLD_DIR:-}" && -f "$WORLD_DIR/scripts/sis-self-summary-post.sh" ]]; then
        timeout 25 bash "$WORLD_DIR/scripts/sis-self-summary-post.sh" \
            --goal-id "$GOAL_ID" >/dev/null \
            2>>"$CORE_ROOT/logs/iteration-close-stderr.log" \
            || echo "[iteration-close] WARN: sis-self-summary-post.sh did not deliver (non-fatal; in-world grounding not refreshed this iteration)" >&2
    fi

    # user-signal-refresh (g-001-269) -- the WRITER for user-signal-snapshot.yaml,
    # which goal-selector's user_signal_boost criterion reads. Hosted HERE, beside
    # the watchdog tick, deliberately: the consumer's docstring says the snapshot is
    # "refreshed per iteration by the signal-refresh hook in aspirations-precheck",
    # and that LLM-dispatched hook was never built -- so the criterion sat inert
    # with a reader and no writer. An LLM-dispatched refresh would reintroduce the
    # same fragility that left the fresh-eyes cadence 92 goals un-dispatched
    # (g-001-262); a script-side tick cannot be forgotten under context pressure.
    # Running at CLOSE rather than at precheck is intentional and sufficient: the
    # snapshot only needs to be fresh by the NEXT selection, and close is
    # guaranteed-executed where precheck pseudocode is not. Fail-open, sub-second,
    # single file write. See core/scripts/user-signal-refresh.py docstring.
    #
    # PROVENANCE (g-029-101 -> back-ported UP 2026-07-31, g-115 v2.8.7): this block
    # was born ZDS-LOCAL; the 2026-07-30 mirror-style sync (1df2b2e8) deleted it
    # there and the loss was silent -- the reader kept reading a well-formed
    # snapshot that had simply stopped advancing (frozen ~20h before detection).
    # It now lives HERE (dev source of the promotion chain), so framework syncs
    # carry it instead of deleting it. That incident is why reconcile-not-mirror
    # (.claude/rules/promotion-cycle.md "Pre-Overwrite Drift Gate") is mandatory.
    python3 "$(_winpath "$SCRIPT_DIR/user-signal-refresh.py")" \
        >>"$CORE_ROOT/logs/iteration-close-stderr.log" 2>&1 || true

    # Monitor-tick -- FW-1b demoted pure-monitoring probes (g-317-13). Runs each
    # ENABLED probe (allowlist in core/config/monitor-probes.yaml; ships empty =
    # inert) at its own interval_hours, beside the watchdog tick -- same LOCAL-tick
    # pattern, no daemon, no cloud cron (guard-441). A clean probe records a
    # last-run marker in <agent>/session/monitor-tick-state.json and files NOTHING
    # (no goal slot); a tripped probe converts to ONE deduped Investigate via
    # monitor-finding-convert.py. Ships inert until probes are migrated into the
    # allowlist (Phase-3 / Apply-3). Fail-open: errors routed to the diagnostic
    # log, never aborts productivity-check. cygpath: Windows-Python file-arg.
    python3 "$(_winpath "$SCRIPT_DIR/monitor-tick.py")" --tick \
        2>>"$CORE_ROOT/logs/iteration-close-stderr.log" || true

    # Embedding-index freshness tick (g-306-84) — per-box staleness check for
    # the retrieval embedding index. Silent no-op while embedding_blend_enabled
    # is false (one YAML read) or the index is absent (initial build is a
    # deliberate operator action, never hook-spawned). When the blend is live
    # and a corpus source is newer than the index — rb, guardrails, OR the
    # knowledge tree (_tree.yaml + node .md bodies, added g-115-3763) — spawns
    # `embedding-index-build.py --update` DETACHED (incremental — re-embeds
    # changed docs only), debounced to one attempt per 6h. Same LOCAL-tick
    # rationale as agent-watchdog above: the index is per-box daemon cache,
    # so a world-scoped recurring goal (runs on ONE box per firing) cannot
    # keep every box fresh. Fail-open: never delays loop continuation.
    python3 "$(_winpath "$SCRIPT_DIR/embedding-index-freshness.py")" \
        2>>"$CORE_ROOT/logs/iteration-close-stderr.log" || true

    # Cold-snapshot cadence tick (g-115-5279) — replaces recurring goal
    # g-115-4317, which burned a whole LLM iteration weekly to run one command.
    # Only the TRIGGER moved: cold-snapshot.sh / cold_snapshot.py are untouched.
    #
    # READ THE CADENCE COMMENT ON ITS NEIGHBOURS BEFORE COPYING THIS ONE. Every
    # other tick in this phase is deliberately PER-BOX (a per-box index, a
    # per-box watchdog, a per-box probe state), and their "a world-scoped
    # recurring goal runs on ONE box per firing" rationale is the exact INVERSE
    # of what this one needs: firing per-box here would take ~5x the snapshots
    # and ~5x the storage on this fleet, handing back the cost the move saves.
    # So the cadence stamp is a single shared S3 object beside the snapshots
    # (<env>/cold-snapshots/_last-run.json), claimed before the run — not a
    # per-agent WM slot, and not a synced world/ file (a read-through cache with
    # merge semantics is the defect, not the fix).
    #
    # Returns immediately: it decides, claims, and spawns the actual snapshot
    # DETACHED, so a multi-minute walk+compress+upload never becomes the loop's
    # iteration time. Fail-open like its neighbours. On any non-ok verdict the
    # run files an Investigate — g-115-4317's verification required exactly that,
    # and retiring the goal moves the obligation rather than dropping it.
    python3 "$(_winpath "$SCRIPT_DIR/cold-snapshot-tick.py")" --tick \
        2>>"$CORE_ROOT/logs/iteration-close-stderr.log" || true

    # Housekeeping tick (P1, 2026-08-21) — per-box temp-purge + scratchpad-GC
    # cadence. Root cause it fixes: the mechanical purge was scheduled through
    # the goal scorer, which ranks janitorial work last BY DESIGN (g-115-3319:
    # the open drain goal sat at rank #120/151 for weeks while pressure grew
    # 18x). Only the TRIGGER moved (cold-snapshot precedent above):
    # temp-drain-purge.sh + its guards are untouched, and the LLM drain stays
    # goal-driven. PER-BOX like watchdog/monitor (a temp store lives where its
    # agent runs), NOT fleet-stamped like cold-snapshot. Ships SHADOW (dry-run
    # + report-only) until aspirations.yaml housekeeping_tick.shadow is
    # flipped after fleet review of core/logs/housekeeping-<agent>.jsonl.
    # Self-gating 6h; spawns its worker DETACHED; fail-open. Second caller is
    # sessionstart-orchestrator.sh (assistant boxes never reach this phase).
    python3 "$(_winpath "$SCRIPT_DIR/housekeeping-tick.py")" --tick --source iteration-close \
        2>>"$CORE_ROOT/logs/iteration-close-stderr.log" || true

    # Stale-sentinel canary (g-115-717) — defense-in-depth for Cat C sentinels
    # (bash writer + SKILL-only consumer). Detects sentinels set continuously
    # across N canary runs without being cleared (consumer SKILL likely bypassed)
    # and files an Investigate goal. Threshold: stale_sentinel.threshold_iterations
    # in core/config/aspirations.yaml (default 3). Fail-open: any error is
    # non-fatal and routed to iteration-close-stderr.log. --quiet suppresses
    # the JSON report when nothing fired to keep clean iterations terse.
    python3 "$(_winpath "$SCRIPT_DIR/stale-sentinel-canary.py")" --quiet \
        2>>"$CORE_ROOT/logs/iteration-close-stderr.log" || true

    # Cadence-stale canary (g-115-2986) — defense-in-depth for the g-115-2984
    # cadence battery, one level below the sentinel canary. The battery makes the
    # registered cadence CHECKS un-skippable, but the DISPATCH (invoking the ritual skill
    # on a printed '▸ CADENCE FIRE' line) stays LLM-orchestrated. This canary
    # counts a cadence that keeps FIRING (its check returns exit 0) across N canary
    # runs without its dispatch stamp advancing — a skipped dispatch, the
    # felt-sense-starvation class (g-115-2982) — and files an Investigate.
    # Threshold: stale_cadence.threshold_iterations in core/config/aspirations.yaml
    # (default 3). Fail-open: any error is non-fatal and routed to
    # iteration-close-stderr.log. --quiet suppresses the JSON when nothing fired.
    python3 "$(_winpath "$SCRIPT_DIR/cadence-stale-canary.py")" --quiet \
        2>>"$CORE_ROOT/logs/iteration-close-stderr.log" || true

    # History-store vacuum tick (g-115-2792-b; design g-115-2792-a) — cadence
    # for _history_store.vacuum, which had NO caller (measured .history 8.1G =
    # 98% of WORLD_PATH). 24h time-gated + per-box locked + BACKGROUNDED so the
    # ~114k-file walk never blocks iteration close. Archive-before-delete
    # (Decision 3): orphan payloads copy to a lifecycle-exempt REMOTE graveyard
    # + are verified before any local unlink; archive/verify failure aborts
    # WITHOUT deleting. Lands SAFE (history_vacuum.apply defaults false =>
    # enumerate+report only) until g-115-2792-c runs the positive control and
    # flips apply=true. Same LOCAL-tick + fail-open (`|| true`) + stderr-sink
    # contract as the sibling ticks above. Uses the .sh wrapper (sources
    # _paths.sh, backgrounds its own run). Config: aspirations.yaml § history_vacuum.
    bash "$SCRIPT_DIR/history-vacuum-tick.sh" \
        >>"$CORE_ROOT/logs/iteration-close-stderr.log" 2>&1 || true

    # Aspirations hot-store eviction tick (2026-08-14, operator-directed S3
    # cost-down). Daily per-box cadence for aspirations-evict-completed.py,
    # which had NO call site (g-115-5771) while its target file was 54.4
    # GB/day of whole-object PUT churn (g-358-02). Same LOCAL-tick +
    # fail-open (`|| true`) + stderr-sink contract as the vacuum tick above;
    # 24h stamp + per-box lock + backgrounded inside the wrapper. Config:
    # aspirations.yaml § aspirations_eviction (age_days 3, operator-approved).
    bash "$SCRIPT_DIR/aspirations-evict-tick.sh" \
        >>"$CORE_ROOT/logs/iteration-close-stderr.log" 2>&1 || true

    # Orphan-root sweep (plan v1 D5, 2026-05-19) — periodic detector for
    # cruft directories at the wrong root (external-path-resolution drift).
    # Six scans: Mode A duplicates (world-parent), Mode B skeleton dirs
    # (meta-parent), Mode A cross-sibling, Mode D stale-daemon residue
    # (drive-letter / U+F03A names), Mode E (meta/world at PROJECT_ROOT —
    # resolver fallback signature), Mode F (known LLM-scratch leftovers).
    # Cadence: every iteration. Findings route to
    # core/logs/orphan-root-sweep.log (rotated at 100KB → last 200 lines).
    # Advisory only — the sweep always exits 0; findings are surfaced to
    # the operator via the log file, never abort productivity-check.
    {
        orphan_log="$CORE_ROOT/logs/orphan-root-sweep.log"
        if [ -f "$orphan_log" ] && [ "$(wc -c <"$orphan_log" 2>/dev/null || echo 0)" -gt 100000 ]; then
            tail -n 200 "$orphan_log" > "$orphan_log.tmp" 2>/dev/null && mv "$orphan_log.tmp" "$orphan_log"
        fi
        {
            echo "--- $(date +%Y-%m-%dT%H:%M:%S) iter sweep ---"
            bash "$SCRIPT_DIR/orphan-root-sweep.sh" --auto-clean
        } >>"$orphan_log" 2>&1
    } || true

    # iteration-push MOVED to the TOP of this phase (g-115-2915, 2026-07-22):
    # the durability-critical push now runs BEFORE the ~16-script maintenance
    # battery, not after it, so a slow battery can no longer SIGTERM the terminal
    # Bash before the push lands. See the "Durability-critical push FIRST" block
    # right after the productivity-check echo above for the full rationale.

    # Iteration-anchor cleanup (g-115-206 follow-up — bravo session-58 reflection).
    # productivity-check is the canonical terminal phase, so the iteration anchor
    # MUST be gone before the next iteration starts. Previously this rm was
    # LLM-discretionary in the loop digest's Phase 12; an autocompact between
    # productivity-check and the LLM-driven rm would leave the anchor stale, and
    # postcompact-restore would treat the stale anchor as an authoritative
    # "in-flight goal" claim — observed at start of session 59 iter-recovery
    # (g-255-03 anchor showed phase=selected while disk truth showed
    # status=completed). Moving the rm into iteration-close.sh makes anchor
    # cleanup script-enforced. The LLM-driven rm in the loop digest stays as
    # defense-in-depth (idempotent — file already gone is rm's success path).
    # SSOT pattern: matches rb-254 (last_maintain_at), guard-155 (cadence
    # timestamps), and g-248-75 tree-encoding-drift-gate where bash became
    # the single writer/cleaner for previously LLM-discretionary state.
    rm -f "$AGENT_DIR/session/iteration-checkpoint.json"

    # Loop-continuity imperative (rb-496-family, session 58 alpha-stopping incident).
    # productivity-check is the canonical terminal phase of an iteration. The LLM's
    # next tool call MUST be Skill(aspirations) with args='loop' — a terminal Bash
    # echo or text summary here ends the turn without re-entering the loop and
    # kills the autonomous session. Emit an imperative last line so the contract
    # is unambiguous in the turn's tool output stream.
    # NOTE: only emits for the productivity-check phase (not verify/state-update/
    # learning-gate) because those are mid-iteration. This line is the iteration
    # boundary marker.
    # Precheck-gap detector (2026-08-17, alpha assistant review, cc-09): prints
    # whether Phase 0-1 (Skill(aspirations-precheck)) actually ran, measured from
    # the meter's own start/log stamps vs iterations closed. Placed HERE, in the
    # one tool output the return protocol guarantees the loop LLM reads every
    # iteration, because the measured failure shape was a post-autocompact loop
    # that re-entered Skill(aspirations) and ran "the always-run calls" it
    # remembered from the summary — skipping the whole precheck battery for
    # 4+ iterations (cc-04: 19 closes, ~3 prechecks, 16:36→00:28). Fail-open,
    # always exit 0, one line normally; loud banner only when >= 1 iteration
    # closed since the precheck last started.
    bash "$SCRIPT_DIR/precheck-gap-check.sh" 2>/dev/null || true
    echo ""
    echo "[iteration-close] ═══ ITERATION COMPLETE ═══"
    # Deadman's-switch terminal-pair (DEFAULT-ON since Stage 5, 2026-06-23).
    # The terminal is [ScheduleWakeup(sentinel) THEN Skill(aspirations)] — a
    # self-resurrection net behind the unchanged primary Skill re-entry — for
    # EVERY agent by default. Opt out per-agent with
    # `touch agents/<agent>/session/deadman-disabled` (reverts to the bare Skill
    # imperative, byte-identical to the pre-deadman text). Proven safe before
    # default-flip: charlie ran it 24h with 0 deaths / 23-of-23 loop re-entries
    # (deadman-arm-audit ARMED-OK); fail-safe worst case is a slow loop, never a
    # dead one. See aspirations/SKILL.md Return Protocol +
    # core/config/rationale/deadman-switch.md.
    if [ -f "$AGENT_DIR/session/deadman-disabled" ]; then
        echo "[iteration-close] NEXT ACTION REQUIRED: Call Skill(aspirations) with args='loop' as your VERY NEXT tool call."
    else
        echo "[iteration-close] NEXT ACTION REQUIRED (deadman-switch ON): your terminal response MUST be EXACTLY these TWO batched tool calls, in this order — (1) ScheduleWakeup(prompt='<<autonomous-loop-dynamic>>', delaySeconds=600) — the self-resurrection net; this call is MANDATORY, do NOT omit it; THEN (2) Skill(aspirations) with args='loop' — the primary re-entry and the LAST call, which continues the loop NOW. Emitting Skill(aspirations) ALONE keeps THIS iteration alive but leaves the NEXT one unprotected against a silent text-death — so arm the net EVERY iteration. Both calls, every time."
    fi
    echo "[iteration-close] A Bash echo or text summary as the terminal action kills the loop (see .claude/rules/return-protocol.md)."

    # Imperative-fired tracer (g-115-1126): independent observability lane confirming
    # the ITERATION COMPLETE imperative above reached this code path. A future
    # silent-loop-death investigation can grep core/logs/imperative-fires.jsonl to
    # confirm the imperative fired this iteration. Append-only, fail-open (|| true) —
    # never aborts productivity-check. Controlled values (timestamp/literal/agent),
    # so printf-built JSON is injection-safe and avoids a per-iteration python spawn
    # on this hot path.
    printf '{"ts":"%s","script":"iteration-close","phase":"productivity-check","agent":"%s","event":"iteration-complete-imperative"}\n' \
        "$(date +%Y-%m-%dT%H:%M:%S)" "${MIND_AGENT:-unknown}" \
        >> "$CORE_ROOT/logs/imperative-fires.jsonl" 2>>"$CORE_ROOT/logs/iteration-close-stderr.log" || true
}

# --------------------------- phase: recover (g-284-06) ---------------------------
# Detects split-brain between iteration-checkpoint.json intent_state and
# aspirations.jsonl status for the anchored goal. Two recovery paths:
#
#   A. intent=complete + aspirations.status=pending → verify crashed before
#      writing status. Roll back: clear intent_state so the next iteration's
#      verify can run normally. Log a warning so the operator sees the drift.
#
#   B. intent=complete + aspirations.status=completed → verify wrote status
#      but didn't transition intent_state→committed (crash between Step 1/2
#      and Step 4). Forward-recover: clear in_flight (idempotent — may be
#      already clear), transition intent_state to committed.
#
#   C. intent=committed OR intent_state missing → no split-brain; no action.
#
# Invoked by /aspirations Phase -0.5 (loop_state restoration) on iteration
# re-entry, and by /start --recover after a crashed-runner sweep. Idempotent
# and fail-open: any error logs to stderr and exits 0 so a recovery bug
# never blocks the loop.
do_recover() {
    _CURRENT_PHASE="recover"
    local cp_file="$AGENT_DIR/session/iteration-checkpoint.json"
    if [[ ! -f "$cp_file" ]]; then
        # No anchor — nothing to recover. Clean state.
        return 0
    fi
    # Parse iteration-checkpoint.json. Fail-open on missing keys / parse errors.
    local verdict
    verdict="$(CP="$cp_file" python3 -c '
import json, os, sys
try:
    with open(os.environ["CP"], "r", encoding="utf-8") as f:
        d = json.load(f)
except Exception as e:
    print(f"PARSE_ERROR:{e}", file=sys.stderr)
    sys.exit(0)
intent = d.get("intent_state")
gid = d.get("goal_id") or ""
src = d.get("source") or ""
out = d.get("intent_outcome") or ""
if intent != "complete":
    sys.exit(0)  # nothing to recover
print(f"INTENT_COMPLETE|{gid}|{src}|{out}")
' 2>/dev/null)" || true
    [[ -z "$verdict" ]] && return 0
    [[ "$verdict" != INTENT_COMPLETE\|* ]] && return 0

    local _gid _src _out
    IFS='|' read -r _ _gid _src _out <<< "$verdict"
    [[ -z "$_gid" || -z "$_src" ]] && return 0

    # Probe aspirations.jsonl for the anchored goal's current status.
    local _src_file
    if [[ "$_src" == "world" ]]; then
        _src_file="$WORLD_DIR/aspirations.jsonl"
    else
        _src_file="$AGENT_DIR/aspirations.jsonl"
    fi
    local _status
    _status="$(GID="$_gid" SF="$_src_file" python3 -c '
import json, os, sys
try:
    with open(os.environ["SF"], "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            asp = json.loads(line)
            for g in asp.get("goals", []):
                if g.get("id") == os.environ["GID"]:
                    print(g.get("status") or "")
                    sys.exit(0)
except Exception:
    pass
print("")
' 2>/dev/null)" || true

    if [[ "$_status" == "completed" ]]; then
        # Case B — forward-recover. Finish the transition.
        echo "[iteration-close] recover: forward-recovery for $_gid (intent=complete, status=completed, finishing transition to committed)" >&2
        # Scope to $_gid, NOT $GOAL_ID (g-306-161). do_recover derives the goal
        # entirely from the checkpoint and never reads $GOAL_ID — --phase recover
        # is invoked by the loop on re-entry and by /start --recover, neither of
        # which need pass --goal, so $GOAL_ID here is empty or belongs to some
        # other iteration. $_gid is the goal whose status this branch just
        # verified as completed, and it is guaranteed non-empty (the emptiness
        # check ~25 lines above returns early).
        #
        # Forward-recovery finishes ONE goal's transition; it is not a garbage
        # collector. If in_flight has since moved to another goal, that is a
        # live claim from a newer iteration and blanking it is unambiguously
        # wrong — the row we are entitled to clear is $_gid's and only that.
        bash "$SCRIPT_DIR/team-state-clear-in-flight.sh" --agent "$AGENT" --if-goal "$_gid" \
            || echo "[iteration-close] WARN: team-state-clear-in-flight failed during recovery" >&2
        bash "$CORE_ROOT/scripts/loop-state-save.sh" update \
            --set "intent_state=committed" || true
    else
        # Case A — roll back. Aspirations didn't catch the completion.
        # intent_state=rolled_back preserves audit trail and is excluded from
        # do_recover's "INTENT_COMPLETE" trigger so subsequent recover invocations
        # are no-ops. The next iteration's verify runs normally.
        echo "[iteration-close] recover: split-brain detected for $_gid (intent=complete, status=${_status:-unknown}) — rolling back intent" >&2
        bash "$CORE_ROOT/scripts/loop-state-save.sh" update \
            --set "intent_state=rolled_back" || true
    fi
}

# --------------------------- phase-marker helper (Tier 0 telemetry) ---------------------------
# Emits phase_start/phase_end markers to the execution diary so
# phase-cost-report.py can attribute wall-clock cost per phase.
# Plan: ~/.claude/plans/i-had-one-agent-luminous-reddy.md (Tier 0).
# Suppressed failure is intentional — telemetry must never block the hot path;
# a broken diary writer is a dev-env bug, not a phase-execution failure.
_emit_marker() {
    local kind="$1"  # phase-start | phase-end
    local name="$2"
    local args=("$kind" "$name")
    [[ -n "$GOAL_ID" ]] && args+=(--goal "$GOAL_ID")
    bash "$SCRIPT_DIR/execution-diary.sh" "${args[@]}" >/dev/null 2>&1 || true
}

case "$PHASE" in
    verify)              PHASE_NAME="phase-5-verify" ;;
    state-update)        PHASE_NAME="phase-8-state-update" ;;
    learning-gate)       PHASE_NAME="phase-12-learning-gate" ;;
    productivity-check)  PHASE_NAME="phase-12-productivity" ;;
    recover)             PHASE_NAME="phase-recover" ;;
    *) echo "unknown phase: $PHASE" >&2; exit 2 ;;
esac

# --------------------------- dispatch ---------------------------
# trap-on-EXIT ensures phase-end fires even when the phase body errors under set -e.
# g-284-04: ALSO emit recovery instructions on rc != 0, by calling
# _print_recovery_instructions BEFORE the phase-end marker. Both run under
# the same EXIT trap; recovery prints first so the operator sees the
# retry command at the bottom of stderr.
_emit_marker phase-start "$PHASE_NAME"
trap '_rc=$?; _print_recovery_instructions $_rc; _emit_marker phase-end "$PHASE_NAME"; exit $_rc' EXIT
case "$PHASE" in
    verify)              do_verify ;;
    state-update)        do_state_update ;;
    learning-gate)       do_learning_gate ;;
    productivity-check)  do_productivity_check ;;
    recover)             do_recover ;;
esac
