#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-tool-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# PostToolUse[*] hook — relay an UNCONSUMED stop-requested to the model at the
# tool-call boundary.
#
# The loop consumes stop-requested only at Phase -1.4, the top of an iteration.
# A goal execution spans arbitrarily many tool calls inside one iteration, so a
# stop raised mid-execution waits for the residual GOAL — unbounded — not the
# residual turn. Measured on a live capped vessel 2026-09-16 (): the
# 350 s grace expired with the signal 0 B unconsumed and the mind demonstrably
# alive. This hook makes the signal reachable one tool call after it is raised.
# Full rationale, and the delivery-vs-compliance limit, in stop-signal-relay.py.
#
# Thin bash wrapper. The Python body runs as a FILE, never a heredoc on
# `python -`, so sys.stdin carries the hook payload rather than the script body
# (same pattern as presence-tick.sh / iteration-close-reminder.sh).
#
# SAFETY: fail open on ANY error. Never exits non-zero. Empty stdout + exit 0 =
# "no additional context injected" per Claude Code's PostToolUse contract
# (guard-141).
set -uo pipefail
exec 2>/dev/null  # a per-tool-call hook must never produce terminal noise

source "$(cd "$(dirname "$0")" && pwd)/_paths.sh" 2>/dev/null || exit 0
source "$CORE_ROOT/scripts/_platform.sh" 2>/dev/null || true

SCRIPT_PATH="$CORE_ROOT/scripts/stop-signal-relay.py"
[ -f "$SCRIPT_PATH" ] || exit 0

# STORAGE_BACKEND=local: this hook reads ONLY machine-local session files under
# agents/<agent>/session/. Inheriting the box's remote-storage default makes
# every fire instantiate the remote backend (client + credential setup) —
# measured ~0.16s of pure init on a fast box for the sibling PostToolUse[*]
# hook, enough to approach the hook timeout on a slow one ().
exec env STORAGE_BACKEND=local python3 "$SCRIPT_PATH"
