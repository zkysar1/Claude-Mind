#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Entry sentinel for hook-fire-audit () — FIRST executable line,
# bash-builtin only, fail-open. mtime of core/logs/hook-fires/recovery-gate
# = last fire of this hook.
{ _HF_DIR="${BASH_SOURCE[0]%/*}/../.." ; mkdir -p "$_HF_DIR/core/logs/hook-fires" 2>/dev/null && : > "$_HF_DIR/core/logs/hook-fires/recovery-gate" 2>/dev/null ; unset _HF_DIR ; } 2>/dev/null || true

# recovery-gate.sh — script-gated auto-recovery for crashed runner sessions.
#
# Invoked by the SessionStart hook (configured in .claude/settings.json).
# Detects unambiguously-crashed runner sessions and performs the same cleanup
# as `/start --recover` — without routing through the user. The gate fires
# ONLY when ALL SIX conditions are simultaneously true:
#
#   1. agent-state == RUNNING            (something claims the loop owns it)
#   2. heartbeat-stale.sh returns "stale" (no one is advancing the mtime)
#   2.5. runner-recent-block.sh returns 1 (no BLOCK logged in last 5 min)
#   2.7. execution-diary.jsonl mtime older than stale_minutes/2 (default 15min)
#   3. stop-requested NOT set            (graceful-stop is not in flight)
#   4. background-jobs.sh has-pending exits 1 (no Tier-A registered job)
#
# Condition 2.5 (, 2026-05-09): multi-signal liveness. Heartbeat
# alone is too weak when transient platform issues (e.g., Claude Code 2.1.133
# stop-hook timeouts at 8.4s when normal is 2.5s) make heartbeat staleness a
# false-positive zombie signal. A recent BLOCK in .stop-hook-log proves the
# runner is alive: stop-hook fired AND the loop re-entered AND advanced.
#
# Condition 2.7 (, 2026-05-10): execution-diary freshness — second
# multi-signal liveness probe. execution-diary.jsonl is appended at every
# Phase start/end (sub-minute granularity) AND survives stop-hook
# interruptions because phase-end is written BEFORE the LLM yields the turn.
# When stop-hooks are intermittently timing out (e.g. Claude Code 2.1.133
# regression), the runner stays alive (responding to user-typed Skill calls,
# processing background-task notifications) but heartbeat-tick may skip
# enough cycles for the mtime to cross stale. Diary mtime is a more reliable
# liveness signal for that path. Threshold = stale_minutes/2 (default 15min)
# is more protective of live runners than the 30min heartbeat threshold but
# still recovers genuine zombies whose diary went silent for ≥15min.
#
# Conditions 3 and 4 prevent stomping legitimate scenarios:
#   - graceful /stop: Phase -1.4 owns the wind-down. Even if heartbeat goes stale
#     while the agent is in graceful-stop wait, recovery must NOT fire — the
#     /stop skill will resolve it cleanly.
#   - long external job: Processor runs 3-7h on the GPU EC2; the runner-heartbeat
#     mtime can lag if the loop is parked waiting on the job. background-jobs.yaml
#     is the explicit "I have a long thing in flight" signal — single source of
#     truth, NOT a heartbeat-threshold inflation.
#
# WHY NO PID-LIVENESS CHECK: Plan called for a PID liveness probe alongside
# heartbeat. We removed it — neither $$ nor $PPID from an LLM Bash call
# captures the long-lived Claude Code process (the launcher decouples; PPID
# resolves to init/1). Adding a layer that systematically mis-reports DEAD
# would auto-recover healthy sessions whenever the heartbeat staled (e.g.,
# long subagent calls). Multi-signal liveness via .stop-hook-log (Cond 2.5)
# is the alternative we adopted: it doesn't require process-table access,
# works identically across platforms, and uses an artifact the runner ITSELF
# produces. If you want PID-liveness later, capture it from the SessionStart
# hook's $PPID (which DOES resolve to Claude Code at hook time) and verify
# on this platform first.
#
# This is the SECOND authorized caller of session-state-set.sh outside /start
# and /stop (productivity-stop-gate.sh is the first). See:
#   .claude/rules/stop-hook-compliance.md  — exception block
#   .claude/rules/user-interaction.md      — Script-Level Restrictions
#
# Cleanup is delegated to session-manifest-clear.sh — same code path
# /start --recover Phase 0.7 uses. Single source of truth for the clear list.
# Includes cross-agent team-state in_flight clearance (rb-671) — without it,
# partner agents see the recovered agent as indefinitely mid-execution.
#
# BOUND-AGENT GATE (2026-05-14, post-zeta-recovery audit): all three paths
# (A crashed-runner, B state-corruption, C hung-autocompact) act ONLY on the
# agent bound to the current SessionStart SID. Prior design iterated every
# *<agent>/local-paths.conf and applied per-agent gates — that produced
# cross-agent state-flips "without explicit user intent" when a SessionStart
# for agent A happened during long-running work in agent B. Now: if the
# SessionStart payload has no session_id, or no `.active-agent-<sid>` binding
# exists, or the binding points at a framework directory, all three paths
# skip. The user must explicitly /start --recover from a bound terminal for
# any cross-agent recovery they actually want.
#
# Fail-open: ANY error or unmet condition exits 0. The gate never blocks
# session start. It either recovers silently and surfaces the notice, or it
# does nothing.

