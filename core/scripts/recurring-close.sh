#!/usr/bin/env bash
# recurring-close.sh — atomic close for a recurring goal.
#
# Replaces the manual 5-step workflow:
#   bash iteration-close.sh --phase verify        --goal <id> --status completed --source <s> --outcome <o>
#   bash iteration-close.sh --phase state-update  --goal <id> --source <s> --outcome <o>
#   bash iteration-close.sh --phase learning-gate --goal <id> --source <s> --outcome <o>
#   bash iteration-close.sh --phase productivity-check
#   bash aspirations-update-goal.sh <id> lastAchievedAt "$(date +%Y-%m-%dT%H:%M:%S)"
#
# Now one command:
#   bash recurring-close.sh <goal-id> <outcome-class> [--source world|agent] \
#                           [--summary "..."] [--override-uncommitted "<reason>"]
#     outcome-class ∈ {routine, deep}
#
#   --override-uncommitted forwards to iteration-close.sh do_verify (the only
#   wrapped phase that consumes it). Use when closing an audit-only / inspection
#   goal whose dirty files in the working tree belong to PRIOR iterations, not
#   this goal. Routes through the same world/uncommitted-work-overrides.jsonl
#   audit ledger as direct iteration-close calls. (, 2026-05-08).
#
# Behavior:
#   1. Validate args (MIND_AGENT set, goal-id present, outcome ∈ {routine,deep}).
#   2. Verify the goal IS recurring (refuse otherwise).
#   3. Run the 4 iteration-close phases in order. iteration-close.sh do_verify
#      now routes recurring goals through aspirations-complete-by.sh, which
#      bumps lastAchievedAt + achievedCount + currentStreak/longestStreak.
#   4. Update consecutive_routine on the goal record:
#        outcome=routine → increment by 1
#        outcome=deep    → reset to 0
#   5. If consecutive_routine >= recurring.cargo_cult_threshold (default 3),
#      fire cargo-cult-detector.py to auto-file an Idea goal proposing interval
#      extension or skill change.
#
# Per the plan improve-recurring-goals-kind-yao.md (2026-04-19): this script is
# the canonical close path for recurring goals. Non-recurring goals continue to
# use iteration-close.sh directly.

set -uo pipefail
# NOTE: NOT set -e. Previously -e caused partial-progress failure: if phase N
# exited non-zero, phases N+1..4 never ran AND the consecutive_routine update
# never fired, leaving the goal in a mid-iteration limbo.  (session 53
# iter 13 ) documents this. Each phase call is now wrapped below with
# explicit `|| PHASE_N_RC=$?` capture; a final summary exits with the max rc.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"
# Convert MSYS /c/... → Windows C:/... for inline `python3 -c "open(r'$path', ...)"`.
source "$SCRIPT_DIR/_platform.sh"
# _platform.sh converts REPO_ROOT/PROJECT_ROOT/CORE_ROOT/etc but NOT SCRIPT_DIR,
# which was set via pwd in MSYS form. The inline Python block below exports
# SD="$SCRIPT_DIR" and does `Path(os.environ["SD"]) / "aspirations.py"` — without
# this conversion, Python receives /c/... which becomes C:\c\... on Windows
# subprocess.run ( iter 14  trace). Convert SCRIPT_DIR too.
if [ "${MSYSTEM:-}" != "" ] && command -v cygpath &>/dev/null; then
    SCRIPT_DIR="$(cygpath -m "$SCRIPT_DIR")"
fi
cd "$PROJECT_ROOT"

# Record which phase aborted if something goes catastrophically wrong after the
# graceful-degradation wrapper (e.g., bash parse error, signal). Fail-open —
# trap only logs, never blocks.
FAILED_PHASE=""
trap '[[ -n "$FAILED_PHASE" ]] && echo "[recurring-close] ABORT during: $FAILED_PHASE" >&2 || true' EXIT

if [[ -z "${MIND_AGENT:-}" ]]; then
    echo "recurring-close: MIND_AGENT not set" >&2
    exit 2
fi

# ─────────────────────────── arg parse ───────────────────────────
GOAL_ID=""
OUTCOME=""
SOURCE=""
SUMMARY=""
OVERRIDE_UNCOMMITTED=""
# : § STATE-UPDATE quality flags. iteration-close.sh has parsed these
# since , but this wrapper did not — so `--artifacts-count` et al. exited
# rc=2 "unknown flag" and a recurring deep close could never carry them.
# What was and was NOT broken (measured 2026-07-28, don't re-derive):
#   - `--tree-updated` was ALREADY reachable here, via the  auto-detect
#     inside iteration-close do_state_update (probes iteration-checkpoint.json
#     :selected_at against tree .md mtimes). Since state-update-audit.py:94 sets
#     `measured = bool(tree_updated) or any(...)`, a recurring deep close that
#     edited the tree before this wrapper ran was already measured.
#   - The three VALUE flags had no auto-detect and no parser entry, so they were
#     genuinely unreachable — the close was measured but never enriched.
# Explicit beats inferred either way: passing --tree-updated makes the claim
# auditable (and the  validator still IGNORES it, loudly, when no tree
# edit is detectable since the anchor), so forwarding it is not redundant.
TREE_UPDATED=""
TREE_UPDATED_OVERRIDE=""
ARTIFACTS_COUNT=""
ENCODING_SCORE=""
FINDINGS_COUNT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source)  SOURCE="$2"; shift 2 ;;
        --summary) SUMMARY="$2"; shift 2 ;;
        --outcome) OUTCOME="$2"; shift 2 ;;
        --goal)    GOAL_ID="$2"; shift 2 ;;
        # § STATE-UPDATE quality flags — forwarded to iteration-close.sh
        # --phase state-update ONLY (mirrors iteration-close.sh:179-186).
        # Pass them on THIS call: guard-1235 rules out any post-hoc amend,
        # because re-running state-update to add them re-fires journal-append
        # and iteration-commit. (guard-1235 also cites goals_completed
        # double-counting; that specific arm is stale — loop-state-bump-counters.py
        # :473 is an idempotent per-goal_id no-op within a session. The
        # do-not-re-run conclusion still stands on the other two writers.)
        --tree-updated)          TREE_UPDATED="true"; shift ;;
        --tree-updated-override) TREE_UPDATED_OVERRIDE="true"; shift ;;
        --artifacts-count)       ARTIFACTS_COUNT="$2"; shift 2 ;;
        --encoding-score)        ENCODING_SCORE="$2"; shift 2 ;;
        --findings-count)        FINDINGS_COUNT="$2"; shift 2 ;;
        # Forward --override-uncommitted to iteration-close.sh do_verify
        # (the only wrapped phase that consumes it).  — was missing
        # the twin patch from b13325b which only updated iteration-close.sh.
        --override-uncommitted) OVERRIDE_UNCOMMITTED="$2"; shift 2 ;;
        -*) echo "recurring-close: unknown flag $1" >&2; exit 2 ;;
        *)
            if [[ -z "$GOAL_ID" ]]; then
                GOAL_ID="$1"
            elif [[ -z "$OUTCOME" ]]; then
                OUTCOME="$1"
            else
                echo "recurring-close: too many positional args (got: $1)" >&2
                exit 2
            fi
            shift
            ;;
    esac
