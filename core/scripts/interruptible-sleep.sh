#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Interruptible sleep with 1s-granularity signal polling.
# Usage: interruptible-sleep.sh <seconds>
#
# Exit codes (semantics matter — Phase -0.5e branches on them):
#   0 = natural completion OR /stop signal detected (stop-loop, stop-requested)
#   1 = bad arguments
#   2 = wake-on-signal — a state change occurred that should break backoff
#       early. Specifically: blocker-cleared or pq-resolved was written.
#       The caller should clear blocked_sleep_until and run a light-precheck
#       pass instead of continuing to sleep. See aspirations/SKILL.md
#       Phase -0.5e ELSE branch for exit-2 handling.
#
# Signal files are CONSUMED when detected (one-shot). The writer is
# responsible for ensuring the signal is meaningful before setting it.
#
# WRITER STATUS (session-50  + ): both signals now wired.
#   blocker-cleared: written by blocker-recheck.py main() after _wm_set_blockers
#     when cleared>0.
#   pq-resolved: written by /respond skill after transitioning a pq to
#     status:answered or status:superseded (see .claude/skills/respond/SKILL.md).
# The session.py VALID_SIGNALS set includes both names; downstream writers only
# need to call session-signal-set.sh <name>.
source "$(dirname "${BASH_SOURCE[0]}")/_paths.sh"

SECONDS_TO_SLEEP="${1:?Usage: interruptible-sleep.sh <seconds>}"
STOP_FILE="$AGENT_DIR/session/stop-loop"
STOP_REQ_FILE="$AGENT_DIR/session/stop-requested"
BLOCKER_CLEARED_FILE="$AGENT_DIR/session/blocker-cleared"
PQ_RESOLVED_FILE="$AGENT_DIR/session/pq-resolved"
# Change 2: quiescence wake signals. Intended to break long quiescent sleeps
# early when external state changes so the agent doesn't sleep through
# actionable signal. WRITERS NOT YET WIRED — until a future PR teaches
# board.py, the email-poll path, and aspirations.py (on claim release) to
# set these files, the polling loop below never observes them and a
# quiescent sleep simply runs its full duration. The conservative default.
# Consumed (rm -f) on detect like the existing blocker-cleared / pq-resolved
# signals. Signal file names are the contract — renames require coordinated
# edits to: this file, core/scripts/session.py VALID_SIGNALS, and
# core/config/session-manifest.yaml (recovery_action: clear entry).
BOARD_ACTIVITY_FILE="$AGENT_DIR/session/board-activity"
EMAIL_RECEIVED_FILE="$AGENT_DIR/session/email-received"
CLAIM_RELEASED_FILE="$AGENT_DIR/session/goal-claim-released"

HEARTBEAT_TICK_SCRIPT="$(dirname "${BASH_SOURCE[0]}")/heartbeat-tick.sh"

