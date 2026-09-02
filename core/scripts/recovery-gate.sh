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
    # : which path fired (A|B|C|D) and the probe JSON it fired on. Both
    # ride into recovery-log.jsonl so a firing is never a bare conclusion: the
    # 2026-09-01 false kill could not be triaged from "all 6 signals confirm
    # dead" because the log carried the verdict without the evaluations or the
    # session that performed it.
    local path="${3:-?}"
    local evidence="${4:-}"
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
        # : this branch REFUSES to recover, so the box stays stuck and
        # only a user-only command can clear it. Announce it. Existence-tested
        # with -f, never -x (guard-1124: own-cloud mirror sync strips +x).
        # stderr is deliberately NOT redirected — the recorder's own
        # before/verdict/failure lines must reach this script's log, or an inert
        # notification is indistinguishable from a working one (guard-3737).
        if [[ -f "$SCRIPT_DIR/stop-reason-record.py" ]]; then
            python3 "$SCRIPT_DIR/stop-reason-record.py" \
                --path recovery-failed-permanent --agent "$agent" \
                --reason "$fail_count consecutive failed recoveries; refusing automatic retry. Last cause: $cause" \
                || echo "[recovery-gate] WARN: stop-reason recorder exited non-zero; $agent is stuck and may be unannounced." >&2
        else
            echo "[recovery-gate] WARN: stop-reason-record.py missing — $agent is stuck with nobody told." >&2
        fi
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

    # DDB runner-claim release with the crashed session's OLD on-disk token
    # (2026-07-07, bravo dual-runner incident follow-through). A crashed runner
    # leaves its DDB row RUNNING; local recovery flips only the LOCAL state, so
    # without this release the next /start on THIS box is held hostage by its
    # OWN stale row until OWNERSHIP_STALE_SECONDS elapses (~65 min post-
    # calibration). MUST run BEFORE manifest-clear below — runner-token is
    # recovery_action:clear, so the old token is gone after it. Token-
    # conditional and idempotent: if a peer machine already stale-broke and
    # re-claimed, the old token no longer matches and this is a no-op — it can
    # never steal a peer's claim. Fail-open: a DDB hiccup must never block
    # recovery (the acquire path's stale-break remains the fallback).
    MIND_AGENT="$agent" bash "$SCRIPT_DIR/runner-claim.sh" release --agent "$agent" \
        2>>"$CORE_ROOT/logs/recovery-gate-stderr.log" || true

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

    # : recovery SUCCEEDED, which means state is now IDLE and the
    # autonomous loop is NOT running. Recovering the state is not the same as
    # resuming the work — only the user-only /start does that — so without this
    # the box sits quietly IDLE and correct. Announced AFTER the state-set
    # succeeded so the claim in the email ("went IDLE") is true when sent.
    # stderr intentionally un-redirected (guard-3737); -f not -x (guard-1124).
    if [[ -f "$SCRIPT_DIR/stop-reason-record.py" ]]; then
        python3 "$SCRIPT_DIR/stop-reason-record.py" \
            --path recovery-gate-zombie --agent "$agent" \
            --reason "crashed runner recovered to IDLE: $cause" \
            || echo "[recovery-gate] WARN: stop-reason recorder exited non-zero; $agent recovered to IDLE possibly unannounced." >&2
    else
        echo "[recovery-gate] WARN: stop-reason-record.py missing — $agent recovered to IDLE with nobody told." >&2
    fi

    # Session-telemetry crash close (WP4, 2026-06-03). The runner crashed — no
    # graceful-stop D6.6 close ran — so finalize its durable telemetry record
    # with status=crashed, ended_reason=recovery-gate. The record lives at
    # $WORLD_DIR/telemetry/session-records/$agent/$sid_recorded.json; if WP1 never
    # wrote an open record, write_close synthesizes from binding.yaml
    # (wp1_missing=True) so even a crash-only session is captured. write_crash
    # forces goals_completed=-1 (the crashed session's true outcome is unknown).
    # Best-effort: a telemetry failure must NEVER abort recovery (|| true) and
    # the module itself never raises. python3 is sanctioned here — this .sh
    # sources _paths.sh (CLAUDE.md python-invocation rule); the import-a-pure-
    # library form (no `scripts/X.py <subcmd>`) is clean past
    # check-no-python-cli-fallback. guard-165: sid/agent/script-dir pass via
    # ENV, python source single-quoted. Only when a crashed SID was recorded.
    if [[ -n "$sid_recorded" ]]; then
        TSID="$sid_recorded" TAGENT="$agent" TSDIR="$SCRIPT_DIR" python3 -c 'import os,sys; sys.path.insert(0, os.environ["TSDIR"]); from _session_telemetry import write_crash; write_crash(sid=os.environ["TSID"], agent=os.environ["TAGENT"])' 2>/dev/null || true
    fi

    # Recovery notice — /prime surfaces this in the next session's PRIMED
    # block, then deletes the file. Single line; just human text.
    # Only written when state-set IDLE succeeded — never advertise success
    # for a failed recovery.
    mkdir -p "$_adir/session"
    printf '%s\n' "Auto-recovered: $cause" > "$_adir/session/recovery-notice"

    # Audit log — append-only JSONL. The entry is the durable VERDICT RECORD
    # (): action, path, the acting session (the SessionStart hook's own
    # SID — the session that performed the demotion, distinct from the runner
    # SID it demoted) and the full probe evaluation the path fired on. Readers:
    # recovery-yank-check.py (worker-side escalation), recovery-yank-reverse.sh
    # (a live reducer proving the yank false), and cross-box triage.
    local ts
    ts="$(date +%Y-%m-%dT%H:%M:%S)"
    local entry
    entry="$(_recovery_log_entry recover "$ts" "$agent" "$cause" "$sid_recorded" "$path" "$evidence")"
    if [[ -n "$entry" ]]; then
        printf '%s\n' "$entry" >> "$_adir/session/recovery-log.jsonl"
    fi

    # Cross-box marker ( part 3). recovery-log.jsonl and recovery-notice
    # are machine-local files a worker Body on ANOTHER box can never read, so
    # the same verdict is mirrored into this agent's team-state row, which IS
    # synced. Fail-open and quiet: a daemon hiccup must not turn a completed
    # recovery into a failed one.
    local marker
    marker="$(RG_TS="$ts" RG_CAUSE="$cause" RG_SID="$sid_recorded" RG_PATH="$path" RG_ACT="${SESSION_ID:-}" \
        python3 -c 'import json,os; print(json.dumps({"ts": os.environ["RG_TS"], "path": os.environ["RG_PATH"], "cause": os.environ["RG_CAUSE"][:240], "sid_recorded": os.environ["RG_SID"] or None, "acting_sid": os.environ["RG_ACT"] or None}))' 2>/dev/null || echo "")"
    if [[ -n "$marker" ]]; then
        bash "$SCRIPT_DIR/team-state-update.sh" --field "agent_status.$agent.last_recovery" --value "$marker" >/dev/null 2>&1 || true
    fi

    echo "[recovery-gate] RECOVERED $agent (path $path): $cause" >&2
}