done

if [[ -z "$GOAL_ID" || -z "$OUTCOME" || -z "$SOURCE" ]]; then
    echo "Usage: recurring-close.sh <goal-id> <outcome-class> --source <world|agent> [--summary <text>]" >&2
    echo "  deep closes may also pass the § STATE-UPDATE quality flags, forwarded to" >&2
    echo "  --phase state-update: [--tree-updated] [--tree-updated-override]" >&2
    echo "  [--artifacts-count N] [--encoding-score X] [--findings-count N]" >&2
    echo "  Pass them on THIS call — there is no post-hoc amend (guard-1235)." >&2
    echo "  outcome-class ∈ {routine, deep}" >&2
    exit 2
fi

if [[ "$OUTCOME" != "routine" && "$OUTCOME" != "deep" ]]; then
    echo "recurring-close: outcome-class must be 'routine' or 'deep' (got: $OUTCOME)" >&2
    exit 2
fi

# Track caller-supplied outcome for post-mutation comparison + journal entries.
# loop-state-mutate may flip routine→deep via Block A (per-goal streak) or
# Block C (global ratio). We pass the FINAL outcome to the iteration-close
# phases so verify/state-update/learning-gate see the post-flip class.
ORIGINAL_OUTCOME="$OUTCOME"

# ─────────────────────────── recurring check ───────────────────────────
# WORLD_AGENT_ONLY: cross-agent goals run under an MIND_AGENT env override
# ( Option 3), so $AGENT_DIR already points at the owning agent.
if [[ "$SOURCE" == "world" ]]; then
    SRC_FILE="$WORLD_DIR/aspirations.jsonl"
elif [[ "$SOURCE" == "agent" ]]; then
    SRC_FILE="$AGENT_DIR/aspirations.jsonl"
else
    echo "recurring-close: --source must be 'world' or 'agent' (got: $SOURCE)" >&2
    exit 2
fi

if ! GID="$GOAL_ID" SF="$SRC_FILE" python3 - <<'PYEOF'
import json, os, sys
gid = os.environ["GID"]
sf  = os.environ["SF"]
try:
    with open(sf, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            asp = json.loads(line)
            for g in asp.get("goals", []):
                if g.get("id") == gid:
                    sys.exit(0 if g.get("recurring") else 1)
except Exception as e:
    print(f"[recurring-close] source read failed: {e}", file=sys.stderr)
    sys.exit(1)
sys.exit(1)
PYEOF
then
    echo "recurring-close: goal $GOAL_ID not found or not recurring in $SRC_FILE" >&2
    exit 1
fi

# ─────────────────── deliverable-file verification () ───────────────────
# rb-428 class: a recurring skill's deliverable-writing step (e.g. /agent-completion-
# report writing agents/<agent>/COMPLETION-REPORT.md in its Phase 4) is LLM-gated and
# can drift — a close advances lastAchievedAt WITHOUT the named deliverable being
# regenerated (canonical , 2026-07-11: lastAchievedAt bumped, no write touched
# the report). This bash-gated FLAG makes that drift VISIBLE at close time (bash steps
# do not drift, LLM steps do). deliverable-verify.py reads the goal's deliverable_file
# + its CURRENT lastAchievedAt (verify has NOT yet bumped it — this block runs before
# the phases below) and checks mtime > lastAchievedAt. Goals WITHOUT the field verdict
# "skip" → they close exactly as before. FAIL-OPEN + FLAG-ONLY: never blocks a close (a
# false-stale mtime — e.g. an own-cloud stale pull,  — must not gate real
# work). {agent} in the path expands to $MIND_AGENT (a shared recurring goal produces
# a per-agent deliverable — rb-1556). A hard-refuse mode would hook in here.
DELIV_VERDICT="$(python3 "$SCRIPT_DIR/deliverable-verify.py" \
    --goal-id "$GOAL_ID" --source-file "$SRC_FILE" \
    --agent "$MIND_AGENT" --project-root "$PROJECT_ROOT" 2>/dev/null || echo skip)"
case "$DELIV_VERDICT" in
    stale)
        echo "[recurring-close] ⚠ DELIVERABLE NOT REGENERATED: $GOAL_ID is closing (lastAchievedAt about to advance) but its deliverable_file has NOT been modified since the prior close. The skill's deliverable-writing step may have been skipped (rb-428 LLM-abbreviation drift). Close proceeding — this is a FLAG, not a block (g-115-2036)." >&2
        ;;
    missing)
        echo "[recurring-close] ⚠ DELIVERABLE MISSING: $GOAL_ID names a deliverable_file that does not exist on disk — the deliverable was likely never produced. Close proceeding — FLAG only (g-115-2036)." >&2
        ;;
esac

# ─────────────────────────── loop_state mutation (Block A/B/C/D) ───────────────────────────
# Magic Wand #1 (alpha session-60 reflection, 2026-05-07). Single-writer for
# loop_state mutations during the recurring path. Replaces the LLM-side
# manual patch ("after recurring-close, bump goals_completed, update
# routine_streaks, recompute Block A/B/C") that was the silent-corruption
# class flagged in the magic-wand analysis. Bash now owns:
#
#   - routine_streaks[goal.id] (per-goal anti-drift streak + flip-at-threshold)
#   - signals.routine_streak_global, routine_count_total, productive_streak
#   - signals.consecutive_blocked_sleeps (reset on deep)
#   - goals_completed_this_session, productive_goals_this_session
#
# As of , bash ALSO owns evolutions / last_evolution_at /
# alignment_check_at / aspirations_touched (loop-state-bump-counters.py:
# --goal-id increments alignment+touched every close, --reset-alignment at
# aspirations-select, --evolution-fired at aspirations-evolve). The only fields
# the LLM still overlays at LOOP_CONTINUE are the circuit-breaker pair
# (consecutive_goal_failures, last_failed_goal_id; learning-gate) and
# idle_fallback_created (all-blocked) — narrow slot-specific read-merge-writes,
# not a full-slot mirror. Mirrors tree-encoding-drift-gate.sh which is the
# single writer for goals_since_last_tree_update (, rb-428 family).
#
# Stdout is the post-mutation outcome (routine|deep). Block A may flip
# routine → deep when per-goal streak crosses recurring.routine_streak_flip_threshold
# (default 5). Block C may flip again on the global routine ratio. The
# 4 iteration-close phases below use this post-flip outcome so verify /
# state-update / learning-gate see the final classification.
#
# Fail-open: script always exits 0; on error it echoes the caller's claimed
# outcome on stdout. Caller continues with the unflipped outcome — never
# blocks recurring close.
FINAL_OUTCOME="$(bash "$SCRIPT_DIR/recurring-loop-state-mutate.sh" \
    --goal-id "$GOAL_ID" --outcome "$OUTCOME" \
    || echo "$ORIGINAL_OUTCOME")"