set -uo pipefail   # NOT -e — we want explicit branching on probe outcomes
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"
cd "$PROJECT_ROOT"

# --- SessionStart source gate ---
# This block is an INTENT pre-filter, NOT a 5th liveness condition.
# DO NOT add PID / process / heartbeat checks here — see the
# "WHY NO PID-LIVENESS CHECK" block above for why a 5th condition is wrong.
#
# Continuations (source=compact or source=resume) preserve the logical
# session across a fresh CLAUDE_CODE_SID; the runner was NOT crashed, it
# was paused by the platform (autocompact) or the user (claude --resume).
# rb-432 traces what happens when this check is absent: a continuation
# whose heartbeat aged past the stale threshold is indistinguishable from
# a crashed runner at the 6-condition layer and gets wrongly demoted.
#
# CRITICAL — the source gate applies to PATH A (crashed-runner detection)
# ONLY. Path B (state-corruption detection) below runs unconditionally
# regardless of source, because state=RUNNING + running-session-id missing
# is a degenerate combination that should not exist on a legitimate compact
# continuation either (session-save-id.sh preserves running-session-id
# across compact via the four-witness atomic write).
#
# Unmatched sources (startup, clear, empty, malformed, python-missing)
# fall through the case to the 6-condition gate. That IS the fail-open:
# no sentinel, no fallback chain, just "skip only on known continuations."
#
# STDIN IS CONSUMED HERE. Do not add further stdin reads below this block.
PAYLOAD=""
if [ ! -t 0 ]; then
    PAYLOAD="$(cat)"
fi
SOURCE="$(printf '%s' "$PAYLOAD" | python3 -c 'import json,sys; d=json.loads(sys.stdin.read() or "{}"); print(d.get("source",""))' 2>/dev/null)"
SESSION_ID="$(printf '%s' "$PAYLOAD" | python3 -c 'import json,sys; d=json.loads(sys.stdin.read() or "{}"); print(d.get("session_id",""))' 2>/dev/null)"

