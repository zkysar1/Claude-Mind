#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# agent-watchdog.sh — wrapper for agent-watchdog.py.
#
# Per-agent session observability probes. Reads MIND_AGENT from env
# (required). One invocation = one tick: load prev state, run each probe's
# check(), log transitions to core/logs/watchdog-<agent>.jsonl, save new
# state to <agent>/session/watchdog-prev-state.json.
#
# Canonical invocation: iteration-close.sh productivity-check phase calls
# agent-watchdog.py --tick directly (not via this wrapper). This wrapper is
# for ad-hoc human inspection:
#   bash core/scripts/agent-watchdog.sh         # one tick (default)
#   bash core/scripts/agent-watchdog.sh --once  # snapshot all probes, no diff
#
# Earlier the watchdog ran as a detached daemon via watchdog-start.sh /
# watchdog-stop.sh. That model was retired because nohup+disown semantics
# on Git Bash for Windows don't reliably keep child processes alive past
# parent-shell exit. The current periodic-tick model is cross-platform.
# See agent-watchdog.py's top-level docstring for full design notes.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
exec python3 "$CORE_ROOT/scripts/agent-watchdog.py" "$@"