# ─── Wake-signal classes (Magic Wand #2, alpha session-60) ───────────────
#
# WAKE_BLOCKER_SIGNALS (always exit 2 once outside debounce window):
#   blocker-cleared, pq-resolved, email-received
#   These represent state changes that genuinely unblock work — a goal
#   waiting on a blocker can now run, a pending question got answered, the
#   user replied. Sleeping past these wastes wall-clock the agent could be
#   spending on the unblocked path.
#
# WAKE_INFORMATIONAL_SIGNALS (consume the file but DO NOT exit 2 during
# QUIESCENCE_SLEEP=1):
#   board-activity, goal-claim-released
#   These represent partner activity. They're useful in normal backoff
#   (the partner just finished something, so re-evaluate the queue), but
#   during APPROVED quiescence they fragment the sleep without unblocking
#   anything we can act on. Quiescence is structurally user-gated by
#   definition — partner activity doesn't change that. Consuming the
#   file (one-shot semantics) is preserved so the next sleep starts clean.
#
# QUIESCENCE_SLEEP=1: caller signals this is a quiescence-approved sleep
# (B6.5 path). Set by aspirations-all-blocked when the quiescence gate
# returned rc=0. In that mode, only blocker-class signals exit 2;
# informational signals are demoted to "consume only" — the debounce
# machinery is bypassed entirely (no file read, no file write, no exit).
# The early-return at the top of handle_wake_signal enforces this;
# DO NOT move that branch below the debounce block — that would
# write to .wake-debounce-mtime even when no wake fired, polluting
# the debounce timeline for blocker-class signals later in the sleep.
# QUIESCENCE_SLEEP unset / 0: classic backoff sleep. All wake signals
# behave the same way (exit 2 outside debounce window).
#
# Wake-signal debounce (, prior incident ; tuned ):
# When the partner agent is bursty-active, _wake_signals.touch_peer_signals
# fans out to this agent's session dir on every board post + claim release.
# Without debounce, we wake on every fire (alpha session 60 06:09: 3
# consecutive 0-byte exit-2 sleeps within seconds). The debounce coalesces
# wake signals within WAKE_DEBOUNCE_SECONDS into a single wake. Set to 0
# to disable. Affects ALL wake signals (blocker-cleared,
# pq-resolved, board-activity, email-received, goal-claim-released);
# stop signals (stop-loop, stop-requested) bypass debounce entirely.
#
# DEFAULT SCALES WITH SLEEP DURATION ():
#   Short sleeps (<= 60s) → 30s floor (preserve existing short-sleep behavior).
#   Long sleeps (> 60s)   → SECONDS_TO_SLEEP / 2 (so a 1800s sleep tolerates
#                            up to one wake every 900s without exiting; a
#                            3600s sleep tolerates one every 1800s).
# Callers MAY override by exporting WAKE_DEBOUNCE_SECONDS=<N> before invoking.
# The bug shape this scaling fixes: idle-tick.sh + aspirations-all-blocked B7.2
# pass long sleeps without overriding the default — partner signal fanout from
# touch_peer_signals (board.py, aspirations.py:cmd_release) fires every 30s+
# during partner-active periods, exiting 2 on every fire. Pre-fix bravo
# iter-150 worked around manually with WAKE_DEBOUNCE_SECONDS=3600; this makes
# the workaround the default.
if [ -z "${WAKE_DEBOUNCE_SECONDS+x}" ]; then
  if [ "${SECONDS_TO_SLEEP:-0}" -gt 60 ]; then
    WAKE_DEBOUNCE_SECONDS=$(( SECONDS_TO_SLEEP / 2 ))
  else
    WAKE_DEBOUNCE_SECONDS=30
  fi
fi
WAKE_DEBOUNCE_FILE="$AGENT_DIR/session/.wake-debounce-mtime"

# First-fire stale-signal fix (, investigation , alpha
# 2026-05-03 + bravo 2026-05-07 incidents):
# When a signal file (especially board-activity from partner board-post fanout
# via _wake_signals.touch_peer_signals) is sitting in this agent's session/
# at sleep-start AND the debounce file is absent or its window expired, the
# very first iteration (i=0) of the for-loop calls handle_wake_signal, fails
# the debounce check, writes a new mtime, and exits 2 with empty output.
# That short-circuits any sleep > 0 to ~1 second of wall-clock and produces
# the "0-byte output exit 2" symptom alpha observed (3 consecutive bg sleeps
# at session 60 06:09). The  debounce-scaling fix sized the window
# but didn't address the absent-debounce or expired-debounce first-fire path.
#
# Fix: capture SLEEP_START before the for-loop. In handle_wake_signal,
# stat the signal file's mtime BEFORE removing it; if mtime < SLEEP_START
# the file is stale (predates this sleep — the wake event already happened
# in a prior iteration), so consume the one-shot file but DO NOT exit 2.
# Real wake events that arrive mid-sleep have mtime >= SLEEP_START and
# still exit 2 outside the debounce window (semantics preserved).
SLEEP_START=$(date +%s)