# _recovery_log_entry <action> <ts> <agent> <cause> <sid_recorded> <path> <evidence-json>
# Builds one recovery-log.jsonl row. Everything passes through the ENVIRONMENT
# (guard-165): the evidence blob is arbitrary JSON and the cause is free text.
# `evidence` is embedded as an object when it parses, else as a string (never
# dropped — a malformed blob is still evidence of what the probe printed).
_recovery_log_entry() {
    RL_ACTION="$1" RL_TS="$2" RL_AGENT="$3" RL_CAUSE="$4" RL_SID="$5" RL_PATH="$6" RL_EVIDENCE="$7" \
    RL_ACTING="${SESSION_ID:-}" RL_SOURCE="${SOURCE:-}" \
    python3 -c 'import json, os
ev_raw = os.environ.get("RL_EVIDENCE") or ""
try:
    ev = json.loads(ev_raw) if ev_raw else None
except Exception:
    ev = ev_raw
print(json.dumps({
    "ts": os.environ["RL_TS"],
    "agent": os.environ["RL_AGENT"],
    "action": os.environ["RL_ACTION"],
    "path": os.environ["RL_PATH"],
    "cause": os.environ["RL_CAUSE"],
    "sid_recorded": os.environ["RL_SID"] or None,
    "acting_sid": os.environ["RL_ACTING"] or None,
    "source": os.environ["RL_SOURCE"] or None,
    "evidence": ev,
}))' 2>/dev/null || echo ""
}

# POST_RECOVERY_EDIT_OVERRIDE="User-directed Path-B self-heal + SID-loss forensics (2026-06-18, bravo investigation). Recovery-flow framework fix authored in (IDLE,autonomous) before the user re-runs /start. Bug 1: Path B demoted a DEMONSTRABLY-ALIVE runner to IDLE after it lost running-session-id mid-run, killing the loop (3rd occurrence). Bug 2: the SID-loss deleter was never captured. Audited to world/post-recovery-edits.jsonl."

# SID-loss forensic capture (g-398-investigation, 2026-06-18). 3rd occurrence
# of the bug class: alpha 2026-04-25, bravo 2026-05-11, bravo 2026-06-18 — all
# three left the original deleter unidentified because no watcher caught the
# present->absent transition in time (the watchdog RunningSidProbe ticks only
# at iteration-close; it sampled AROUND the mid-iteration deletion). Path B is
# the ONE moment we are guaranteed to observe the degenerate state with full
# context, yet it previously discarded everything and just recovered. This
# records the discriminating signals to <agent>/session/sid-loss-forensics.jsonl
# on EVERY Path-B trigger (self_heal OR recover), closing the sampling gap.
#
# Highest-value datum: runner_token_present. session-manifest-clear.sh removes
# running-session-id AND runner-token TOGETHER, so runner-token STILL PRESENT
# here proves the deleter was NOT a recovery manifest-clear — it is the
# unidentified upstream deleter (cross-agent clear, OneDrive sync, or a write
# race). runner-token ABSENT here means a manifest-clear (or graceful-stop,
# but that is gated out by the stop-requested check) ran first.
_capture_sid_loss_forensics() {
    local agent="$1" latest="$2" rdc_rc="$3" decision="$4"
    local _adir
    _adir="$(agent_dir "$agent")"
    mkdir -p "$_adir/session"
    local rtok="false"
    [[ -f "$_adir/session/runner-token" ]] && rtok="true"
    local ts
    ts="$(date +%Y-%m-%dT%H:%M:%S)"
    # guard-165: all values pass via argv; python source single-quoted.
    local entry
    entry="$(python3 -c "import json,sys; print(json.dumps({'ts': sys.argv[1], 'agent': sys.argv[2], 'event': 'sid_loss_observed', 'trigger': 'path_b_state_corruption', 'running_session_id_present': False, 'runner_token_present': sys.argv[3]=='true', 'latest_session_id_present': bool(sys.argv[4]), 'latest_session_id': sys.argv[4] or None, 'runner_dead_check_rc': int(sys.argv[5]) if sys.argv[5].lstrip('-').isdigit() else None, 'verdict': {'0':'dead','1':'alive'}.get(sys.argv[5],'error_or_unknown'), 'decision': sys.argv[6], 'source': sys.argv[7] or None}))" \
        "$ts" "$agent" "$rtok" "$latest" "$rdc_rc" "$decision" "${SOURCE:-}" 2>/dev/null || echo "")"
    if [[ -n "$entry" ]]; then
        printf '%s\n' "$entry" >> "$_adir/session/sid-loss-forensics.jsonl"
    fi
    local latest_present="false"; [[ -n "$latest" ]] && latest_present="true"
    echo "[recovery-gate] SID-LOSS forensics: agent=$agent runner_token_present=$rtok latest_present=$latest_present rdc_rc=$rdc_rc decision=$decision" >&2
}