# --- Bound-agent gate (2026-05-14 audit) ---
# Recovery acts ONLY on the agent this SessionStart is bound to. The earlier
# "iterate every agent dir" design ( era) let a SessionStart for
# agent A flip agent B's state on disk — the load-bearing surprise that killed
# zeta's runner on 2026-05-13 when a new zeta-mode session triggered Path A
# against the running zeta loop. Cross-agent state-flips happened "without
# explicit user intent" — the user opening a terminal for one agent had no
# business modifying another agent's state.
#
# Bound agent is derived from SessionStart payload session_id +
# .active-agent-<sid> binding file. If unbound (fresh terminal, no /start
# yet), all three paths skip. The user can /start --recover for explicit
# recovery; that path uses session-state-set.sh under its own authorization.
#
# Fail-open: missing payload, missing binding file, framework dir match —
# all exit 0 (no action). The gate never blocks session start.
BOUND_AGENT=""
# Phase 2.6 binding (preferred): agents/<name>/sessions/<SID>/binding.yaml.
# Without this check, Phase 2.6 sessions whose .active-agent-<SID> form
# was retired by /start --retire-legacy never get auto-recovered from
# zombie state — recovery-gate exits silently at the empty BOUND_AGENT
# check below. Matches the stop-hook.sh fix (ff6e71c6).
if [ -n "$SESSION_ID" ]; then
    for _BF in "$PROJECT_ROOT/${AGENTS_PARENT_DIR}"/*/sessions/"$SESSION_ID"/binding.yaml; do
        [ -f "$_BF" ] || continue
        _BD="${_BF%/sessions/*}"
        BOUND_AGENT="${_BD##*/}"
        break
    done
fi
# Legacy fallback: pre-Phase-2.6 .active-agent-<SID> file at PROJECT_ROOT.
if [ -z "$BOUND_AGENT" ] && [ -n "$SESSION_ID" ] && [ -f "$PROJECT_ROOT/.active-agent-$SESSION_ID" ]; then
    BOUND_AGENT="$(tr -d '\r\n[:space:]' < "$PROJECT_ROOT/.active-agent-$SESSION_ID")"
fi
case "$BOUND_AGENT" in
    ""|core|meta|world|.git|.claude) BOUND_AGENT="" ;;
esac
# DO NOT REMOVE — load-bearing exit. Path A/B/C all index "$PROJECT_ROOT/$BOUND_AGENT/...";
# without this guard they would scan/clear PROJECT_ROOT itself or a framework dir.
if [ -z "$BOUND_AGENT" ] || [ ! -d "$(agent_dir "$BOUND_AGENT")/session" ]; then
    exit 0
fi