# Strip whitespace defensively; one-token outcome on stdout per the contract.
FINAL_OUTCOME="$(echo -n "$FINAL_OUTCOME" | tr -d '[:space:]')"
if [[ "$FINAL_OUTCOME" != "routine" && "$FINAL_OUTCOME" != "deep" ]]; then
    echo "[recurring-close] WARN: loop-state-mutate returned unexpected outcome '$FINAL_OUTCOME' — falling back to caller's claim '$ORIGINAL_OUTCOME'" >&2
    FINAL_OUTCOME="$ORIGINAL_OUTCOME"
fi
if [[ "$FINAL_OUTCOME" != "$ORIGINAL_OUTCOME" ]]; then
    echo "[recurring-close] outcome flipped: $ORIGINAL_OUTCOME → $FINAL_OUTCOME (Block A/C reclassification — see recurring-loop-state-mutate stderr summary)" >&2
fi
OUTCOME="$FINAL_OUTCOME"

# ─────────────────────────── 4 iteration-close phases ───────────────────────────
# verify routes recurring goals through aspirations-complete-by.sh
# (see iteration-close.sh do_verify, IS_RECURRING branch).
#
# Graceful-degradation wrapper (): each phase is run in an isolated
# rc capture so a failure in phase N does NOT abort phases N+1..4. The
# consecutive_routine update at the end still fires so cross-session streaks
# don't silently drift. We track the max rc across phases and exit with that
# at the end (non-zero if ANY phase failed, 0 if all succeeded).
COMMON=(--goal "$GOAL_ID" --source "$SOURCE")
[[ -n "$SUMMARY" ]] && COMMON+=(--summary "$SUMMARY")

MAX_RC=0
PHASE_RESULTS=""

run_phase() {
    local phase_name="$1"; shift
    FAILED_PHASE="$phase_name"  # updated before call; trap reports this if we abort
    local rc=0
    bash "$SCRIPT_DIR/iteration-close.sh" "$@" || rc=$?
    if [[ $rc -ne 0 ]]; then
        echo "[recurring-close] PHASE FAILED: $phase_name (rc=$rc) — continuing to next phase" >&2
        PHASE_RESULTS+="${phase_name}=fail(${rc}) "
        [[ $rc -gt $MAX_RC ]] && MAX_RC=$rc
    else
        PHASE_RESULTS+="${phase_name}=ok "
    fi
    FAILED_PHASE=""  # clear so trap EXIT doesn't misreport a successful phase
}

# Build verify-specific arg array — only this phase consumes
# --override-uncommitted (per iteration-close.sh do_verify gating in b13325b:
# the flag is only forwarded to aspirations-update-goal.sh when status=completed).
# Empty OVERRIDE_UNCOMMITTED preserves normal gate enforcement.
VERIFY_EXTRA=()
[[ -n "$OVERRIDE_UNCOMMITTED" ]] && VERIFY_EXTRA+=(--override-uncommitted "$OVERRIDE_UNCOMMITTED")

# Build state-update-specific arg array — only this phase consumes the four
# § STATE-UPDATE quality flags (). Each is appended only when the
# caller supplied it, so an omitted flag leaves iteration-close.sh at exactly
# its argparse default and the pre-existing behavior is byte-identical.
# Deliberately NOT added to COMMON: verify and learning-gate reject unknown
# flags, so a COMMON-level add would break every recurring close.
STATE_EXTRA=()
[[ "$TREE_UPDATED" == "true" ]]          && STATE_EXTRA+=(--tree-updated)
[[ "$TREE_UPDATED_OVERRIDE" == "true" ]] && STATE_EXTRA+=(--tree-updated-override)
[[ -n "$ARTIFACTS_COUNT" ]]              && STATE_EXTRA+=(--artifacts-count "$ARTIFACTS_COUNT")
[[ -n "$ENCODING_SCORE" ]]               && STATE_EXTRA+=(--encoding-score "$ENCODING_SCORE")
[[ -n "$FINDINGS_COUNT" ]]               && STATE_EXTRA+=(--findings-count "$FINDINGS_COUNT")

run_phase verify           --phase verify           "${COMMON[@]}" --status completed --outcome "$OUTCOME" "${VERIFY_EXTRA[@]}"
run_phase state-update     --phase state-update     "${COMMON[@]}" --outcome "$OUTCOME" "${STATE_EXTRA[@]}"

# ─── force_tree_encoding bypass-consumer (, follow-up rb-911 / ) ───
# tree-encoding-drift-gate.py fires inside iteration-close state-update and writes
# force_tree_encoding="true" when goals_since_last_tree_update crosses threshold.
# Its only consumer is aspirations-state-update SKILL.md Step 8 (LLM-side) — which
# this recurring-close shortcut BYPASSES by design. Without this hook, the sentinel
# accumulates "true" on recurring-only sessions and the per-goal encoding-quality
# override never fires for them. We log + clear so the next non-recurring deep
# goal doesn't inherit a stale signal (or worse, treat it as a fresh trigger).
# force_tree_maintain (paired sentinel) is consumed independently by precheck
# Phase 0-pre; only force_tree_encoding needs this bypass-consumer.
FTE_VAL="$(bash "$SCRIPT_DIR/wm-read.sh" force_tree_encoding 2>/dev/null || echo null)"
# wm-read returns the JSON-encoded value; "true" string vs JSON true. Match both.
if [[ "$FTE_VAL" == '"true"' || "$FTE_VAL" == 'true' ]]; then
    NOW_ISO="$(date +%Y-%m-%dT%H:%M:%S)"
    # Journal a short observation so the bypass is auditable. journal-add.sh
    # takes JSON on stdin (the argv form was silently failing).
    printf '{"date":"%s","entry_type":"observation","goal_id":"%s","content":"recurring-close force_tree_encoding bypass-consume: sentinel was set by tree-encoding-drift-gate during state-update but Step 8 was skipped (recurring-close shortcut). force_tree_maintain backstops global tree maintenance; per-goal encoding override is logged here and sentinel cleared so non-recurring goals do not inherit stale signal."}' \
        "$NOW_ISO" "$GOAL_ID" \
        | bash "$SCRIPT_DIR/journal-add.sh" >/dev/null 2>&1 \
        || echo "[recurring-close] WARN: force_tree_encoding bypass-journal-append failed (non-fatal)" >&2
    # Clear sentinel — same semantics as aspirations-state-update SKILL.md Step 8.
    echo '"false"' | bash "$SCRIPT_DIR/wm-set.sh" force_tree_encoding >/dev/null 2>&1 \
        || echo "[recurring-close] WARN: force_tree_encoding clear failed (non-fatal — next iteration will re-clear)" >&2
    echo "[recurring-close] force_tree_encoding bypass-consumed for recurring goal $GOAL_ID (cleared; per-goal encoding override n/a on recurring path)" >&2