# Self-heal audit (companion to _perform_recovery). A Path-B self-heal restores
# running-session-id from latest-session-id on a DEMONSTRABLY-ALIVE runner,
# re-arming the stop-hook safety net WITHOUT demoting agent-state (the loop
# stays RUNNING). Logged to recovery-log.jsonl with action=self_heal so the
# audit stream is unified with recovery events. Does NOT touch the
# recovery-failure-count (no recovery occurred) and does NOT write
# recovery-notice (that signal means "demoted to IDLE"; self-heal is the
# opposite — the loop was preserved).
_record_self_heal() {
    local agent="$1" sid="$2"
    local _adir
    _adir="$(agent_dir "$agent")"
    mkdir -p "$_adir/session"
    local ts
    ts="$(date +%Y-%m-%dT%H:%M:%S)"
    local entry
    entry="$(python3 -c "import json,sys; print(json.dumps({'ts': sys.argv[1], 'agent': sys.argv[2], 'action': 'self_heal', 'cause': 'running-session-id missing while RUNNING but runner DEMONSTRABLY ALIVE (runner-dead-check rc=1); restored from latest-session-id, loop preserved (no IDLE demotion)', 'sid_restored': sys.argv[3]}))" \
        "$ts" "$agent" "$sid" 2>/dev/null || echo "")"
    if [[ -n "$entry" ]]; then
        printf '%s\n' "$entry" >> "$_adir/session/recovery-log.jsonl"
    fi
    echo "[recovery-gate] SELF-HEAL $agent: restored running-session-id=$sid (runner alive; loop preserved, NOT demoted to IDLE)" >&2
}

