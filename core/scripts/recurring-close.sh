#!/usr/bin/env bash
# recurring-close.sh — atomic close for a recurring goal.
#
# Replaces the manual 5-step workflow:
#   bash iteration-close.sh --phase verify        --goal <id> --status completed --source <s>
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

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source)  SOURCE="$2"; shift 2 ;;
        --summary) SUMMARY="$2"; shift 2 ;;
        --outcome) OUTCOME="$2"; shift 2 ;;
        --goal)    GOAL_ID="$2"; shift 2 ;;
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
# LLM still owns the LLM-only fields at LOOP_CONTINUE (evolutions, alignment
# checks, aspirations_touched, consecutive_goal_failures, last_failed_goal_id,
# idle_fallback_created). The LLM read-merge-write pattern at LOOP_CONTINUE
# picks up bash mutations because Phase -0.5 of the next iteration restores
# loop_state from WM. Mirrors tree-encoding-drift-gate.sh which is the
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

run_phase verify           --phase verify           "${COMMON[@]}" --status completed "${VERIFY_EXTRA[@]}"
run_phase state-update     --phase state-update     "${COMMON[@]}" --outcome "$OUTCOME"

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

GID="$GOAL_ID" SF="$SRC_FILE" OUTCOME="$OUTCOME" OUTCOME_ORIGIN="$OUTCOME_ORIGIN" SRC_FLAG="$SOURCE" SD="$SCRIPT_DIR" python3 - <<'PYEOF'
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
current = 0
current_deep = 0
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

# Project root via _paths
sys.path.insert(0, sd)
try:
    import _paths
except Exception:
    sys.exit(0)

agent_dir = _paths.AGENT_DIR
if not agent_dir:
    sys.exit(0)

exp_path = agent_dir / "experience.jsonl"
threshold_seconds = 30 * 60  # 30-minute window
now = datetime.now()
has_recent = False
if exp_path.exists():
    try:
        # Read tail-N for performance — recent entries are at end
        lines = exp_path.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines[-100:]):  # last 100 entries
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("goal_id") != gid:
                continue
            cdate_s = e.get("created") or ""
            try:
                cdate = datetime.fromisoformat(cdate_s)
                if (now - cdate).total_seconds() < threshold_seconds:
                    has_recent = True
                    break
            except (ValueError, TypeError):
                continue
    except Exception:
        sys.exit(0)

if not has_recent:
    # 'trigger' uses 'postflip-deep' to make explicit that this fires on the
    # POST-Block-A/C-flip outcome, not the caller's original CLI outcome.
    # 'original_outcome' carries the caller's CLI arg so consumers can
    # distinguish 'caller asked for deep' from 'system flipped routine->deep'.
    # See  falsification finding and  Idea goal.
    payload = json.dumps({
        "triggered_at": now.isoformat(timespec="seconds"),
        "trigger": "recurring-close-postflip-deep-no-recent-entry",
        "goal_id": gid,
        "original_outcome": original_outcome,
    })
    wm_py = Path(pr) / "core" / "scripts" / "wm.py" if pr else None
    if wm_py and wm_py.exists():
        try:
            subprocess.run(
                [sys.executable, str(wm_py), "set", "force_experience_archival"],
                input=payload, text=True, capture_output=True, timeout=5,
            )
            print(f"[recurring-close] Phase 4.25 enforcement: deep close on {gid} with no recent experience entry — set force_experience_archival sentinel", file=sys.stderr)
        except Exception:
            pass  # Fail-open
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
echo ""
echo "[recurring-close] ═══ ITERATION COMPLETE ═══"
echo "[recurring-close] NEXT ACTION REQUIRED: Call Skill(aspirations) with args='loop' as your VERY NEXT tool call."
echo "[recurring-close] A Bash echo or text summary as the terminal action kills the loop (see .claude/rules/return-protocol.md)."

exit $MAX_RC
