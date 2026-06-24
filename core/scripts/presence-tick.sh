#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# PostToolUse hook — record tool call to world/presence/<agent>.jsonl for cross-agent visibility.
# Visibility-only: fail-silent on ALL errors. MUST NEVER block tool execution.
# Schema: {ts, agent, tool, goal_id, phase, seq, session_id}
# Storage budget: ~140 bytes/entry × 200 calls/h ≈ 672KB/day, truncated to 1000 entries
# at session-end via aspirations-consolidate.
#
#  — Magic-wand framework improvement #1 (cross-agent presence). Polish layer
# over per-iteration team-state.last_active heartbeat: per-tool-call visibility so
# bravo can see alpha is mid-execution on a 10-minute goal without waiting for the
# next heartbeat tick. Lock contention: zero cross-agent (each agent owns its own file).
#
# Activation: LIVE -- bound by .claude/settings.json PostToolUse hook with
# matcher='*' (settings.json ~L360-367; fires on every tool call). The
# "deny-list-blocked / dormant" note is STALE (8, 2026-06-20): the
# hook is active on this install. presence-tick.py bounds its stdin read so
# an orphaned pipe cannot hang the process (was 120-129h py.exe orphans).
set -uo pipefail
exec 2>/dev/null  # silence stderr — visibility hook must never produce noise

source "$(cd "$(dirname "$0")" && pwd)/_paths.sh" 2>/dev/null || exit 0
source "$CORE_ROOT/scripts/_platform.sh" 2>/dev/null || true

# Delegate to presence-tick.py — heredoc-vs-stdin-vs-script pitfall avoided
# by running .py file directly so sys.stdin gets the hook JSON payload, not
# the script body. (The earlier all-bash heredoc approach had python3 read
# the heredoc as its script AND lost the hook payload.)
exec python3 "$CORE_ROOT/scripts/presence-tick.py"