# === SHARED RECOVERY ACTION ===
# Both Path A (crashed-runner) and Path B (state-corruption) end here.
#
# CRITICAL — DO NOT silently swallow `session-state-set.sh IDLE` failures.
# An incident on 2026-04-25 (alpha session 58 follow-up) showed the
# previous form `session-state-set.sh IDLE 2>/dev/null || true` left the
# system in a half-recovered zombie: manifest-clear deleted
# running-session-id but state stayed RUNNING because the state-set
# silently failed. The recovery-notice was written anyway, falsely
# advertising success. The fix is to capture rc, log a critical
# desync-warnings entry on failure, AND SKIP the recovery-notice +
# recovery-log writes — single source of truth: a recovery either
# completed (state IS IDLE) or it didn't (warning surfaces, no notice).
_perform_recovery() {
    local agent="$1"
    local cause="$2"
    local _adir
    _adir="$(agent_dir "$agent")"

    # Permanent-failure circuit breaker (2026-05-12 hardening). If
    # _perform_recovery has failed N consecutive times, REFUSE to retry —
    # the underlying problem (permission denied, locked state file, broken
    # session-state-set.sh) is not going to fix itself, and silently looping
    # on every SessionStart hides the problem from the user. After N=3 we
    # write recovery-failed-permanent (manifest entry) and return rc=2 so
    # Path A/B callers see a distinct exit code. The /start --recover --force
    # path clears both files (counter + permanent signal) to allow manual
    # retry once the user has investigated. DO NOT lower the threshold below
    # 3 — transient FS/AV issues commonly bounce 1-2 retries.
    local fail_count=0
    if [[ -f "$_adir/session/recovery-failure-count" ]]; then
        fail_count="$(cat "$_adir/session/recovery-failure-count" 2>/dev/null | tr -d '[:space:]' || echo 0)"
        [[ "$fail_count" =~ ^[0-9]+$ ]] || fail_count=0
    fi
    if [[ "$fail_count" -ge 3 ]]; then
        mkdir -p "$_adir/session"
        printf 'PERMANENT_RECOVERY_FAILURE: %s consecutive failed recoveries. Last cause: %s. Run "/start %s --recover --force" to clear.\n' \
            "$fail_count" "$cause" "$agent" > "$_adir/session/recovery-failed-permanent" 2>/dev/null || true
        echo "[recovery-gate] CRITICAL: $agent has $fail_count consecutive failed recoveries; refusing automatic retry. Run '/start $agent --recover --force' to clear." >&2
        return 2
    fi

    local sid_recorded=""
    if [[ -f "$_adir/session/running-session-id" ]]; then
        sid_recorded="$(tr -d '\r\n[:space:]' < "$_adir/session/running-session-id")"
    fi

    # State transition — RUNNING → IDLE. Authorized by the exception block
    # in stop-hook-compliance.md. NOT silently swallowed (see CRITICAL above).
    # stderr routes to recovery-gate-stderr.log so the rc=$rc log entry below
    # has a corresponding diagnostic — without this, an operator only sees the
    # rc but cannot diagnose WHY (permission denied, traceback, missing path).
    # Pattern matches iteration-close.sh:514 (post-state-update-gate stderr
    # routing). Filed as fresh-eyes-code bravo-fec-recovery-gate-stderr-swallow
    # F-001, fixed in .
    #
    # ORDERING (, 2026-05-13, inverse of rb-323/guard-403): the
    # state-set IDLE fires BEFORE manifest-clear so observers never see
    # state=RUNNING + sid=missing during the cleanup window. If state-set
    # fails, manifest-clear is SKIPPED — leaving state=RUNNING + sid present
    # (normal RUNNING, recoverable on next SessionStart) rather than the
    # half-recovered zombie state (sid gone + state=RUNNING) that the
    # previous ordering produced. The rc-check + desync-warnings + retry-
    # counter machinery below is preserved as defense-in-depth.
    mkdir -p "$CORE_ROOT/logs"
    MIND_AGENT="$agent" bash "$SCRIPT_DIR/session-state-set.sh" IDLE 2>>"$CORE_ROOT/logs/recovery-gate-stderr.log"
    local rc=$?
    if [[ $rc -ne 0 ]]; then
        local ts_fail
        ts_fail="$(date +%Y-%m-%dT%H:%M:%S)"
        local warning
        warning="$(python3 -c "import json,sys; print(json.dumps({'id':'recovery_state_transition_failed','severity':'critical','description':sys.argv[1],'logged_at':sys.argv[2]}))" \
            "Recovery FAILED for $agent: session-state-set.sh IDLE returned rc=$rc. State did NOT transition; manifest-clear was SKIPPED to avoid half-recovered zombie. Cause: $cause" \
            "$ts_fail" 2>/dev/null || echo "")"
        if [[ -n "$warning" ]]; then
            mkdir -p "$_adir/session"
            printf '%s\n' "$warning" >> "$_adir/session/desync-warnings.jsonl"
        fi
        echo "[recovery-gate] CRITICAL: $agent recovery half-completed (state-set IDLE rc=$rc); see desync-warnings.jsonl" >&2
        # Increment the consecutive-failure counter. Atomic .tmp + mv so
        # interrupted writes don't corrupt the counter. Bounded retry (the
        # threshold check at function entry trips at 3 and short-circuits).
        local new_count=$((fail_count + 1))
        echo "$new_count" > "$_adir/session/recovery-failure-count.tmp" 2>/dev/null \
            && mv "$_adir/session/recovery-failure-count.tmp" "$_adir/session/recovery-failure-count" 2>/dev/null
        return 1
    fi

    # Manifest-driven clear — IDENTICAL to /start --recover Phase 0.7.
    # Single source of truth lives in session-manifest-clear.sh.
    # Runs AFTER state-set IDLE succeeded ( reorder); the cleanup
    # window now shows state=IDLE + sid present (mirrors aspirations-graceful-
    # stop D1→D6 pattern) instead of state=RUNNING + sid=missing.
    MIND_AGENT="$agent" bash "$SCRIPT_DIR/session-manifest-clear.sh" >&2 || true

    # Recovery succeeded — reset the consecutive-failure counter. Any prior
    # transient failures were not the underlying-broken case, so the user
    # should not see a permanent signal next session.
    rm -f "$_adir/session/recovery-failure-count" 2>/dev/null || true
    rm -f "$_adir/session/recovery-failed-permanent" 2>/dev/null || true

    # Recovery notice — /prime surfaces this in the next session's PRIMED
    # block, then deletes the file. Single line; just human text.
    # Only written when state-set IDLE succeeded — never advertise success
    # for a failed recovery.
    mkdir -p "$_adir/session"
    printf '%s\n' "Auto-recovered: $cause" > "$_adir/session/recovery-notice"

    # Audit log — append-only JSONL.
    local ts
    ts="$(date +%Y-%m-%dT%H:%M:%S)"
    local entry
    entry="$(python3 -c "import json,sys; print(json.dumps({'ts': sys.argv[1], 'agent': sys.argv[2], 'cause': sys.argv[3], 'sid_recorded': sys.argv[4] or None}))" \
        "$ts" "$agent" "$cause" "$sid_recorded" 2>/dev/null || echo "")"
    if [[ -n "$entry" ]]; then
        printf '%s\n' "$entry" >> "$_adir/session/recovery-log.jsonl"
    fi

    echo "[recovery-gate] RECOVERED $agent: $cause" >&2
}

