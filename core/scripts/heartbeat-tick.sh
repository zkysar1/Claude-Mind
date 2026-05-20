#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Per-iteration heartbeat tick: local file mtime + cross-agent team-state write.
#
# Both heartbeats advance together — single source of truth for "this agent is
# alive at NOW." Called once per aspirations-loop iteration from Phase -0.5,
# once from /start autonomous seed, and every 60s from interruptible-sleep.sh
# during long B7 waits so mtime can't cross the staleness threshold.
#
# Liveness model: pure mtime. heartbeat-stale.sh compares file age against
# runner_heartbeat.stale_minutes and returns fresh/stale. No writer-identity
# check — the heartbeat-tick cadence during waits + the single-caller invariant
# (aspirations loop + /start + interruptible-sleep) is the liveness contract.
#
# Why a script and not inline in aspirations/SKILL.md:
# SKILL.md pseudocode is loaded into LLM context at prime/boot — changes don't
# take effect for currently-running loops until autocompact reloads or
# /start --recover. Scripts re-exec on every call, so the LATEST version on
# disk is always the one that runs. See rb-399.
#
# Cross-agent writes (team-state.yaml agent_status.<self>.*):
#   - last_active (ISO timestamp) — partner liveness signal
#   - live_phase (diary-tail derived) — informational, see live-phase-emit.sh
#   - Read by partner agents via team-state-read.sh; used by /prime, status
#     displays, fresh-eyes-review, and anything that infers partner liveness
#     or current activity.
#   - Fail-open via `|| true` — a team-state write failure (lock contention,
#     disk full) must NEVER block the iteration. The local heartbeat touch is
#     unconditional; if THAT fails the filesystem is broken.
#
# stderr is NOT suppressed (no 2>/dev/null) — see rb-400. Real errors must
# surface to the LLM-readable log so silent failures can't hide.

set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"

# --- State gate (g-115-NEW, 2026-05-13) -------------------------------------
# Refuse to tick when agent-state=IDLE. Without this gate, a stop-hook-
# cancelled loop death + recovery-gate's RUNNING→IDLE flip leaves callers
# (e.g. /aspirations Phase -0.5) free to write a fresh heartbeat AGAINST an
# IDLE state. End state: agent-state=IDLE + runner-heartbeat=fresh +
# loop-active=set — exactly the `heartbeat_without_running` desync that
# session-manifest.yaml already flags as a warning. This gate prevents the
# write at the source instead of catching it after the fact.
#
# Canonical incident: alpha session cbb27ab3 on 2026-05-13. After hung-
# autocompact recovery flipped state RUNNING→IDLE at 05:17, the LLM resumed
# via "continue" and batched 4 Bash calls (state-get + stop-check +
# loop-active-set + heartbeat-tick) into one. By the time the state-get
# result arrived, loop-active was set + heartbeat was ticked against IDLE.
#
# Bypass: --bypass-state — used by /start IDLE→RUNNING transition where
# heartbeat-tick MUST seed mtime BEFORE state-set RUNNING per rb-323/guard-403
# to close the observer-probe race (state=RUNNING with stale heartbeat).
# Only /start (IDLE Step 3 + UNINITIALIZED Phase C8) is authorized to pass it.
#
# Why state=IDLE is the only bad case: state=RUNNING is the normal autonomous
# path (per-iteration tick + interruptible-sleep B7 tick); state=UNINITIALIZED
# happens only on the very first /start of an agent (heartbeat file is absent
# anyway — no desync possible); state=NO_AGENT means MIND_AGENT is unset and
# _paths.sh would have already exited.
if [ "${1:-}" != "--bypass-state" ]; then
    # rb-400 — no 2>/dev/null (silent-boundary anti-pattern). `|| true` falls
    # open: a hypothetical state-get crash → empty STATE → STATE != "IDLE" →
    # tick proceeds. Aspirations Phase -1.5 is the primary gate; this is
    # defense-in-depth, so fail-open is correct here.
    STATE=$(bash "$(cd "$(dirname "$0")" && pwd)/session-state-get.sh" || true)
    if [ "$STATE" = "IDLE" ]; then
        echo "heartbeat-tick: REFUSED — agent-state=IDLE. Caller is writing a heartbeat against a non-running agent (this is the alpha-2026-05-13 desync class). Use --bypass-state from /start IDLE→RUNNING only." >&2
        exit 2
    fi
fi

# Pure mtime heartbeat. Content is irrelevant — only the file's mtime is read
# by heartbeat-stale.sh. DO NOT reintroduce a writer-identity check here (prior
# token-based scheme was removed 2026-04-21 after pure-mtime proved sufficient
# once interruptible-sleep.sh began ticking during B7 waits).
#
# DO NOT add a running-session-id refresh here either. running-session-id and
# latest-session-id must be written atomically as a pair under runner-legitimacy
# gates — see session-save-id.sh:127-142. Refreshing one without the other from
# heartbeat-tick desyncs the pair and re-introduces the /stop runner/observer
# misclassification class (2026-04-20 hang). Autocompact rotation is handled
# upstream by session-save-id.sh's four-witness gate. If that gate fails, fix
# the gate — do not add a defensive write here.
touch "$AGENT_DIR/session/runner-heartbeat"

bash "$(dirname "$0")/team-state-update.sh" \
    --field "agent_status.$MIND_AGENT.last_active" \
    --value "\"$(date +%Y-%m-%dT%H:%M:%S)\"" || true

# Live_phase mirror from execution-diary tail. The `|| true` below is the
# SINGLE fail-open boundary for this signal — DO NOT add internal fallbacks
# inside live-phase-emit.sh. Failures there must crash loudly so stderr
# surfaces the problem and the next tick retries naturally.
bash "$(dirname "$0")/live-phase-emit.sh" || true
