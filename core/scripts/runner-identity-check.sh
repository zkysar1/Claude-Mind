#!/usr/bin/env bash
# runner-identity-check.sh — "Am I THE runner for this agent?" gate.
#
# Purpose: a session about to (re-)enter the aspirations loop body asks
# whether THIS session is the runner by comparing its own SID ($MIND_SID,
# injected into every Bash call by bash-agent-inject.py) against the agent's
# running-session-id. This is the per-SESSION identity gate the loop's
# Phase -1.5 was missing.
#
# WHY THIS EXISTS (the multi-runner gap, 2026-05-23)
# ---------------
# session-state-get.sh reads the SHARED agent-level agent-state file. EVERY
# session of an agent reads the same "RUNNING" the real runner wrote, so the
# loop's Phase -1.5 state check answers "is this AGENT running?" — NOT "am I
# THE runner?". The loop self-re-enters every iteration via Skill(aspirations).
# When two+ terminals run the same agent (e.g. a second /start auto-recovers
# during a stale-heartbeat window and takes over running-session-id while the
# original session keeps looping), the non-runner terminals pass the state
# check forever and iterate indefinitely, confusing the real runner with
# concurrent writes to shared world/agent state. stop-hook.sh Gate 0 ALLOWS a
# non-runner to stop, but nothing ever TELLS it to. This gate is the active
# eject point.
#
# SELF-HEALING: running-session-id holds exactly one SID (session-save-id.sh
# enforces the "/start is sole first-writer" invariant). So at most one session
# matches. When a new session claims runner, the old runner fails this check on
# its very NEXT iteration and ejects itself — convergence to exactly one runner
# with no manual intervention.
#
# Inputs (env, injected by bash-agent-inject.py on every Bash call):
#   MIND_SID   — this session's SID (the authoritative current SID)
#   MIND_AGENT — the bound agent
#
# Exit codes:
#   0 — I AM the runner, OR ambiguous (FAIL-OPEN) → caller CONTINUES the loop
#   1 — I am NOT the runner (definite mismatch)   → caller EJECTS (no re-entry)
#
# FAIL-OPEN is deliberate and load-bearing. Exit 1 fires ONLY on a definite
# mismatch: MIND_SID non-empty AND running-session-id non-empty AND they
# differ. Every ambiguity (either SID empty/unreadable, agent unset, paths
# missing) returns 0. Rationale: running-session-id holds exactly one SID, so
# if it is ever transiently empty (a brief /start race, fresh install, or a
# crashed runner before recovery-gate repairs it), failing CLOSED would make
# EVERY session mismatch and kill the legitimate runner too. The Phase -1.5
# state!=RUNNING check plus recovery-gate are the safety net for the empty-
# runner-sid window; this gate's ONLY job is to eject confirmed non-runners.
#
# DO NOT add a force/override flag. There is no legitimate scenario where a
# session whose SID differs from a live running-session-id should continue the
# loop — that IS the bug. A user who wants this terminal to be the runner runs
# /stop on the other terminal, or /start --recover here.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_paths.sh
source "$SCRIPT_DIR/_paths.sh"

MY_SID="${MIND_SID:-}"
AGENT="${MIND_AGENT:-}"

# Fail-open on any missing input — cannot make a definite determination.
[ -n "$MY_SID" ] || exit 0
[ -n "$AGENT" ] || exit 0

# tr -d '\r\n' — strip Windows CRLF so the comparison is byte-exact (same
# idiom as sid-collision-check.sh / session-save-id.sh). cat failure (missing
# file) is suppressed; RUNNER_SID becomes empty and the next check fail-opens.
RUNNER_SID=$(cat "$(agent_dir "$AGENT")/session/running-session-id" 2>/dev/null | tr -d '\r\n')

# Fail-open when running-session-id is empty/unreadable. See header rationale.
[ -n "$RUNNER_SID" ] || exit 0

if [ "$MY_SID" != "$RUNNER_SID" ]; then
    echo "[runner-identity-check] This session ($MY_SID) is NOT the runner for agent '$AGENT' (runner is $RUNNER_SID). Another terminal owns the autonomous loop — this session will exit the loop. Use /stop $AGENT or /start $AGENT --mode reader here." >&2
    exit 1
fi

exit 0