# === PATH B: State-corruption detection (source-independent) ===
# Triggers on the degenerate combination state=RUNNING + running-session-id
# missing + no stop-requested. This intersection is provably impossible in a
# healthy session: /start atomically pair-writes running-session-id +
# state=RUNNING; aspirations-graceful-stop deletes running-session-id only
# AFTER state has transitioned out of RUNNING; session-save-id.sh on
# autocompact preserves the atomic relationship via the four-witness check.
# So if we observe (RUNNING, missing, no stop-requested) together, a partial
# recovery or unauthorized writer cleared the SID and the loop has lost its
# safety net (stop-hook hits gate=no-runner, no longer BLOCKs turn-end).
# Detection independent of heartbeat: even a fresh heartbeat doesn't redeem
# a session that has no SID claim — observed 2026-04-25 alpha incident,
# where the user manually drove "continue" prompts for hours after this
# state appeared, but the loop died the moment the user paused.
_check_state_corruption() {
    local agent="$1"
    local _adir
    _adir="$(agent_dir "$agent")"
    [[ -d "$_adir/session" ]] || return 0

    local state
    state="$(MIND_AGENT="$agent" bash "$SCRIPT_DIR/session-state-get.sh" 2>/dev/null || echo "")"
    [[ "$state" == "RUNNING" ]] || return 0

    [[ ! -f "$_adir/session/running-session-id" ]] || return 0

    # stop-requested gate — graceful-stop owns the wind-down, recovery must
    # not stomp it even if running-session-id has been cleared by D-step.
    # Exit-code semantics mirror Cond 2.5/Cond 4 (rb-762): rc=1 is the ONLY
    # "continue" code. rc=0 (signal exists) AND rc=2+ (script error) both
    # suppress recovery. Conservative: if the probe is broken, do NOT stomp
    # a possibly-mid-stop agent.
    MIND_AGENT="$agent" bash "$SCRIPT_DIR/session-signal-exists.sh" stop-requested >/dev/null 2>&1
    local sr_rc=$?
    [[ $sr_rc -eq 1 ]] || return 0   # 0=signal-set, 2+=error → both suppress recovery

    # Autocompact in flight — session-save-id.sh source=compact branch
    # rewrites running-session-id from the breadcrumb. Path B must not race
    # ahead and demote a mid-compact runner. Path C handles HUNG compacts
    # (compact-pending mtime >60min); this gate handles NORMAL compacts.
    # DO NOT REMOVE — origin: zeta 2026-05-12T08:45 false-positive recovery,
    # session-save-id and recovery-gate hooks fired in parallel order.
    [[ -f "$_adir/session/compact-pending" ]] && return 0

    local cause="state corruption: state=RUNNING, running-session-id missing, no stop-requested"
    _perform_recovery "$agent" "$cause"
}