# Consume the signal file. If the previous wake was within
# WAKE_DEBOUNCE_SECONDS, return so the for-loop continues sleeping
# (signal still consumed — one-shot semantics preserved). Otherwise
# record the wake time and exit 2 so Phase -0.5e routes to the
# light-precheck branch. Fail-open on any error reading the debounce
# file: missing/corrupt content allows the wake.
#
# Second arg (Magic Wand #2, alpha session-60): signal class. Either
# "blocker" (always exit 2 outside debounce) or "informational" (in
# quiescence mode: consume + return; in normal mode: classic exit 2
# behavior). Default "blocker" preserves prior behavior for any caller.
handle_wake_signal() {
  local file_path="$1"
  local class="${2:-blocker}"

  # Capture file mtime BEFORE removal so we can distinguish stale
  # pre-existing signals from real wake events that arrived mid-sleep.
  # Fail-open: if stat fails (file vanished in a race between [ -f ] and
  # here), file_mtime=0 < SLEEP_START → treat as stale → return 0.
  local file_mtime
  file_mtime=$(stat -c %Y "$file_path" 2>/dev/null || echo 0)

  rm -f "$file_path"

  # Quiescence demotion: during a quiescence-approved sleep, partner
  # activity (informational class) does NOT break the sleep. The signal
  # is consumed (one-shot semantics) and the sleep continues.
  if [ "$class" = "informational" ] && [ "${QUIESCENCE_SLEEP:-0}" = "1" ]; then
    return 0
  fi

  # First-fire stale-signal fix (): if the signal file's mtime
  # predates SLEEP_START, the wake event happened before this sleep began.
  # Consume the one-shot file (preserved behavior) but DO NOT exit 2 —
  # there's no live wake event to break out for. Real wake events
  # arriving mid-sleep have mtime >= SLEEP_START and continue to the
  # debounce check below.
  if [ "$file_mtime" -lt "$SLEEP_START" ]; then
    return 0
  fi

  if [ "$WAKE_DEBOUNCE_SECONDS" -gt 0 ] && [ -f "$WAKE_DEBOUNCE_FILE" ]; then
    local last_wake now
    last_wake=$(cat "$WAKE_DEBOUNCE_FILE" 2>/dev/null)
    if [[ "$last_wake" =~ ^[0-9]+$ ]]; then
      now=$(date +%s)
      if (( now - last_wake < WAKE_DEBOUNCE_SECONDS )); then
        return 0
      fi
    fi
  fi
  date +%s > "$WAKE_DEBOUNCE_FILE"
  exit 2
}