# === PATH B: State-corruption detection (source-independent) ===
# Triggers on the degenerate combination state=RUNNING + running-session-id
# missing + no stop-requested. This intersection should not exist in a healthy
# session: /start atomically pair-writes running-session-id + state=RUNNING;
# aspirations-graceful-stop deletes running-session-id only AFTER state has
# transitioned out of RUNNING; session-save-id.sh on autocompact preserves the
# atomic relationship via the four-witness check. So if we observe (RUNNING,
# missing, no stop-requested) together, a partial recovery or unauthorized
# writer cleared the SID and the loop has lost its safety net (stop-hook hits
# gate=no-runner, no longer BLOCKs turn-end).
#
# DECISION (2026-06-18 self-heal, supersedes the old always-recover behavior):
# the degenerate state has TWO causes — (a) a genuinely crashed/zombie runner,
# or (b) a LIVE runner that lost running-session-id mid-run to the upstream
# SID-loss bug. Demoting to IDLE is correct for (a) but a productivity-
# destroying false positive for (b): the live session keeps executing the
# in-flight goal while its loop heartbeat is dead, then ends silently at the
# next pause. This was observed THREE times — the prior comment claimed "even a
# fresh heartbeat doesn't redeem a session that has no SID claim," but that was
# the rationale for the blunt kill, not a law: latest-session-id (preserved by
# the manifest) still holds the live SID, so a DEMONSTRABLY-ALIVE runner CAN be
# redeemed by restoring running-session-id rather than killed. The 2026-04-25
# alpha incident (user drove "continue" for hours, loop died at the first
# pause) is exactly the case self-heal now prevents.
#
# Liveness oracle: runner-dead-check.sh (the canonical 6-condition gate; note
# it does NOT itself read running-session-id, so the missing SID does not skew
# its verdict).
#   rc=1  -> definitively ALIVE -> SELF-HEAL (requires latest-session-id).
#   rc=0  -> definitively dead  -> recover (unchanged behavior).
#   rc=2+ -> probe error/unknown -> recover (preserve prior blunt behavior;
#           never resurrect a SID on an oracle error). Conservative bias:
#           recover-unless-proven-alive — a wrong recover is user-fixable via
#           /start, whereas a wrong self-heal of a dead runner would linger
#           (until Path A's 6-condition gate reaches rc=0 next SessionStart).
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

    # --- SELF-HEAL vs RECOVER (see DECISION block above) ---
    local latest=""
    if [[ -f "$_adir/session/latest-session-id" ]]; then
        latest="$(tr -d '\r\n[:space:]' < "$_adir/session/latest-session-id")"
    fi
    MIND_AGENT="$agent" bash "$SCRIPT_DIR/runner-dead-check.sh" >/dev/null 2>&1
    local rdc_rc=$?

    if [[ -n "$latest" && "$rdc_rc" -eq 1 ]]; then
        # Runner is DEMONSTRABLY ALIVE and we have a SID to restore. Re-arm the
        # safety net by restoring running-session-id atomically (.tmp + mv).
        # agent-state is NOT touched (stays RUNNING) — the live loop keeps
        # running. This does NOT call _perform_recovery (no IDLE demotion, no
        # manifest-clear), so the ordering invariant in
        # test_recovery_ordering_invariant.py is unaffected.
        _capture_sid_loss_forensics "$agent" "$latest" "$rdc_rc" "self_heal"
        if printf '%s\n' "$latest" > "$_adir/session/running-session-id.tmp" 2>/dev/null \
           && mv "$_adir/session/running-session-id.tmp" "$_adir/session/running-session-id" 2>/dev/null; then
            _record_self_heal "$agent" "$latest"
            return 0
        fi
        # Self-heal write failed (rare — local FS error). Fall through to
        # recovery so the degenerate state does not persist unaddressed.
        echo "[recovery-gate] SELF-HEAL write FAILED for $agent — falling through to recovery" >&2
        _perform_recovery "$agent" "state corruption: running-session-id missing, runner alive but self-heal write failed" B
        return 0
    fi

    # Genuinely dead, oracle error/unknown, or no latest-session-id to restore.
    _capture_sid_loss_forensics "$agent" "$latest" "$rdc_rc" "recover"
    local cause="state corruption: state=RUNNING, running-session-id missing, no stop-requested"
    _perform_recovery "$agent" "$cause" B
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
# during 1h 31m Phase 4 of  (no autocompact actually running).
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

    # Heartbeat gate: must be stale (no iteration is advancing it). Exactly
    # `stale` — a heartbeat file that EXISTS and aged out. `absent` (no file)
    # is inert here as everywhere (): a box with no heartbeat writer
    # would otherwise satisfy this gate permanently.
    local hb
    hb="$(MIND_AGENT="$agent" bash "$SCRIPT_DIR/heartbeat-stale.sh" 2>/dev/null || echo "fresh")"
    [[ "$hb" == "stale" ]] || return 0

    # Execute-in-flight suppressor ( followup, 2026-05-22). When
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

    # Roundtrip-completed suppressor (, 2026-09-02). The sentinel is
    # written ONLY at PreCompact, so a diary write NEWER than the sentinel
    # proves the loop executed post-compact work — the roundtrip COMPLETED
    # and the sentinel is a leftover, not a hang. The 15-min freshness window
    # above cannot see this case: under provider rate-limit backoff the
    # resumed loop can go quiet >15min (diary stale, heartbeat stale) while
    # the sentinel ages past 60min — every "stale" signal true, yet the
    # stalest artifact of all is the sentinel itself. Measured
    # 2026-09-01T07:01:34 (staging deployment): Path C demoted a LIVE
    # rate-limited loop to IDLE off a sentinel the resumed loop had outlived
    # by hours. session-save-id.sh's SID-match clear closes the common case
    # at the next SessionStart; this closes the window where Path C evaluates
    # first. Consume the sentinel here (self-heal — the diary already proves
    # completion, so its evidentiary value is spent; leaving it re-arms the
    # same false positive on every later evaluation) and suppress. The
    # cleanup-vs-recovery boundary above is intact: we are past
    # state==RUNNING, on the road to a recovery the diary just falsified.
    if [[ -f "$diary" && "$diary" -nt "$cif" ]]; then
        # Evidence + audit row through the shared verdict-record writer so the
        # suppression carries the same shape as a Path A veto (action=suppressed,
        # path=C) and recovery_yank.py — which treats action=recover, or a row
        # with NO action, as a yank — never mistakes it for one.
        local sh_sid="" sh_ev
        [[ -f "$_adir/session/running-session-id" ]] && sh_sid="$(tr -d '\r\n[:space:]' < "$_adir/session/running-session-id")"
        sh_ev="$(RG_CIF="$(stat -c %Y "$cif" 2>/dev/null || echo 0)" RG_DIARY="$(stat -c %Y "$diary" 2>/dev/null || echo 0)" RG_CIF_SID="$(tr -d '\r\n[:space:]' < "$cif" 2>/dev/null | head -c 64)" \
            python3 -c 'import json,os; print(json.dumps({"sentinel_consumed": True, "compact_in_flight_mtime_epoch": int(os.environ["RG_CIF"] or 0), "execution_diary_mtime_epoch": int(os.environ["RG_DIARY"] or 0), "compact_in_flight_sid": os.environ.get("RG_CIF_SID") or None}))' 2>/dev/null || echo "")"
        rm -f "$cif" 2>/dev/null || true
        local shentry
        shentry="$(_recovery_log_entry suppressed "$(date +%Y-%m-%dT%H:%M:%S)" "$agent" \
            "hung-autocompact kill SUPPRESSED: execution-diary newer than compact-in-flight (autocompact roundtrip completed after the sentinel was written); stale sentinel consumed, loop preserved" \
            "$sh_sid" C "$sh_ev")"
        if [[ -n "$shentry" ]]; then
            printf '%s\n' "$shentry" >> "$_adir/session/recovery-log.jsonl"
        fi
        echo "[recovery-gate] Path C SUPPRESSED for $agent: execution-diary newer than compact-in-flight (roundtrip completed); stale sentinel consumed" >&2
        return 0
    fi

    # stop-requested gate: graceful /stop owns the wind-down.
    # Exit-code semantics mirror Cond 2.5/Cond 4 (rb-762): rc=1 is the ONLY
    # "continue" code. rc=0 (signal exists) AND rc=2+ (script error) both
    # suppress recovery.
    MIND_AGENT="$agent" bash "$SCRIPT_DIR/session-signal-exists.sh" stop-requested >/dev/null 2>&1
    local sr_rc=$?
    [[ $sr_rc -eq 1 ]] || return 0   # 0=signal-set, 2+=error → both suppress recovery

    local cause="hung autocompact: compact-in-flight mtime >60min, heartbeat=$hb, state=RUNNING, no stop-requested, no execute-in-flight, diary stale and not newer than sentinel"
    _perform_recovery "$agent" "$cause" C
}