# POST_RECOVERY_EDIT_OVERRIDE="User-directed framework fix for hung-autocompact false-positive recovery; implementing before /start delta to prevent immediate repeat."
# === PATH C: Hung-autocompact detection (source-independent) ===
# Triggers when compact-in-flight exists with mtime > 60 min AND heartbeat is
# stale AND state=RUNNING AND no stop-requested AND diary stale AND no
# execute-in-flight sentinel. Covers the case where the autocompact API call
# hangs indefinitely — precompact-serialize.sh wrote compact-in-flight, but
# SessionStart(compact) never fires to consume it because the autocompact
# never completes.
#
# 2026-05-22 sentinel rename: previously checked compact-pending mtime, but
# compact-pending is written by stop-hook on EVERY iteration BLOCK as the
# SID-binding breadcrumb. Its mtime tracked "time since last BLOCK" not
# "time since autocompact start" — a deep Phase 4 work session running
# >60 min without a phase boundary aged out the threshold and false-
# positive-fired. Canonical incident: 2026-05-22T17:02:31 delta recovered
# during 1h 31m Phase 4 of 7 (no autocompact actually running).
# Fix: compact-in-flight is written ONLY by PreCompact, so its mtime
# accurately reflects autocompact start. See precompact-serialize.sh and
# session-save-id.sh for the new sentinel's lifecycle.
#
# Source-independent: must fire on source=compact (the hung compact's own
# delayed SessionStart, if it ever arrives) AND source=startup (a fresh
# session opened while the original is hung). Mirrors Path B's design.
#
# 60-min threshold: normal autocompact round-trips in <2 min; 60 min is
# unambiguously hung. With the compact-in-flight rename above, the threshold
# now precisely matches autocompact roundtrip duration — no need to inflate
# to cushion the prior conflation.
#
# Origin: bravo's 2026-05-05 incident — compact-pending stamped at 17:07:44,
# autocompact hung 4h, no postcompact-restore ever ran, loop_state went empty,
# bravo unrecoverable until external nudge. See rb encoded under .
_check_hung_autocompact() {
    local agent="$1"
    local _adir
    _adir="$(agent_dir "$agent")"
    # 2026-05-22: switched from compact-pending (stop-hook breadcrumb, refreshed
    # on every BLOCK) to compact-in-flight (PreCompact-only sentinel). See
    # function-level comment block above for the rename rationale.
    local cif="$_adir/session/compact-in-flight"
    [[ -f "$cif" ]] || return 0

    # CRITICAL — DO NOT lower the 60-min threshold below the upper bound of a
    # NORMAL autocompact roundtrip. Normal compacts complete in <2 min; 30 min
    # is too aggressive (would race the heartbeat-stale threshold for the
    # same condition). 60 min is the unambiguous-hang line. Lowering this to
    # match heartbeat-stale (30 min) would cause Path C to false-positive on
    # an autocompact that's slow but recovering. Origin: rb-697 (bravo
    # 2026-05-05, autocompact hung 4h before user nudge).
    [[ -n "$(find "$cif" -maxdepth 0 -mmin +60 2>/dev/null)" ]] || return 0

    # CRITICAL — state==RUNNING is the recovery-vs-cleanup boundary. Path C is
    # for recovering a stuck-in-RUNNING agent, NOT for cleaning up stale
    # compact-in-flight files on already-IDLE agents. session-save-id.sh
    # handles the cleanup case at SessionStart. If a future dev expands this
    # to IDLE-state agents, the recovery action (_perform_recovery →
    # state-set IDLE) becomes a no-op write and the audit log fills with
    # spurious entries. Cleanup ≠ recovery.
    local state
    state="$(MIND_AGENT="$agent" bash "$SCRIPT_DIR/session-state-get.sh" 2>/dev/null || echo "")"
    [[ "$state" == "RUNNING" ]] || return 0

    # Heartbeat gate: must be stale (no iteration is advancing it).
    local hb
    hb="$(MIND_AGENT="$agent" bash "$SCRIPT_DIR/heartbeat-stale.sh" 2>/dev/null || echo "fresh")"
    [[ "$hb" == "stale" ]] || return 0

    # Execute-in-flight suppressor (7 followup, 2026-05-22). When
    # the agent is mid-Phase-4-execute, deep code work can run >60 min
    # without a phase boundary or diary write — every liveness signal goes
    # stale even though work IS progressing. execute-in-flight is written by
    # execution-diary.py on phase_start phase-4-execute, deleted on phase_end
    # phase-4-execute (or any subsequent phase_start). Sentinel within 4h
    # (240min) — agent is genuinely working in Phase 4, suppress recovery.
    # Beyond 4h — Phase 4 itself is hung, let recovery fire.
    local eif="$_adir/session/execute-in-flight"
    if [[ -f "$eif" ]]; then
        if [[ -z "$(find "$eif" -maxdepth 0 -mmin +240 2>/dev/null)" ]]; then
            return 0
        fi
    fi

    # Diary freshness suppressor (defense-in-depth, 2026-05-22). Mirrors
    # Path A's Condition 2.7 — if execution-diary was written in the last
    # 15 min, the agent is actively producing narrative work, suppress
    # recovery. Catches cases where execute-in-flight is absent (non-Phase-4
    # long work) but the agent is still alive and writing observations or
    # findings.
    local diary="$_adir/session/execution-diary.jsonl"
    if [[ -f "$diary" ]]; then
        if [[ -z "$(find "$diary" -maxdepth 0 -mmin +15 2>/dev/null)" ]]; then
            return 0
        fi
    fi

    # stop-requested gate: graceful /stop owns the wind-down.
    # Exit-code semantics mirror Cond 2.5/Cond 4 (rb-762): rc=1 is the ONLY
    # "continue" code. rc=0 (signal exists) AND rc=2+ (script error) both
    # suppress recovery.
    MIND_AGENT="$agent" bash "$SCRIPT_DIR/session-signal-exists.sh" stop-requested >/dev/null 2>&1
    local sr_rc=$?
    [[ $sr_rc -eq 1 ]] || return 0   # 0=signal-set, 2+=error → both suppress recovery

    local cause="hung autocompact: compact-in-flight mtime >60min, heartbeat=$hb, state=RUNNING, no stop-requested, no execute-in-flight, diary stale"
    _perform_recovery "$agent" "$cause"
}