# ─── Quiescence bg-job registration (7, 2026-07-10) ──────────────
# WHY: during RUNNING quiescence troughs the loop launches THIS script as a
# harness background task and ends its turn (quiescence-cycle-cache.py:193
# directive; all-blocked B7.2 same shape). stop-hook.sh fires on that
# turn-end and BLOCKs unless an ALLOW gate matches; its Gate 2.6 allows when
# `background-jobs.sh has-pending` is true. The bare bg-sleep was never
# registered in that ledger, so the carve-out never fired and the loop
# fast-spun troughs (~100x idle turns — rb-2541, charlie's 8-iteration
# confirmation; guard-967 pre-fix wording). Registering the sleep as a
# Tier-A background job fires the carve-out: the turn-end is ALLOWed, the
# sleep paces its full duration, and the harness bg-completion notification
# re-invokes the loop (Skill(aspirations) per the cache-HIT directive).
# SAFETY RAILS (per the 7 spec):
#   - Scoped to QUIESCENCE_SLEEP=1 — hot-path backoff sleeps (5s) never pay
#     the two python spawns (IRREDUCIBLY LOCAL budget preserved; the spawns
#     amortize over an 1800s quiescence sleep).
#   - No orphaned-row hazard: has-pending counts a row ONLY while its PID is
#     alive (background-jobs.py cmd_has_pending strictness, 2026-05-14
#     incidents) — a SIGKILLed sleep leaves a dead-PID row that gates
#     NOTHING; the EXIT/TERM/INT/HUP traps deregister on every catchable
#     exit path (natural end, stop-signal exit 0, wake exit 2, TERM kill).
#   - /stop responsiveness unchanged: stop-loop / stop-requested are polled
#     every 1s by the loop below; detection exits 0 → EXIT trap deregisters
#     → the pending row clears within ~1-2s of a /stop. No orphaned rows.
#   - PID namespace ( class, probed 2026-07-10): on Git Bash/MSYS,
#     $$ is an MSYS pid that Windows python's pid_alive CANNOT see (probe:
#     pid_alive(msys $$)=False while alive; pid_alive(winpid)=True) — so
#     register /proc/$$/winpid when it exists, else $$ (Linux fleet boxes).
#   - completion_check "true" is honest semantics: the job IS the process —
#     PID dead means the sleep finished (exit 0 = completed) — and satisfies
#     has-pending's completion-mechanism requirement. No --output-artifacts:
#     the sleep has no downstream file consumers (guard-333 N/A).
#   - Registration failure fails OPEN to pre-fix behavior (sleep runs
#     unregistered; that one cycle spins as before). stderr NOT suppressed
#     (rb-400); stdout silenced only to keep the bg task output clean.
_QS_BGJOBS_SCRIPT="$(dirname "${BASH_SOURCE[0]}")/background-jobs.sh"
_QS_JOB_ID=""
_qs_deregister() {
  if [ -n "$_QS_JOB_ID" ]; then
    bash "$_QS_BGJOBS_SCRIPT" deregister --id "$_QS_JOB_ID" >/dev/null || true
    _QS_JOB_ID=""
  fi
}
if [ "${QUIESCENCE_SLEEP:-0}" = "1" ]; then
  # Traps BEFORE register so no catchable-signal window exists where a
  # registered row outlives the process. Deregistering an unregistered id
  # is a harmless no-op ("not found").
  trap _qs_deregister EXIT
  trap 'exit 143' TERM
  trap 'exit 130' INT
  trap 'exit 129' HUP
  _QS_PID="$(cat "/proc/$$/winpid" 2>/dev/null || echo "$$")"
  if bash "$_QS_BGJOBS_SCRIPT" register \
        --id "quiescence-sleep-$$" --type quiescence-sleep --pid "$_QS_PID" \
        --completion-check "true" >/dev/null; then
    _QS_JOB_ID="quiescence-sleep-$$"
  fi
fi

for (( i=0; i<SECONDS_TO_SLEEP; i++ )); do
  [ -f "$STOP_FILE" ] && exit 0
  [ -f "$STOP_REQ_FILE" ] && exit 0
  # Wake-on-signal: caller wants backoff broken early because external state
  # changed. handle_wake_signal consumes the file and decides exit 2 vs
  # continue based on the debounce window + signal class.
  # Class "blocker" — always exit 2 outside debounce window.
  [ -f "$BLOCKER_CLEARED_FILE" ] && handle_wake_signal "$BLOCKER_CLEARED_FILE" blocker
  [ -f "$PQ_RESOLVED_FILE" ]     && handle_wake_signal "$PQ_RESOLVED_FILE"     blocker
  [ -f "$EMAIL_RECEIVED_FILE" ]  && handle_wake_signal "$EMAIL_RECEIVED_FILE"  blocker
  # Class "informational" — partner activity, demoted during quiescence.
  [ -f "$BOARD_ACTIVITY_FILE" ]  && handle_wake_signal "$BOARD_ACTIVITY_FILE"  informational
  [ -f "$CLAIM_RELEASED_FILE" ]  && handle_wake_signal "$CLAIM_RELEASED_FILE"  informational
  # Periodic heartbeat tick during long waits. Without this the heartbeat
  # mtime can cross runner_heartbeat.stale_minutes (default 30) during a
  # B7 cap sleep (1800s), letting recovery-gate.sh Condition 2 fire on the
  # next SessionStart. Tick every 60s. `|| true` so a heartbeat failure
  # never breaks the sleep, but stderr is intentionally NOT suppressed —
  # real errors must surface (see rb-400, heartbeat-tick.sh header).
  if (( i > 0 && i % 60 == 0 )); then
    bash "$HEARTBEAT_TICK_SCRIPT" >/dev/null || true
  fi
  sleep 1
done