fi
# ─── end force_tree_encoding bypass-consumer ───

run_phase learning-gate    --phase learning-gate    "${COMMON[@]}" --outcome "$OUTCOME"
run_phase productivity     --phase productivity-check

echo "[recurring-close] phases: $PHASE_RESULTS" >&2

# ─────────────────────────── consecutive_routine + cargo-cult ───────────────────────────
# OUTCOME_ORIGIN (): derived from caller-side comparison of the
# LLM's claimed outcome (ORIGINAL_OUTCOME) vs the post-mutation outcome
# (FINAL_OUTCOME, now in OUTCOME). When the mutate script's Block A or
# Block C force-flips routine→deep for anti-drift, the resulting "deep"
# outcome is a SESSION-level pattern-matching artifact, NOT a goal-level
# signal that genuine work emerged. Persisting this distinction lets
# auto-contract (cargo-cult-detector.py --contract-mode) count only
# GENUINE deeps in consecutive_deep, breaking the runaway feedback loop
# documented in  /  (interval 1.0h → 0.33h via three
# consecutive forced flips).
#
# origin = "forced-flip" when ORIGINAL=routine AND FINAL=deep
#        = "genuine" otherwise (caller claimed deep, or stayed routine)
#
# Effect on consecutive_deep:
#   genuine deep    → consecutive_deep += 1 (drives auto-contract as before)
#   forced-flip deep → consecutive_deep UNCHANGED (forced flips don't count)
#   routine          → consecutive_deep = 0 (reset on streak break)
if [[ "$OUTCOME" == "deep" && "$ORIGINAL_OUTCOME" == "routine" ]]; then
    OUTCOME_ORIGIN="forced-flip"
else
    OUTCOME_ORIGIN="genuine"
fi

NOW="$(date +%Y-%m-%dT%H:%M:%S)"
GID="$GOAL_ID" SF="$SRC_FILE" OUTCOME="$OUTCOME" OUTCOME_ORIGIN="$OUTCOME_ORIGIN" SRC_FLAG="$SOURCE" SD="$SCRIPT_DIR" NOW="$NOW" python3 - <<'PYEOF'
import json, os, subprocess, sys
from pathlib import Path
import yaml

gid     = os.environ["GID"]
sf      = os.environ["SF"]
outcome = os.environ["OUTCOME"]
outcome_origin = os.environ["OUTCOME_ORIGIN"]
src     = os.environ["SRC_FLAG"]
sd      = Path(os.environ["SD"])

