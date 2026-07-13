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
#   --summary "<one-line>"   optional; used in journal entry + dependent unblock
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

# g-284-04: Recovery instructions on non-zero exit. The trap below reads
# _CURRENT_PHASE (set by each do_* function at entry) and prints
# phase-specific recovery commands when the script exits with rc != 0.
# Goal: when a verify-rejection / state-update-failure / lock-conflict
# leaves the goal in an indeterminate state, the caller (or operator)
# sees the exact retry command — not a generic "exit 1".
_CURRENT_PHASE=""

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
            echo "  Goal ${GOAL_ID:-?} may be in indeterminate state (verify rejection)." >&2
            local cmd="bash core/scripts/iteration-close.sh --phase verify --goal ${GOAL_ID:-<id>} --status ${GOAL_STATUS:-completed} --source ${SOURCE:-world}"
            [[ -n "$OUTCOME" ]] && cmd+=" --outcome $OUTCOME"
            [[ -n "$SUMMARY" ]] && cmd+=" --summary \"$SUMMARY\""
            [[ -n "$OVERRIDE_UNCOMMITTED" ]] && cmd+=" --override-uncommitted \"$OVERRIDE_UNCOMMITTED\""
            [[ -n "$OVERRIDE_MISSING_ARTIFACT" ]] && cmd+=" --override-missing-artifact \"$OVERRIDE_MISSING_ARTIFACT\""
            echo "  Retry: $cmd" >&2
            echo "  Revert (mark pending): bash core/scripts/aspirations-update-goal.sh --source ${SOURCE:-world} ${GOAL_ID:-<id>} status pending" >&2
            ;;
        state-update)
            echo "  Goal ${GOAL_ID:-?} has status=completed (verify succeeded) but state-update failed mid-phase." >&2
            echo "  Retry: bash core/scripts/iteration-close.sh --phase state-update --goal ${GOAL_ID:-<id>} --source ${SOURCE:-world} --outcome ${OUTCOME:-deep}" >&2
            echo "  (No goal-status revert needed — verify already closed the goal record.)" >&2
            ;;
        learning-gate)
            echo "  Goal ${GOAL_ID:-?} is closed (verify + state-update done) but learning-gate sub-step failed." >&2
            echo "  Retry: bash core/scripts/iteration-close.sh --phase learning-gate --goal ${GOAL_ID:-<id>} --source ${SOURCE:-world} --outcome ${OUTCOME:-deep}" >&2
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
        --phase)   PHASE="$2"; shift 2 ;;
        --goal)    GOAL_ID="$2"; shift 2 ;;
        --status)  GOAL_STATUS="$2"; shift 2 ;;
        # WORLD_AGENT_ONLY: cross-agent goals arrive with MIND_AGENT already
        # overridden to the owner (g-115-978 Option 3); enum stays world|agent.
        --source)  SOURCE="$2"; shift 2 ;;
        --outcome) OUTCOME="$2"; shift 2 ;;
        --summary) SUMMARY="$2"; shift 2 ;;
        # g-242-10: documented opt-out for the Phase 4.26 gate. Forwarded to
        # phase-4-26-gate.sh; logs to world/phase-4-26-overrides.jsonl.
        --no-retrieval-applicable) NO_RETRIEVAL_APPLICABLE="$2"; shift 2 ;;
        # g-115-228: state-update-audit velocity quality inputs. Forwarded only
        # at --phase state-update. Validation (range/type) happens downstream
        # in state-update-audit.py argparse — keep this layer thin.
        --tree-updated)     TREE_UPDATED="true"; shift ;;
        # g-115-464: bypass --tree-updated validation for cases where the
        # caller knows tree was updated but mtime probing won't see it
        # (programmatic update path, mtime-touched-back, etc.).
        --tree-updated-override) TREE_UPDATED_OVERRIDE="true"; shift ;;
        --artifacts-count)  ARTIFACTS_COUNT="$2"; shift 2 ;;
        --encoding-score)   ENCODING_SCORE="$2"; shift 2 ;;
        --findings-count)   FINDINGS_COUNT="$2"; shift 2 ;;
        # Forward --override-uncommitted to the aspirations-update-goal.sh
        # status=completed call inside do_verify. Without this, audit-only
        # Investigate goals (no source-code changes) and other goals that
        # don't own the dirty files in the working tree cannot reach
        # iteration-close — the gate fires correctly at the update layer
        # but iteration-close was previously stripping the override path.
        # Logged to world/uncommitted-work-overrides.jsonl by the gate.
        --override-uncommitted) OVERRIDE_UNCOMMITTED="$2"; shift 2 ;;
        # Forward --override-missing-artifact to the aspirations-update-goal.sh
        # status=completed call inside do_verify. Mirrors --override-uncommitted
        # forwarding above. Without this, goals whose description path-strings
        # don't exactly match the on-disk placement (e.g., a tree node placed
        # in a structurally-correct alternate location per the goal's "or
        # similar location under X" clause) cannot reach iteration-close —
        # goal-completion-artifact-gate.py fires at the update layer but
        # iteration-close was previously stripping the override path. Logged
        # to world/missing-artifact-overrides.jsonl by the gate.
        --override-missing-artifact) OVERRIDE_MISSING_ARTIFACT="$2"; shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
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