# === PATH D: Wedged-loop detection (heartbeat FRESH + unclosed phase, ) ===
#
# The 2026-07-04 own-cloud fleet-wedge ( failures #4/#5): a loop wedged
# at phase-0-precheck behind a _fileops.acquire_lock exception kept re-ticking
# the DDB heartbeat (FRESH) while diary writes stalled behind the wedged lock --
# so Paths A/C (which BOTH require heartbeat STALE) never fired, and "a fresh
# heartbeat masked the wedged loop." Path D is the mirror image: it fires ONLY
# when the heartbeat is FRESH but an execution-diary phase_start has been left
# unclosed past the wedge threshold (phase-wedge-check.py). Recovery (RUNNING ->
# IDLE) lets the next SessionStart re-enter cleanly, breaking the wedge that
# previously required the manual restart the incident documents.
#
# Fail-safe layering (why this cannot mis-recover a healthy agent):
#   - heartbeat==fresh is the discriminator from Path A/C (stale-heartbeat);
#   - the execute-in-flight suppressor (shared with Path A/C) protects a
#     genuinely-long phase-4-execute deep-code goal (<4h);
#   - the wedge threshold (runner_heartbeat.wedge_stale_minutes, 65min) is set
#     ABOVE runner_heartbeat.stale_minutes (60min) BY DESIGN (): a
#     HEALTHY non-phase-4 phase's local heartbeat ages WITH its phase_start, so
#     by the time phase_start crosses 65min the heartbeat is already >60min ->
#     STALE and the heartbeat-FRESH gate below suppresses FIRST. Only a genuine
#     wedge (heartbeat re-ticked fresh while the diary freezes) reaches the wedge
#     check. (At 45min < 60 a long precheck/state-update false-recovered a
#     healthy agent -- the  defect this invariant fixes.)
#   - the runner-age gate () suppresses when the CURRENT runner is
#     younger than the wedge threshold: a diary marker predating this runner is
#     INHERITED (/start neither rolls the diary nor stamps entries with a sid),
#     so it is not evidence about it. This is the one gate the 65-vs-60
#     calibration above cannot supply, because a fresh /start writes a NEW
#     heartbeat while inheriting an OLD phase_start — breaking the ages-together
#     coupling that whole invariant rests on.
#   - the assistant-turn liveness veto () suppresses when the runner
#     session emitted an assistant turn within assistant_turn_fresh_minutes,
#     read from the Claude Code transcript — or from the zakcode session document
#     when the runner is a zakcode session (2026-08-30: with only the transcript
#     consulted, a live zakcode reducer read as "no_transcript" and this path
#     recovered it to IDLE mid-select). This is the ONLY gate here not
#     derived from the execution diary, and it exists because no diary-derived
#     gate CAN reach the remaining false-fire class: a live user conversation
#     writes nothing to the diary, so an alive-but-off-loop agent and a wedged
#     one are indistinguishable to every other gate in this list.
#   - phase-wedge-check.py fails OPEN to no-recovery (rc!=0) on any error.
_check_wedged_loop() {
    local agent="$1"
    local _adir
    _adir="$(agent_dir "$agent")"
    [[ -d "$_adir/session" ]] || return 0

    # state==RUNNING — recovery-vs-cleanup boundary (same as Path C).
    local state
    state="$(MIND_AGENT="$agent" bash "$SCRIPT_DIR/session-state-get.sh" 2>/dev/null || echo "")"
    [[ "$state" == "RUNNING" ]] || return 0

    # Heartbeat gate — FRESH is the Path-D discriminator. A stale heartbeat is
    # Path A/C territory (dead/hung); a fresh heartbeat + a frozen diary is the
    # wedge signature. Default to "stale" on probe error so a broken heartbeat
    # probe suppresses Path D (conservative — fail toward no-recovery).
    local hb
    hb="$(MIND_AGENT="$agent" bash "$SCRIPT_DIR/heartbeat-stale.sh" 2>/dev/null || echo "stale")"
    [[ "$hb" == "fresh" ]] || return 0

    # Execute-in-flight suppressor (shared contract with Path A/C): a genuine
    # mid-Phase-4 deep-code goal can hold phase-4-execute open >45min while
    # progressing. Within 4h (240min) -> suppress. Beyond 4h -> Phase 4 itself
    # is hung, let the wedge check decide.
    local eif="$_adir/session/execute-in-flight"
    if [[ -f "$eif" ]]; then
        if [[ -z "$(find "$eif" -maxdepth 0 -mmin +240 2>/dev/null)" ]]; then
            return 0
        fi
    fi

    # Wedge detector: an execution-diary phase_start unclosed past the wedge
    # threshold. rc=0 wedged, rc=1 clean, rc=2 error. ONLY rc=0 proceeds --
    # rc=1 and rc=2 both suppress (the helper fails open to no-recovery).
    # python3 (not py -3): this script sources _paths.sh, so python3 is the
    # sanctioned form (CLAUDE.md python-invocation rule; matches the SOURCE
    # parse call earlier in this script).
    # Capture stdout (do NOT discard): the verdict already carries
    # threshold_minutes, so the runner-age gate below reuses the detector's own
    # resolved threshold instead of re-parsing aspirations.yaml in bash. One
    # source of truth for the threshold, and the gate can never drift from the
    # detector it guards. `local` is declared on its OWN line — `local x="$(...)"`
    # would make $? the exit status of `local`, not of the command substitution.
    local wedge_json wedge_rc
    wedge_json="$(MIND_AGENT="$agent" python3 "$SCRIPT_DIR/phase-wedge-check.py" 2>/dev/null)"
    wedge_rc=$?
    [[ "$wedge_rc" -eq 0 ]] || return 0

    # Runner-age gate () — a diary marker older than the CURRENT runner
    # cannot be evidence about that runner. /start does not roll or reset
    # execution-diary.jsonl and diary entries carry no sid, so check_wedge (a
    # pure diary detector, by contract) structurally cannot tell a marker left
    # by a PRIOR runner from one left by this one. Measured incident: a /start
    # 4m55s old was recovered because the inherited last marker — a phase_start
    # from the previous run — was already 70.9min against the 65min threshold.
    # A fresh /start writes a brand-new heartbeat (FRESH) while inheriting that
    # stale phase_start, so both Path-D gates read TRUE from signals of
    # DIFFERENT ages, defeating the  calibration (which assumes a
    # healthy phase's heartbeat ages WITH its phase_start — a coupling that a
    # stop/start boundary breaks).
    #
    # runner-token is the signal, NOT latest-session-id (which the goal
    # suggested) — measured 2026-08-05: /start writes all three of
    # running-session-id, latest-session-id and runner-token in one atomic
    # triple-write (start/SKILL.md:902), but session-save-id.sh's autocompact
    # path re-writes ONLY the first two (`for _SID_TARGET in latest-session-id
    # running-session-id`, L332-338). runner-token is in no refresh loop, so
    # its mtime is the runner's START time and nothing can reset it. Keying on
    # latest-session-id would let every autocompact reset the runner age and
    # suppress Path D for another full threshold — silently disabling recovery
    # for exactly the long-running sessions the 2026-07-04 fleet-wedge hit.
    #
    # Mirrors the execute-in-flight idiom above: `find -mmin +N` lists the file
    # only when it is OLDER than N minutes, so an EMPTY result means the runner
    # is younger than the threshold -> suppress. Missing token (abnormal —
    # /start always writes one; session-manifest-clear removes it only AFTER a
    # recovery) does NOT suppress: an undeterminable runner age must not
    # silently disable the genuine-wedge path, which is the costlier failure.
    local wedge_thresh rtok
    wedge_thresh="$(printf '%s' "$wedge_json" | python3 -c 'import json,sys; d=json.loads(sys.stdin.read() or "{}"); v=d.get("threshold_minutes"); print(int(float(v)) if v is not None else "")' 2>/dev/null)"
    rtok="$_adir/session/runner-token"
    if [[ -n "$wedge_thresh" && -f "$rtok" ]]; then
        if [[ -z "$(find "$rtok" -maxdepth 0 -mmin "+$wedge_thresh" 2>/dev/null)" ]]; then
            return 0
        fi
    fi

    # stop-requested gate: graceful /stop owns the wind-down (rb-762 rc=1-only;
    # 0=signal-set and 2+=error both suppress).
    MIND_AGENT="$agent" bash "$SCRIPT_DIR/session-signal-exists.sh" stop-requested >/dev/null 2>&1
    local sr_rc=$?
    [[ $sr_rc -eq 1 ]] || return 0

    # No Tier-A background job (loop legitimately blocked on external work).
    MIND_AGENT="$agent" bash "$SCRIPT_DIR/background-jobs.sh" has-pending >/dev/null 2>&1
    local bg_rc=$?
    [[ $bg_rc -eq 1 ]] || return 0

    # Assistant-turn liveness veto () — the LAST gate, and the only
    # one here not derived from the execution diary. Every gate above reads the
    # diary or the heartbeat, and a live user conversation moves NEITHER: the
    # diary records loop phases, not conversational turns, so "wedged" and
    # "alive-but-off-loop" are identical in diary+heartbeat space. That is why
    # the three prior narrowings ( calibration,  runner-age,
    #  liveness veto) were each followed by another false firing —
    # 5 across 4 agents, and that count is a FLOOR. Measured on foxtrot
    # 2026-08-14: 5h35m of TOTAL diary silence with the heartbeat fresh, while
    # a user conversation was live. The  veto did not malfunction;
    # there genuinely was no diary activity for it to see.
    #
    # rc semantics mirror phase-wedge-check.py above (0 = the condition holds):
    #   0 -> a recent assistant turn exists            -> SUPPRESS (return 0)
    #   1 -> no recent turn / nothing to say           -> proceed to recovery
    #   2 -> transcript present but unreadable         -> SUPPRESS (guard-487
    #        fail-closed-as-suppressed, which agrees with this function's
    #        fail-open-to-no-recovery contract rather than conflicting with it)
    # rc=1 deliberately covers "no running-session-id" and "no transcript":
    # ABSENCE IS NOT EVIDENCE OF LIVENESS. Measured cc-02 2026-08-15 — only the
    # box-RESIDENT agent has either, so treating absent as a suppression would
    # disable Path D everywhere except the one box, which is a deletion of
    # Path D rather than a narrowing. Same reasoning as the runner-token gate
    # above: an undeterminable signal must not silently disable the
    # genuine-wedge path, the costlier failure.
    #
    # Cost is a 400KB TAIL read, never a full parse — 1.6ms against a live
    # 281MB transcript on cc-02 (21ms/60MB on cc-08), inside the hook budget.
    # Stdout is CAPTURED, not discarded, so the age that caused a suppression
    # rides into the log line below (guard-3802: a suppressed alarm must carry
    # the severity it suppressed, or a suppression and a healthy run emit the
    # same silence).
    local turn_json turn_rc
    turn_json="$(MIND_AGENT="$agent" python3 "$SCRIPT_DIR/assistant-turn-freshness.py" 2>/dev/null)"
    turn_rc=$?
    # PROCEED TO RECOVERY ONLY ON A RECOGNISED NO-TURN VERDICT — every other
    # outcome SUPPRESSES (guard-487: a suppression gate fails CLOSED). This is an
    # ALLOWLIST and must stay one. rc=1 conflates "no recent turn" with "I could
    # not measure" (guard-2173), so no rc test can tell a verdict from a crash;
    # the probe prints JSON on every documented path, so a {..."verdict"...}
    # payload is the exact discriminator for "it actually ran".
    #
    # Two measured rounds produced this, and round 1 was too narrow. Round 1
    # () treated EMPTY stdout as the error: _paths/_dt import at module
    # level, outside every try where main()'s belt-and-braces cannot reach them,
    # and Python exits 1 on a module-level exception — landing a broken import on
    # the proceed-to-recovery branch. Measured cc-02 2026-08-15: one broken import
    # -> rc=1, 0 bytes, a LIVE agent recovered, traceback eaten by 2>/dev/null.
    # Round 2 — the fresh-eyes pass over round 1's OWN fix — measured that the
    # same class survived whenever the failure produced OUTPUT: a non-JSON message
    # at rc=49 and a truncated payload at rc=1 BOTH still recovered a live agent.
    # Emptiness was one shape of "could not measure", never the invariant.
    #
    # The `}` anchor is load-bearing: it is what rejects a truncated `{"verdict": `
    # write. Command substitution strips print()'s trailing newline, so a healthy
    # payload really does end in `}`. COUPLING, stated so it cannot rot silently:
    # this gate now depends on the probe's emit SHAPE, not only its rc — if
    # assistant-turn-freshness.py stops emitting a "verdict" key, Path D goes
    # permanently quiet (the fail-safe direction, but silent). Scenarios 2 and 4
    # of test_recovery_gate_assistant_turn.sh pin that shape from this side.
    #
    # The 2>/dev/null swallow deliberately STAYS: the stdout shape is sufficient
    # signal alone, and guard-2175 warns that making stderr load-bearing would
    # activate every latent defect feeding it on the SUCCESS path, where the
    # defect-detecting test cannot see it.
    if [[ "$turn_rc" -ne 1 || "$turn_json" != \{*'"verdict"'*\} ]]; then
        echo "[recovery-gate] Path D suppressed for $agent by assistant-turn liveness: ${turn_json:-<probe emitted nothing; died before its own emit>} (rc=$turn_rc)" >&2
        return 0
    fi

    # The probe's VERDICT rides into the cause, not just the conclusion. rc=1 has
    # two materially different readings that a bare "no recent assistant turn"
    # cannot separate: `no_recent_assistant_turn` means the transcript was READ
    # and showed nothing recent (a measurement), while `no_transcript` /
    # `no_running_session_id` mean there was nothing to read (an absence). Both
    # correctly proceed to recovery — absence is not liveness — but they are not
    # the same event, and the OFF-BOX case is always the absence one. Without the
    # verdict, every remote agent's recovery is textually identical to a measured
    # local one in recovery-log.jsonl, which is the durable fleet-wide instrument
    # a cross-box triage actually reads (see core/config/conventions/recovery-gate.md
    # "Which artifact to read"). Recording the conclusion without the evidence is
    # what makes a log unable to answer the question it was kept for.
    local cause="wedged loop: heartbeat=fresh, state=RUNNING, execution-diary phase_start unclosed past wedge threshold, runner older than wedge threshold, no stop-requested, no pending bg job, no recent assistant turn [probe: ${turn_json:-unavailable}] (g-328-23, g-328-45, g-115-6253)"
    _perform_recovery "$agent" "$cause" D "${turn_json:-}"
}