# Read current consecutive_routine AND consecutive_deep counters. Both default
# to 0 for legacy goals. Errors propagate.
# : ALSO read the lifetime substantive-hit tally. substantive_hits is
# the numerator, substantive_runs the denominator. Both default to 0 for legacy
# goals — substantive_runs is counted from field-introduction (NOT seeded from
# achievedCount) so the lifetime rate the chronic-low detector keys on is never
# poisoned by pre-tracking run history.
current = 0
current_deep = 0
current_sub_hits = 0
current_sub_runs = 0
with open(sf, "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        asp = json.loads(line)
        for g in asp.get("goals", []):
            if g.get("id") == gid:
                current = int(g.get("consecutive_routine", 0))
                current_deep = int(g.get("consecutive_deep", 0))
                current_sub_hits = int(g.get("substantive_hits", 0))
                current_sub_runs = int(g.get("substantive_runs", 0))
                break

new_val = current + 1 if outcome == "routine" else 0
# consecutive_deep is the mirror counter that drives the auto-contract path.
# Only GENUINE deep outcomes advance it; anti-drift forced-flips (Block A/C)
# leave it unchanged so the contract trigger reflects real signal, not the
# session-level pattern-matching defense.
# Pre- logic: new_deep = current_deep + 1 if outcome == "deep" else 0
# Post-fix logic: forced-flips are pinned in place; only routine resets.
if outcome == "deep" and outcome_origin == "genuine":
    new_deep = current_deep + 1
elif outcome == "deep":  # outcome_origin == "forced-flip"
    new_deep = current_deep  # unchanged — forced flips don't count toward contract
else:  # routine
    new_deep = 0

# : lifetime substantive-hit tally. substantive_runs is the denominator
# (advances on EVERY close); substantive_hits is the numerator (GENUINE deep
# only — a forced-flip is the anti-drift mechanism, not real substantive output,
# so it must not inflate the lifetime rate chronic-low detection reads). Mirrors
# the consecutive_deep genuine/forced split above.
new_sub_runs = current_sub_runs + 1
if outcome == "deep" and outcome_origin == "genuine":
    new_sub_hits = current_sub_hits + 1
else:
    new_sub_hits = current_sub_hits

upd = subprocess.run(
    [sys.executable, str(sd / "aspirations.py"),
     "--source", src, "update-goal", gid, "consecutive_routine", str(new_val)],
    capture_output=True, text=True, encoding="utf-8",
)
if upd.returncode != 0:
    print(f"[recurring-close] update consecutive_routine failed: {upd.stderr}", file=sys.stderr)
    sys.exit(1)

upd_d = subprocess.run(
    [sys.executable, str(sd / "aspirations.py"),
     "--source", src, "update-goal", gid, "consecutive_deep", str(new_deep)],
    capture_output=True, text=True, encoding="utf-8",
)
if upd_d.returncode != 0:
    print(f"[recurring-close] update consecutive_deep failed: {upd_d.stderr}", file=sys.stderr)
    # Non-fatal — auto-contract is a value-add, not load-bearing.

# Persist last_outcome_origin on the goal for audit / observability ().
# Overwritten each close. Lets future debugging trace WHY consecutive_deep
# stalled or advanced without diving into stderr logs from prior sessions.
# Only meaningful when outcome=deep; for routine closes we still write
# "genuine" (the natural state — no flip happened, the LLM's routine claim
# was honored).
upd_o = subprocess.run(
    [sys.executable, str(sd / "aspirations.py"),
     "--source", src, "update-goal", gid, "last_outcome_origin", outcome_origin],
    capture_output=True, text=True, encoding="utf-8",
)
if upd_o.returncode != 0:
    print(f"[recurring-close] update last_outcome_origin failed: {upd_o.stderr}", file=sys.stderr)
    # Non-fatal — purely observability field.

# : persist the lifetime substantive-hit tally — the WRITER half of the
# chronic-low detector (reader is cargo-cult-detector.py _score_recurring /
# cmd_audit_all). Writer+reader ship together; reader-without-writer is the
# retired-Path-A trap. All writes are NON-FATAL: the tally is detection
# value-add, never load-bearing for the close.
upd_sr = subprocess.run(
    [sys.executable, str(sd / "aspirations.py"),
     "--source", src, "update-goal", gid, "substantive_runs", str(new_sub_runs)],
    capture_output=True, text=True, encoding="utf-8",
)
if upd_sr.returncode != 0:
    print(f"[recurring-close] update substantive_runs failed: {upd_sr.stderr}", file=sys.stderr)
if new_sub_hits != current_sub_hits:
    # A GENUINE deep advanced the tally — write the count + stamp the last-catch
    # timestamp (R1 'last catch'). NOW is local system time, passed via env.
    upd_sh = subprocess.run(
        [sys.executable, str(sd / "aspirations.py"),
         "--source", src, "update-goal", gid, "substantive_hits", str(new_sub_hits)],
        capture_output=True, text=True, encoding="utf-8",
    )
    if upd_sh.returncode != 0:
        print(f"[recurring-close] update substantive_hits failed: {upd_sh.stderr}", file=sys.stderr)
    upd_lsa = subprocess.run(
        [sys.executable, str(sd / "aspirations.py"),
         "--source", src, "update-goal", gid, "last_substantive_at", os.environ["NOW"]],
        capture_output=True, text=True, encoding="utf-8",
    )
    if upd_lsa.returncode != 0:
        print(f"[recurring-close] update last_substantive_at failed: {upd_lsa.stderr}", file=sys.stderr)

# Surface the decision so the loop's stderr stream captures it. Mirrors the
# Block A/C flip notification line above (line ~192).
if outcome == "deep":
    print(
        f"[recurring-close] {gid}: outcome_origin={outcome_origin} "
        f"consecutive_deep={current_deep}→{new_deep} "
        f"(genuine-deeps advance counter; forced-flips pin it; routine resets)",
        file=sys.stderr,
    )

sys.path.insert(0, str(sd))
import _paths
with open(_paths.CONFIG_DIR / "aspirations.yaml", encoding="utf-8") as cf:
    cfg = yaml.safe_load(cf) or {}
threshold = int(cfg["recurring"]["cargo_cult_threshold"])

print(f"[recurring-close] {gid}: outcome={outcome} consecutive_routine={new_val} threshold={threshold}")

if outcome == "routine" and new_val >= threshold:
    # Change 3: batch-mode routing. When cargo_cult.batch_audit_dedupe_hours
    # is set AND no batch Idea landed in the last N hours, run --audit-all
    # instead of the per-goal path. One sweep over ALL recurring goals
    # replaces N individual filings — collapses symptom-chasing.
    #
    # Dedupe check: scan both aspiration sources for a pending/in-progress
    # Idea with title exactly "Batch: Calibrate recurring intervals" whose
    # created_at is within the window. If found, suppress BOTH paths —
    # the outstanding batch Idea covers this goal's signal.
    batch_hours = float((cfg.get("cargo_cult") or {}).get("batch_audit_dedupe_hours", 0))

    def _recent_batch_idea() -> bool:
        if batch_hours <= 0:
            return False
        import datetime as _dt
        now = _dt.datetime.now()
        cutoff = now - _dt.timedelta(hours=batch_hours)
        # MIND_AGENT is guaranteed non-empty by the outer bash check at the
        # top of this script — no need to re-probe it here.
        agent = os.environ["MIND_AGENT"]
        for world_or_agent in ("world", "agent"):
            try:
                if world_or_agent == "world":
                    p = _paths.WORLD_DIR / "aspirations.jsonl"
                else:
                    p = _paths.agent_dir(agent) / "aspirations.jsonl"
                if not p.exists():
                    continue
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        a = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    for g in a.get("goals", []):
                        if g.get("title") != "Batch: Calibrate recurring intervals":
                            continue
                        if g.get("status") in ("completed", "skipped", "expired"):
                            continue
                        ca = g.get("created_at")
                        if ca:
                            try:
                                if _dt.datetime.fromisoformat(str(ca)) >= cutoff:
                                    return True
                            except (ValueError, TypeError):
                                pass
            except Exception:
                continue
        return False

    def _reset_counter():
        r = subprocess.run(
            [sys.executable, str(sd / "aspirations.py"),
             "--source", src, "update-goal",
             gid, "consecutive_routine", "0"],
            capture_output=True, text=True, encoding="utf-8",
        )
        if r.returncode != 0:
            # Non-fatal: next cycle's dedupe (or audit) catches repeat firings.
            # Surface stderr so the failure isn't invisible.
            print(f"[recurring-close] consecutive_routine reset failed "
                  f"(rc={r.returncode}): {r.stderr.strip()}", file=sys.stderr)

    if batch_hours > 0 and _recent_batch_idea():
        # Recent batch Idea is still outstanding — do not re-file. Reset the
        # consecutive_routine counter so this goal doesn't re-trigger the
        # check next cycle before the batch is reviewed.
        print(f"[recurring-close] cargo-cult dedupe HIT — batch Idea already "
              f"outstanding within {batch_hours:g}h; skipping filing for {gid}")
        _reset_counter()
    elif batch_hours > 0:
        # No recent batch Idea → run the batch audit instead of per-goal.
        det = subprocess.run(
            [sys.executable, str(sd / "cargo-cult-detector.py"),
             "--audit-all"],
            capture_output=True, text=True, encoding="utf-8",
        )
        if det.returncode == 0:
            if det.stdout.strip():
                print(det.stdout.strip())
            # Still reset THIS goal's counter — the batch covers all.
            _reset_counter()
        else:
            print(f"[recurring-close] cargo-cult-detector --audit-all failed: "
                  f"{det.stderr}", file=sys.stderr)
    else:
        # Legacy per-goal path (batch_audit_dedupe_hours=0 or missing).
        det = subprocess.run(
            [sys.executable, str(sd / "cargo-cult-detector.py"), gid, "--source", src],
            capture_output=True, text=True, encoding="utf-8",
        )
        if det.returncode == 0:
            if det.stdout.strip():
                print(det.stdout.strip())
        else:
            print(f"[recurring-close] cargo-cult-detector failed: {det.stderr}",
                  file=sys.stderr)

# Auto-contract path: when consecutive_deep crosses threshold, the recurring
# goal's cadence is too loose — shrink it. Symmetric to the cargo-cult
# auto-extend above. Origin: LifingPolls plan item 4 (2026-05-08).
contract_threshold = int(cfg["recurring"].get(
    "deep_streak_contract_threshold", 3))
if outcome == "deep" and new_deep >= contract_threshold:
    det = subprocess.run(
        [sys.executable, str(sd / "cargo-cult-detector.py"), gid,
         "--source", src, "--contract-mode"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if det.returncode == 0:
        if det.stdout.strip():
            print(det.stdout.strip())
    else:
        print(f"[recurring-close] cargo-cult-detector --contract-mode "
              f"failed: {det.stderr}", file=sys.stderr)
PYEOF
PY_RC=$?

# ─────────────────────────── Phase 4.25 enforcement () ───────────────────────────
# Per-goal experience-staleness check for deep-outcome recurring goals.
# Closes the LLM-residue gap diagnosed in : Phase 4.25 (experience
# archival) is LLM-driven and gets forgotten under the recurring-close
# shortcut. The existing rb-428 backstop (experience-staleness-check.sh)
# fires at 12h global staleness — too lax for high-value events.
#
# This block fires for deep outcomes only. If alpha/experience.jsonl has
# no entry matching this goal_id within the last 30 minutes, set
# force_experience_archival WM sentinel. Phase 0-pre2 of next iteration's
# precheck (aspirations-precheck) consumes the sentinel and forces the LLM
# to retro-compose. Per-goal granularity, event-driven (vs time-driven).
#
# Fail-open: any exception → silently exit (no sentinel set). The 12h
# global backstop catches anything this misses.
GID="$GOAL_ID" OUTCOME="$OUTCOME" ORIGINAL_OUTCOME="$ORIGINAL_OUTCOME" PR="$PROJECT_ROOT" SD="$SCRIPT_DIR" python3 - <<'PYEOF' || true
import json, os, sys, subprocess
from datetime import datetime, timedelta
from pathlib import Path

outcome = os.environ.get("OUTCOME", "")
original_outcome = os.environ.get("ORIGINAL_OUTCOME", "")
gid = os.environ.get("GID", "")
pr = os.environ.get("PR", "")
sd = os.environ.get("SD", "")
if outcome != "deep" or not gid or not sd:
    sys.exit(0)

# : suppress canary on forced-flip + empty signal.
# When recurring-loop-state-mutate.py flips routine→deep via Block A/C
# (per-goal streak >= flip_threshold, global routine_streak_global >=
# global_ceiling (default 5; was 8 before 2026-05-12), or session ratio
# >80%), the canary's premise "deep close implies
# substantive work missing from experience.jsonl" is wrong. The flip
# itself is the documentation; placeholder entries that just record
# "the flip happened" duplicate routine_streaks / signals / journal.
# Skip the canary when ALL of:
#   - original_outcome == "routine" AND outcome == "deep" (forced flip)
#   - encoding_queue is empty (no encoding work signals depth — also
#     proxies for hypothesis-resolution and pipeline-unreflected items
#     because those route through encoding_queue upstream)
#   - sensory_buffer is empty (no perception backlog signals depth)
# Fail-closed on signal-check errors: when any wm-read fails, the
# canary RUNS (existing default). Suppression requires positive
# evidence that signals are empty.
if original_outcome == "routine" and outcome == "deep":
    # Use wm-read.sh (daemon-aware wrapper) — wm.py CLI no longer has a
    # `read` subcommand (deleted in the 2026-05-14 daemon-only migration).
    # Reading via the wrapper routes through /v1/wm/read so the canary
    # actually sees the live WM. Fail-closed: any non-zero subprocess rc
    # or JSON parse error sets the value to None and the canary RUNS.
    wm_read = Path(sd) / "wm-read.sh" if sd else None
    suppress = False
    if wm_read and wm_read.exists():
        def _read_slot(name):
            try:
                r = subprocess.run(
                    ["bash", str(wm_read), name, "--json"],
                    capture_output=True, text=True, timeout=5,
                )
                if r.returncode != 0:
                    return None  # fail-closed
                raw = (r.stdout or "").strip()
                if not raw or raw == "null":
                    return []
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return None  # fail-closed
            except Exception:
                return None  # fail-closed
        eq = _read_slot("encoding_queue")
        sb = _read_slot("sensory_buffer")
        if (
            isinstance(eq, list) and len(eq) == 0
            and isinstance(sb, list) and len(sb) == 0
        ):
            suppress = True
    if suppress:
        print(
            f"[recurring-close] g-115-634: forced-flip "
            f"{original_outcome}->{outcome} with empty encoding_queue + "
            f"sensory_buffer — suppressing canary for {gid}",
            file=sys.stderr,
        )
        sys.exit(0)

    # : substantive-artifact probe (second-layer suppression).
    # When the empty-WM check () did not fire (some buffer was
    # non-empty from earlier work this session), the canary still fires
    # unnecessarily if the just-closed iteration produced no new artifact.
    # Probe four artifact types in a tight time window (default 90s back):
    # tree node .md write, new goal filed, non-status board post,
    # pipeline-meta mtime change. ALL FOUR negative → suppress.
    # Config knobs (core/config/aspirations.yaml recurring:):
    #   cargo_cult_suppress_no_artifact (default true) — gate this layer
    #   cargo_cult_artifact_window_seconds (default 90) — probe window
    # Fail-open: any probe error → sentinel fires as normal (the 12h
    # global staleness backstop catches anything the probe misses).
    try:
        import yaml as _yaml_g_115_1089
        sys.path.insert(0, sd)
        import _paths as _paths_g_115_1089
        _cfg_path = _paths_g_115_1089.CONFIG_DIR / "aspirations.yaml"
        _recurring_cfg = {}
        try:
            with open(_cfg_path, encoding="utf-8") as _cf:
                _recurring_cfg = (_yaml_g_115_1089.safe_load(_cf) or {}).get("recurring", {}) or {}
        except Exception:
            pass
        _cfg_enabled = bool(_recurring_cfg.get("cargo_cult_suppress_no_artifact", True))
        _window_seconds = int(_recurring_cfg.get("cargo_cult_artifact_window_seconds", 90))
        if _cfg_enabled:
            _now_ts = datetime.now()
            _cutoff = _now_ts - timedelta(seconds=_window_seconds)
            _world_dir_probe = _paths_g_115_1089.WORLD_DIR
            _agent_name = os.environ.get("MIND_AGENT", "")
            _agent_dir_probe = _paths_g_115_1089.AGENT_DIR
            _substantive = False
            _reason = "no substantive artifact"
            # 1. Tree node .md file edited in window
            _tree_dir = _world_dir_probe / "knowledge" / "tree"
            if _tree_dir.exists():
                for _p in _tree_dir.rglob("*.md"):
                    try:
                        if datetime.fromtimestamp(_p.stat().st_mtime) > _cutoff:
                            _substantive = True
                            _reason = f"tree-md:{_p.name}"
                            break
                    except Exception:
                        continue
            # 2. New goal in world + agent aspirations
            if not _substantive:
                for _asp_path in (_world_dir_probe / "aspirations.jsonl",
                                  _agent_dir_probe / "aspirations.jsonl"):
                    if not _asp_path.exists():
                        continue
                    try:
                        for _line in _asp_path.read_text(encoding="utf-8").splitlines()[-200:]:
                            _line = _line.strip()
                            if not _line:
                                continue
                            try:
                                _rec = json.loads(_line)
                            except json.JSONDecodeError:
                                continue
                            for _g in (_rec.get("goals") or []):
                                _ts = _g.get("created_at") or _rec.get("updated_at")
                                if not _ts:
                                    continue
                                try:
                                    if datetime.fromisoformat(_ts) > _cutoff:
                                        _substantive = True
                                        _reason = f"goal:{_g.get('id')}"
                                        break
                                except ValueError:
                                    continue
                            if _substantive:
                                break
                    except Exception:
                        continue
                    if _substantive:
                        break
            # 3. Non-status board post by this agent
            if not _substantive:
                _board_dir = _world_dir_probe / "board"
                if _board_dir.exists():
                    for _jl in _board_dir.glob("*.jsonl"):
                        try:
                            for _line in _jl.read_text(encoding="utf-8").splitlines()[-50:]:
                                _line = _line.strip()
                                if not _line:
                                    continue
                                try:
                                    _m = json.loads(_line)
                                except json.JSONDecodeError:
                                    continue
                                if _m.get("author") != _agent_name:
                                    continue
                                if _m.get("type") == "status":
                                    continue
                                _ts = _m.get("timestamp", "")
                                try:
                                    if datetime.fromisoformat(_ts) > _cutoff:
                                        _substantive = True
                                        _reason = f"board:{_m.get('id')}"
                                        break
                                except ValueError:
                                    continue
                            if _substantive:
                                break
                        except Exception:
                            continue
            # 4. Pipeline state change (cheap proxy: pipeline.jsonl OR pipeline-meta.json mtime).
            #    rb-1203 / : pipeline-add.sh writes pipeline.jsonl directly without
            #    touching pipeline-meta.json mtime (only pipeline-recompute-meta.sh refreshes
            #    meta), so checking meta alone missed in-window hypothesis filings and the
            #    canary suppressed genuinely-productive forced-flips. Probe pipeline.jsonl first.
            if not _substantive:
                for _pf, _rsn in (
                    (_world_dir_probe / "pipeline.jsonl", "pipeline-jsonl-updated"),
                    (_world_dir_probe / "pipeline-meta.json", "pipeline-meta-updated"),
                ):
                    try:
                        if _pf.exists() and datetime.fromtimestamp(_pf.stat().st_mtime) > _cutoff:
                            _substantive = True
                            _reason = _rsn
                            break
                    except Exception:
                        pass
            if not _substantive:
                print(
                    f"[recurring-close] g-115-1089: forced-flip "
                    f"{original_outcome}->{outcome} with {_reason} — "
                    f"suppressing canary for {gid}",
                    file=sys.stderr,
                )
                sys.exit(0)
    except Exception as _e:
        # Fail-open: artifact probe error must NOT block sentinel writing.
        print(
            f"[recurring-close] g-115-1089: artifact probe error — {_e}",
            file=sys.stderr,
        )

# Project root via _paths
sys.path.insert(0, sd)
try:
    import _paths
except Exception:
    sys.exit(0)

agent_dir = _paths.AGENT_DIR
if not agent_dir:
    sys.exit(0)

# The per-goal check itself now lives in core/scripts/per-goal-experience-check.py
# (), so the NON-recurring close path can run the identical logic —
# iteration-close.sh do_state_update calls the same helper. Everything above this
# line is recurring-ONLY (the Block A/C forced-flip suppressions need
# ORIGINAL_OUTCOME, which only this path has) and deliberately stays here.
#
# guard-2015: this file keeps NO copy of the extracted logic. A left-behind fork
# stops receiving the shared helper's later hardening, and readers who follow the
# helper's "extracted from recurring-close.sh" header would land on the stale one.
#
# 'trigger' says postflip-deep to make explicit that this fires on the
# POST-Block-A/C-flip outcome, not the caller's original CLI outcome;
# 'original_outcome' carries the caller's CLI arg so consumers can distinguish
# 'caller asked for deep' from 'system flipped routine->deep' ( /
# ). Both travel to the helper as flags — the payload shape Phase
# 0-pre2 consumes is unchanged.
#
# Degradation is VISIBLE, not swallowed. The helper always exits 0 and reports
# its own skips on stderr; this wrapper covers the case the helper cannot report
# on — never being reached at all (missing file, interpreter failure, timeout).
_helper = Path(sd) / "per-goal-experience-check.py"
if not _helper.exists():
    print(f"[recurring-close] WARN: per-goal experience check missing at {_helper} — Phase 4.25 enforcement SKIPPED for {gid}", file=sys.stderr)
else:
    try:
        _r = subprocess.run(
            [
                sys.executable, str(_helper),
                "--goal-id", gid,
                "--trigger", "recurring-close-postflip-deep-no-recent-entry",
                "--original-outcome", original_outcome,
            ],
            capture_output=True, text=True, timeout=30,
        )
        if _r.stderr:
            sys.stderr.write(_r.stderr)
        if _r.returncode != 0:
            print(f"[recurring-close] WARN: per-goal experience check exited rc={_r.returncode} for {gid}", file=sys.stderr)
    except Exception as _e:
        print(f"[recurring-close] WARN: per-goal experience check failed ({_e}) — Phase 4.25 enforcement SKIPPED for {gid}", file=sys.stderr)
PYEOF

# Streak-break reflector: convert any signals emitted by complete_by into
# Investigate goals on the parent aspiration. Fail-silent — a reflector
# error must not propagate to MAX_RC because the recurring close itself
# succeeded. Origin: LifingPolls plan item 1 (2026-05-08).
SBR_OUT=$(mktemp)
python3 "$SCRIPT_DIR/streak-break-reflector.py" --agent "$MIND_AGENT" \
    >"$SBR_OUT" 2>&1 || true
if [ -s "$SBR_OUT" ]; then
    cat "$SBR_OUT"
fi
rm -f "$SBR_OUT"

# Propagate phase failures + consecutive_routine-update failure to caller.
# MAX_RC reflects the worst phase outcome; PY_RC reflects the inline Python
# block (consecutive_routine update or cargo-cult-detector). Either non-zero
# is a partial failure the caller should see.
if [[ $PY_RC -gt $MAX_RC ]]; then
    MAX_RC=$PY_RC
fi

# Loop-continuity imperative (session 58 alpha-stopping incident). recurring-close
# calls iteration-close --phase productivity-check internally (which also emits an
# imperative), but the cargo-cult-detector Python block above runs AFTER that and
# becomes the TRUE terminal output seen by the LLM. Re-emit the imperative here so
# the last lines in this tool call's stdout are the contract, not detector status.
#
# Outcome-aware terminal imperative (, Option C from  investigation).
# recurring-close.sh wraps Phase 5/8/12 (verify/state-update/learning-gate +
# productivity-check) but does NOT wrap Phase 6 (aspirations-spark). The loop
# orchestrator's skip rule says spark fires whenever outcome_class == deep.
# Without an outcome-aware imperative here, deep-outcome recurring closures
# silently bypass Phase 6 — the same docs-vs-impl drift class universal RB
# "Docs-vs-impl drift in framework shortcut wrappers" was filed against.
# Sentinel-WM-slot transport for Phase 6 spark imperative ().
# When recurring-close.sh's wall-clock exceeds the Bash 2-minute timeout the
# call backgrounds, the harness fires the stop hook before bg completes, and
# the LLM re-enters /aspirations loop never seeing the stdout imperative
# below. Phase 6 spark was silently bypassed on deep recurring closes
# (observed 2/2:  bfzr7dvyk +  bo42a8rld).
#
# Write the OUTCOME (post Block A/C flip), goal_id, source, summary, and a
# 60-min expires_at into wm.pending_phase_6_spark. The aspirations
# orchestrator's Phase -0.5c.X consumes this slot on next-iteration entry
# BEFORE precheck — if outcome=deep and not expired, it fires
# Skill(aspirations-spark); if outcome=routine or expired, clears silently.
# Stdout imperative below is preserved as backward-compatible signal for
# non-bg cases. The sentinel is the authoritative transport.
# Fail-open: errors echo to stderr and do not change MAX_RC.
EXPIRES_AT="$(py -3 -c "from datetime import datetime, timedelta; print((datetime.now() + timedelta(minutes=60)).isoformat(timespec='seconds'))" 2>/dev/null || true)"
# set_at = sentinel creation time (now). Consumed by Phase -0.5c.2's dedup
# (spark-fire-dedup.py check --sentinel-set-at): a spark recorded at/after this
# set_at fired in response to THIS close (skip the re-fire); one from a prior
# close fired before it (fire). Additive field — older consumers ignore it; the
# new consumer prefers it over the time-window heuristic ( / rb-1674).
SET_AT="$(py -3 -c "from datetime import datetime; print(datetime.now().isoformat(timespec='seconds'))" 2>/dev/null || true)"
if [[ -n "$EXPIRES_AT" ]]; then
    SENTINEL_PAYLOAD="$(GID="$GOAL_ID" OUT="$OUTCOME" SRC="$SOURCE" SUM="$SUMMARY" EXP="$EXPIRES_AT" SETAT="$SET_AT" py -3 -c "
import json, os
print(json.dumps({
    'goal_id':    os.environ['GID'],
    'outcome':    os.environ['OUT'],
    'source':     os.environ['SRC'],
    'summary':    os.environ.get('SUM',''),
    'expires_at': os.environ['EXP'],
    'set_at':     os.environ.get('SETAT',''),
}))
" 2>/dev/null || true)"
    if [[ -n "$SENTINEL_PAYLOAD" ]]; then
        echo "$SENTINEL_PAYLOAD" | bash "$SCRIPT_DIR/wm-set.sh" pending_phase_6_spark >/dev/null 2>&1 \
            || echo "[recurring-close] WARN: pending_phase_6_spark sentinel write failed (non-fatal — stdout imperative remains as fallback)" >&2
    else
        echo "[recurring-close] WARN: could not build pending_phase_6_spark payload (non-fatal)" >&2
    fi
else
    echo "[recurring-close] WARN: could not compute expires_at for pending_phase_6_spark (non-fatal)" >&2
fi

echo ""
echo "[recurring-close] ═══ ITERATION COMPLETE ═══"
# Deadman's-switch terminal-pair (DEFAULT-ON since Stage 5, 2026-06-23). Mirror
# of iteration-close.sh. ON for every agent by default; opt out per-agent with
# `touch agents/<agent>/session/deadman-disabled` (reverts to byte-identical
# pre-deadman imperative). See aspirations/SKILL.md Return Protocol +
# core/config/rationale/deadman-switch.md.
if [ -f "$AGENT_DIR/session/deadman-disabled" ]; then
    if [[ "$OUTCOME" == "deep" ]]; then
        echo "[recurring-close] OUTCOME=deep — NEXT ACTION REQUIRED: Call Skill(aspirations-spark) FIRST (Phase 6 fires on deep; NOT wrapped by recurring-close.sh), THEN Skill(aspirations) with args='loop'."
    else
        echo "[recurring-close] OUTCOME=routine — NEXT ACTION REQUIRED: Call Skill(aspirations) with args='loop' as your VERY NEXT tool call."
    fi
else
    _dm_pair="emit the deadman pair as the loop re-entry (BOTH calls MANDATORY, in this order) — (1) ScheduleWakeup(prompt='<<autonomous-loop-dynamic>>', delaySeconds=600), the self-resurrection net, do NOT omit it; THEN (2) Skill(aspirations) with args='loop' (the LAST call). Skill ALONE keeps THIS iteration alive but leaves the NEXT unprotected against a silent text-death — arm the net EVERY iteration"
    if [[ "$OUTCOME" == "deep" ]]; then
        echo "[recurring-close] OUTCOME=deep (deadman-switch ON) — NEXT ACTION REQUIRED: Call Skill(aspirations-spark) FIRST (Phase 6 fires on deep; NOT wrapped by recurring-close.sh), THEN ${_dm_pair}."
    else
        echo "[recurring-close] OUTCOME=routine (deadman-switch ON) — NEXT ACTION REQUIRED: ${_dm_pair}."
    fi
fi
echo "[recurring-close] A Bash echo or text summary as the terminal action kills the loop (see .claude/rules/return-protocol.md)."

# Imperative-fired tracer (): independent observability lane confirming
# the ITERATION COMPLETE imperative above reached this code path (outcome-aware).
# Mirror of the iteration-close.sh tracer; carries goal_id + outcome for the
# recurring path. Append-only, fail-open (|| true) — runs BEFORE `exit $MAX_RC`
# so it never affects the close exit code. Controlled values → printf JSON is
# injection-safe and avoids a python spawn on this hot path.
printf '{"ts":"%s","script":"recurring-close","goal_id":"%s","outcome":"%s","agent":"%s","event":"iteration-complete-imperative"}\n' \
    "$(date +%Y-%m-%dT%H:%M:%S)" "${GOAL_ID:-unknown}" "${OUTCOME:-unknown}" "${MIND_AGENT:-unknown}" \
    >> "$CORE_ROOT/logs/imperative-fires.jsonl" 2>>"$CORE_ROOT/logs/iteration-close-stderr.log" || true

exit $MAX_RC