# === PATH A: Crashed-runner detection (6-condition gate, source-gated) ===
#
# The 6 conditions are EXTRACTED to `core/scripts/runner-dead-check.sh`
# (, 2026-05-19 — single source of truth). The helper checks:
#   (1)   agent-state == RUNNING
#   (2)   heartbeat-stale.sh returns "stale"
#   (2.5) runner-recent-block.sh returns 1 (no BLOCK in last 5 min;
#          multi-signal — Claude Code 2.1.133 hook timeouts
#         make heartbeat alone a false-positive zombie signal)
#   (2.7) execution-diary.jsonl mtime older than DIARY_STALE_MINUTES
#         (default 15 min;  — diary survives hook interruptions
#         because phase-end writes BEFORE the LLM yields the turn)
#   (3)   stop-requested NOT set (rb-762 rc=1-only continue semantics)
#   (4)   background-jobs.sh has-pending returns 1 (no Tier-A jobs)
#
# Three parallel callers defer to that helper:
#   - This function (silent SessionStart-hook recovery)
#   - `.claude/skills/start/SKILL.md` Step 0.7 (--recover branch; explicit user)
#   - `.claude/skills/start/SKILL.md` auto-recovery branch (RUNNING + autonomous;
#     LLM-orchestrated; kept inline because user-facing branching messages need
#     per-condition state — that inline copy mirrors the helper but is not
#     bash-callable)
#
# Any change to the 6 conditions or probe scripts MUST update:
#   - runner-dead-check.sh (canonical)
#   - /start SKILL.md auto-recovery section (inline LLM copy)
# This function and /start --recover defer to the helper, so no inline updates
# are needed here for condition changes — just keep the comment block in sync.
run_gate_for_agent() {
    local agent="$1"
    local _adir
    _adir="$(agent_dir "$agent")"
    [[ -d "$_adir/session" ]] || return 0  # not initialized

    # POST_RECOVERY_EDIT_OVERRIDE="User-directed framework fix for hung-autocompact false-positive recovery; implementing before /start delta to prevent immediate repeat."
    # Execute-in-flight suppressor (7 followup, 2026-05-22). Same
    # contract as Path C's suppressor: when the agent is mid-Phase-4-execute,
    # deep code work can stale every liveness signal even though work IS
    # progressing. Sentinel within 4h (240min) — agent is genuinely working
    # in Phase 4, suppress recovery. Beyond 4h — Phase 4 itself is hung, let
    # runner-dead-check.sh's 6-condition gate decide. See execution-diary.py
    # _emit_phase_marker for the write/clear sites.
    local eif="$_adir/session/execute-in-flight"
    if [[ -f "$eif" ]]; then
        if [[ -z "$(find "$eif" -maxdepth 0 -mmin +240 2>/dev/null)" ]]; then
            return 0
        fi
    fi

    # Delegate to the canonical helper. Exit-code semantics (rb-762 propagated):
    #   0   = all 6 conditions met (dead) — proceed with recovery
    #   1   = at least one liveness signal positive (alive) — suppress
    #   2+  = script error — suppress (conservative, fail-open)
    MIND_AGENT="$agent" bash "$SCRIPT_DIR/runner-dead-check.sh" >/dev/null 2>&1
    local rc=$?
    [[ "$rc" -eq 0 ]] || return 0

    local cause="crashed runner: all 6 liveness signals confirm dead (runner-dead-check.sh rc=0)"
    _perform_recovery "$agent" "$cause"
}

# All three paths act on BOUND_AGENT only (per bound-agent gate above).
# Cross-agent iteration was removed 2026-05-14 — see audit comment block.

# Path B first — runs regardless of SessionStart source (state-corruption is
# source-independent; see CRITICAL block on the source gate above).
_check_state_corruption "$BOUND_AGENT" || true

# Path C — hung-autocompact detection. Source-independent (rationale in the
# function-level comment above _check_hung_autocompact).
_check_hung_autocompact "$BOUND_AGENT" || true

# Source gate for Path A only.
case "$SOURCE" in
    compact|resume)
        echo "[recovery-gate] source=$SOURCE -- continuation, skipping crashed-runner gate (state-corruption check already ran)" >&2
        exit 0
        ;;
esac

# Path A — heartbeat-based crashed-runner detection.
run_gate_for_agent "$BOUND_AGENT" || true

exit 0