# === PATH A: Crashed-runner detection (6-condition gate, source-gated) ===
#
# The 6 conditions are EXTRACTED to `core/scripts/runner-dead-check.sh`
# (, 2026-05-19 — single source of truth). The helper checks:
#   (1)   agent-state == RUNNING
#   (2)   heartbeat-stale.sh returns "stale" — a file that EXISTS and aged out.
#         "absent" (no heartbeat file at all) is INERT and counts only beside a
#         positive death signal from runner-liveness-evidence.sh ()
#   (2.5) runner-recent-block.sh returns 1 (no BLOCK in last 5 min;
#          multi-signal — Claude Code 2.1.133 hook timeouts
#         make heartbeat alone a false-positive zombie signal)
#   (2.7) execution-diary.jsonl mtime older than DIARY_STALE_MINUTES
#         (default 15 min;  — diary survives hook interruptions
#         because phase-end writes BEFORE the LLM yields the turn)
#   (3)   stop-requested NOT set (rb-762 rc=1-only continue semantics)
#   (4)   background-jobs.sh has-pending returns 1 (no Tier-A jobs)
#   (5)   PRE-KILL RE-CHECK (): once (1)-(4) all hold, the helper runs
#         runner-liveness-evidence.sh and any positive LIFE signal — a live
#         runner process, a recent assistant turn, a live sidecar on this SID,
#         recent provider-retry activity — VETOES the kill. A multi-hour
#         provider rate-limit backoff makes (1)-(4) all true on a LIVE loop;
#         that is the 2026-09-01 false kill this condition closes.
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
    # Execute-in-flight suppressor ( followup, 2026-05-22). Same
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
    #   0   = all 6 conditions + the pre-kill re-check met (dead) — recover
    #   1   = at least one liveness signal positive (alive) — suppress
    #   2+  = script error — suppress (conservative, fail-open)
    # stdout is the JSON verdict record and is CAPTURED (bare, guard-4129) so
    # the firing — or the veto — is logged with every condition evaluation.
    local rdc_json
    rdc_json="$(MIND_AGENT="$agent" bash "$SCRIPT_DIR/runner-dead-check.sh" 2>/dev/null)"
    local rc=$?
    if [[ "$rc" -ne 0 ]]; then
        #  / guard-3385: when the SIX absence conditions all held and
        # ONLY the pre-kill re-check (condition 5) vetoed, record the veto. This
        # is the rate-limited-alive shape the 2026-09-01 kill would have been,
        # and a suppressed firing that leaves no trace is indistinguishable from
        # a gate that never evaluated. Every other rc=1 (some earlier condition
        # read alive) is the ordinary quiet path and is not logged.
        if [[ "$rc" -eq 1 ]] && RG_J="$rdc_json" python3 -c 'import json,os,sys