if [[ -z "$PHASE" ]]; then
    echo "usage: iteration-close.sh --phase {verify|state-update|learning-gate|productivity-check|recover} [--goal <id>] [--status <s>] [--source <w|a>] [--outcome <deep|routine|d|r>] [--summary <t>] [--tree-updated] [--tree-updated-override] [--artifacts-count <n>] [--encoding-score <0.0-1.0>] [--findings-count <n>]" >&2
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
    if [[ -z "$GOAL_ID" || -z "$GOAL_STATUS" || -z "$SOURCE" ]]; then
        local missing=""
        [[ -z "$GOAL_ID" ]]     && missing+=" --goal"
        [[ -z "$GOAL_STATUS" ]] && missing+=" --status"
        [[ -z "$SOURCE" ]]      && missing+=" --source"
        echo "verify: missing required flag(s):$missing" >&2
        echo "  usage: iteration-close.sh --phase verify --goal <id> --status <completed|blocked|skipped> --source <world|agent> [--outcome <deep|routine>] [--summary \"...\"]" >&2
        echo "  hint: pass --status completed for goals already marked complete via aspirations-update-goal.sh" >&2
        exit 2
    fi
    echo "[iteration-close] verify: goal=$GOAL_ID status=$GOAL_STATUS source=$SOURCE"

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
    # cmd_update_goal status=completed is BLOCKED for recurring goals at aspirations.py ~line 669.
    # cmd_complete_by atomically bumps lastAchievedAt + achievedCount + currentStreak/longestStreak
    # while keeping status=pending. Removing this branch resurrects the g-001-01 cargo-cult
    # incident (recurring goal re-selects forever at high score because lastAchievedAt
    # never advances). See plan improve-recurring-goals-kind-yao.md.
    local IS_RECURRING="false"
    if [[ "$GOAL_STATUS" == "completed" ]]; then
        local _src_file
        if [[ "$SOURCE" == "world" ]]; then
            _src_file="$WORLD_DIR/aspirations.jsonl"
        else
            _src_file="$AGENT_DIR/aspirations.jsonl"
        fi
        # Fail-open: probe error → treat as non-recurring. The non-recurring path
        # then hits cmd_update_goal's recurring guard and exits with the real error.
        IS_RECURRING="$(GID="$GOAL_ID" SF="$_src_file" python3 -c '
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
' || echo "false")"
    fi

    if [[ "$IS_RECURRING" == "true" ]]; then
        bash "$SCRIPT_DIR/aspirations-complete-by.sh" --source "$SOURCE" "$GOAL_ID"
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
            OVERRIDE_UNCOMMITTED="auto: iteration-commit.sh scheduled in state-update (g-280-03 wiring)"
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
        "${update_cmd[@]}"
        if [[ "$GOAL_STATUS" == "completed" ]]; then
            bash "$SCRIPT_DIR/aspirations-update-goal.sh" --source "$SOURCE" "$GOAL_ID" completed_date "$TODAY"
        fi
    fi

    # g-248-72: persist outcome_class to the goal record at completion time.
    # Sits OUTSIDE the recurring/non-recurring branch so both paths benefit:
    # recurring-close.sh already routes through do_verify, so each cycle's
    # latest outcome_class is captured. Backward-compatible: when caller
    # omits --outcome (legacy verify callers), $OUTCOME is empty and this
    # block is a no-op. cmd_update_goal in aspirations.py has no known-fields
    # allowlist (sets goal[field]=value directly at line 1273), so the new
    # field is recorded without a schema migration.
    if [[ -n "$OUTCOME" && "$GOAL_STATUS" == "completed" ]]; then
        bash "$SCRIPT_DIR/aspirations-update-goal.sh" --source "$SOURCE" "$GOAL_ID" outcome_class "$OUTCOME"
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
        # Informational: board-post failure is non-fatal, but surface stderr so
        # network/write issues don't silently disappear.
        echo "$msg" | bash "$SCRIPT_DIR/board-post.sh" --channel coordination --type complete --tags "$GOAL_ID" || true
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
    bash "$SCRIPT_DIR/team-state-clear-in-flight.sh" --agent "$AGENT" \
        || echo "[iteration-close] WARN: team-state-clear-in-flight failed for $AGENT (last_active + in_flight clear both lost)" >&2

    # g-284-06 Step 4: Transition intent_state from complete to committed.
    # All three state stores (iteration-checkpoint phase_completed, aspirations
    # status/outcome_class, team-state in_flight clear) have now landed. The
    # committed marker tells the recovery hook that this iteration's verify
    # finished cleanly — no retry needed.
    if [[ "$GOAL_STATUS" == "completed" && -n "$OUTCOME" ]]; then
        bash "$CORE_ROOT/scripts/loop-state-save.sh" update \
            --set "intent_state=committed" || true
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
        exit 2;
    }
    echo "[iteration-close] state-update: goal=$GOAL_ID outcome=$OUTCOME"

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
    # work_class so an empty work_class lands TRAILING. Bash IFS=$'\t' read
    # collapses empty MIDDLE fields on MSYS but preserves trailing. Reorder
    # avoids the collapse. g-115-175 root cause (2026-04-24).
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
        gate_json=$(COMMIT_SHA="${_commit_sha:-}" bash "$SCRIPT_DIR/post-state-update-gate.sh" deep 2>>"$CORE_ROOT/logs/iteration-close-stderr.log" || echo '{"fired":false}')
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
            echo "$gate_json_stamped" | bash "$SCRIPT_DIR/wm-set.sh" fresh_eyes_dispatch_pending >/dev/null 2>&1 || true
            # DISPATCH line — LLM reads this in-turn and invokes /fresh-eyes-code.
            local core_count reason
            core_count=$(echo "$gate_json" | python3 -c "import json,sys; print(json.load(sys.stdin).get('core_count', '?'))" 2>/dev/null || echo "?")
            reason=$(echo "$gate_json" | python3 -c "import json,sys; print(json.load(sys.stdin).get('reason', ''))" 2>/dev/null || echo "")
            echo "[iteration-close] DISPATCH: /fresh-eyes-code required — ${core_count} core files ($reason). See WM.fresh_eyes_dispatch_pending for full file list." >&2
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
            --manifest "$AGENT_DIR/session/retrieval-session.json" \
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
    bash "$SCRIPT_DIR/state-update-audit.sh" "${audit_args[@]}" \
        >/dev/null \
        || echo "[iteration-close] WARN: state-update-audit.sh failed (non-fatal — velocity/backpressure snapshot not recorded for $GOAL_ID)" >&2

    # Step 8.79 Post-State-Update METRIC Gate (g-115-724, rb-917 content-gate
    # sibling). Counter-gate sibling to post-state-update-gate.sh (above) and
    # tree-encoding-drift-gate.sh (below) catch "LLM skipped the encoding step
    # entirely" by counting occurrences. This content-gate catches "LLM did
    # the encoding step on the wrong content" by regex-scanning the
    # outcome_note + verify summary for numeric/ratio findings.
    #
    # Fires on deep outcomes when 2+ distinct numeric findings appear in the
    # outcome_note AND tree-edit-since.py reports no tree edit since
    # iteration-checkpoint.json:selected_at. Writes force_metric_encoding_pending
    # WM sentinel; aspirations-precheck Phase 0-pre4 consumes it.
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
        metric_gate_json=$(printf '%s' "${SUMMARY:-}" | \
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
            echo "[iteration-close] METRIC-ENCODING: $distinct_count distinct numeric findings detected in outcome_note ($metric_reason). force_metric_encoding_pending set — precheck Phase 0-pre4 will dispatch next iteration." >&2
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
    if [[ "$OUTCOME" == "deep" ]] && [[ -f "$WORLD_DIR/conventions/outcome-observation.md" ]]; then
        bash "$WORLD_DIR/scripts/outcome-metrics-collect.sh" 2>/dev/null \
            || echo "[iteration-close] WARN: outcome-metrics-collect.sh failed (non-fatal; outcome-metrics.yaml not advanced this iteration)" >&2
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
print(json.dumps({
    "title": f"Investigate: aspirations.jsonl canary fired on {os.environ[\"CANARY_BASENAME\"]} ({os.environ[\"CANARY_NOW\"]})",
    "priority": "HIGH",
    "participants": ["agent"],
    "category": "framework-architecture",
    "origin_signal": f"canary-fired:{os.environ[\"CANARY_BASENAME\"]}:{os.environ[\"CANARY_NOW\"]}",
    "description": (
        f"iteration-close.sh state-update canary detected corruption in "
        f"{os.environ[\"CANARY_TARGET\"]} after state-update for goal "
        f"{os.environ[\"CANARY_GOAL_ID\"]}. File was restored from latest "
        f".history snapshot in place; corrupted version preserved as "
        f".canary-corrupt sidecar. Investigate root cause — _fileops.py "
        f"outcomes 1+2 should have caught this earlier; canary firing "
        f"means corruption slipped through some path that bypasses the "
        f"writer-layer validation."
    ),
}))
')"
            printf '%s' "$_canary_payload" | bash "$SCRIPT_DIR/aspirations-add-goal.sh" --source world --aspiration asp-115 \
                >/dev/null 2>&1 \
                || echo "[iteration-close] WARN: canary-Investigate goal-file failed for $_canary_target (non-fatal)" >&2
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

    # Retrieval-performed tracking (g-001-132). retrieve.py --goal auto-writes
    # retrieval-session.json when intelligent retrieval fires (Phase 4). Recurring
    # / routine goals skip that path entirely, so the PRIOR goal's file persists
    # with utilization_pending=false and both gates silently short-circuit —
    # leaving "did this goal retrieve?" unobservable and allowing orphan
    # accumulation to hide. Fix: at this point the file either (a) was written
    # for THIS goal and fires the existing feedback flow, or (b) is missing or
    # stale (refers to a prior goal) and gets overwritten with a no-retrieval
    # stub. Either way a per-iteration retrieval-summary line is emitted — grep
    # over N iterations gives retrieval_using_ratio.
    local ret_file="$AGENT_DIR/session/retrieval-session.json"
    local current_file_goal=""
    if [[ -f "$ret_file" ]]; then
        current_file_goal="$(python3 -c "
import json
try:
    print(json.loads(open(r'$ret_file', encoding='utf-8').read()).get('goal_id',''))
except Exception:
    print('')
" 2>/dev/null || echo "")"
    fi

    if [[ "$current_file_goal" != "$GOAL_ID" ]]; then
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
    else
        # Retrieval was performed for this goal — run safety-net feedback if pending.
        local pending
        pending="$(python3 -c "
import json
try:
    d = json.loads(open(r'$ret_file', encoding='utf-8').read())
    print('true' if d.get('utilization_pending') else 'false')
except Exception:
    print('false')
" || { echo "[iteration-close] WARN: retrieval-session.json read failed — retrieval gate skipped" >&2; echo "false"; })"
        if [[ "$pending" == "true" ]]; then
            # Two-tier safety-net feedback (g-242-06, fix-a for rb-428/g-242-05 cycle):
            # (1) Try --infer --confidence conservative first. Produces
            #     times_inferred_helpful (half-weight counter) when distinctive tokens
            #     match execution diary / guardrail triggers — unblocks utilization_score
            #     movement without requiring LLM attestation in Phase 4.26.
            # (2) On exit 4 (schema_version < 2; documented fallback signal), fall back
            #     to --all-unknown (no-op on counters; just records the method so the
            #     pending flag clears). Replaces the older --all-noise fallback whose
            #     blanket times_noise++ was the root cause of the 0/655 helpful audit
            #     2026-05-07: legitimate retrieval-but-unattested nodes accumulated noise
            #     that fed tree.py distill candidates. See utilization-gate.sh header.
            # (3) On any other non-zero: warn + leave pending=true so next iteration retries.
            # Non-fatal either way: retrieval-session.json's utilization_pending flag is
            # what drives retry; we never crash iteration-close over a feedback blip.
            local infer_rc
            # C.2: balanced (min_distinctive=1) replaces conservative (>=2).
            # Token-overlap inference is the second-stage classifier — its job
            # is to find candidates the explicit-attest path missed. The
            # fallback-active backstop in infer_feedback (utilization-feedback.py)
            # is the third stage that catches deterministic trigger-fires.
            # Conservative was starving positive signal: 0 helpful across 320
            # active guardrails as of 2026-05-09 audit.
            if bash "$SCRIPT_DIR/utilization-feedback.sh" --goal "$GOAL_ID" --infer --confidence balanced; then
                echo "[iteration-close] retrieval gate: inferred utilization feedback for $GOAL_ID (balanced)"
            else
                infer_rc=$?
                if [[ $infer_rc -eq 4 ]]; then
                    # Schema too old — documented fallback path
                    if bash "$SCRIPT_DIR/utilization-feedback.sh" --goal "$GOAL_ID" --all-unknown; then
                        echo "[iteration-close] retrieval gate: --infer schema<2, fell back to --all-unknown for $GOAL_ID"
                    else
                        echo "[iteration-close] WARN: utilization-feedback --all-unknown fallback failed for $GOAL_ID (pending=true persists)" >&2
                    fi
                else
                    echo "[iteration-close] WARN: utilization-feedback --infer failed (rc=$infer_rc) for $GOAL_ID (pending=true persists, next iteration will retry)" >&2
                fi
            fi
        fi
        echo "[iteration-close] retrieval-summary: performed=true goal=$GOAL_ID"
    fi

    # Check unreflected hypotheses count — signal via exit; LLM sees stderr
    local unreflected
    unreflected="$(bash "$SCRIPT_DIR/pipeline-read.sh" --unreflected | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(len(d) if isinstance(d, list) else 0)
except Exception:
    print(0)
" || { echo "[iteration-close] WARN: pipeline-read --unreflected failed — defaulting to 0 (LLM may miss unreflected hypothesis count)" >&2; echo "0"; })"
    if [[ "$unreflected" -gt 0 ]]; then
        echo "[iteration-close] LLM-ACTION: $unreflected unreflected hypothesis/es — digest § LEARNING-GATE item 3" >&2
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
    # and a source store (rb/guardrails) is newer than the index, spawns
    # `embedding-index-build.py --update` DETACHED (incremental — re-embeds
    # changed docs only), debounced to one attempt per 6h. Same LOCAL-tick
    # rationale as agent-watchdog above: the index is per-box daemon cache,
    # so a world-scoped recurring goal (runs on ONE box per firing) cannot
    # keep every box fresh. Fail-open: never delays loop continuation.
    python3 "$(_winpath "$SCRIPT_DIR/embedding-index-freshness.py")" \
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

    # Keep origin current (g-115-1734, USER DIRECTIVE Zachary 2026-07-02):
    # fail-soft, rate-limited push of accumulated loop-commits. productivity-check
    # is the terminal phase, so the state-update commit (do_state_update) has
    # already landed. iteration-push.sh batches (pushes only when origin is behind
    # by >= N commits OR the oldest unpushed commit is >= T minutes old), skips
    # when .git/index.lock is held (guard-853), never force-pushes, and is
    # fail-open — a push failure logs to stderr and NEVER aborts productivity-check
    # or blocks loop continuation. Placed BEFORE the ITERATION COMPLETE imperative
    # so that imperative stays the terminal stdout line (return-protocol).
    {
        bash "$SCRIPT_DIR/iteration-push.sh" --repo "$PROJECT_ROOT"
    } || true

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
        bash "$SCRIPT_DIR/team-state-clear-in-flight.sh" --agent "$AGENT" \
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