c = json.loads(os.environ["RG_J"]).get("conditions", {})
six = ("state_running","heartbeat_stale","no_recent_block","diary_stale","no_stop_requested","no_background_jobs")
sys.exit(0 if all(c.get(k) is True for k in six) and c.get("no_life_evidence") is False else 1)' 2>/dev/null; then
            local sup_sid=""
            [[ -f "$_adir/session/running-session-id" ]] && sup_sid="$(tr -d '\r\n[:space:]' < "$_adir/session/running-session-id")"
            local sup_entry
            sup_entry="$(_recovery_log_entry suppressed "$(date +%Y-%m-%dT%H:%M:%S)" "$agent" \
                "crashed-runner kill VETOED by the pre-kill re-check: all six absence conditions held but positive life evidence exists (rate-limit backoff / off-loop activity)" \
                "$sup_sid" A "$rdc_json")"
            [[ -n "$sup_entry" ]] && printf '%s\n' "$sup_entry" >> "$_adir/session/recovery-log.jsonl"
            echo "[recovery-gate] Path A SUPPRESSED for $agent: six absence conditions held, pre-kill re-check found life evidence — not recovering." >&2
        fi
        return 0
    fi

    local cause="crashed runner: all 6 liveness signals confirm dead and the pre-kill re-check found no life evidence (runner-dead-check.sh rc=0)"
    _perform_recovery "$agent" "$cause" A "$rdc_json"
}

# All three paths act on BOUND_AGENT only (per bound-agent gate above).
# Cross-agent iteration was removed 2026-05-14 — see audit comment block.

# Path B first — runs regardless of SessionStart source (state-corruption is
# source-independent; see CRITICAL block on the source gate above).
_check_state_corruption "$BOUND_AGENT" || true

# Path C — hung-autocompact detection. Source-independent (rationale in the
# function-level comment above _check_hung_autocompact).
_check_hung_autocompact "$BOUND_AGENT" || true

# Path D — wedged-loop detection (heartbeat FRESH + unclosed phase, ).
# Source-independent: a storage wedge can freeze the diary on any SessionStart
# source. Runs after Path C; Paths A/C key on STALE heartbeat, Path D on FRESH,
# so at most one path's heartbeat gate passes for a given liveness state.
_check_wedged_loop "$BOUND_AGENT" || true

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
