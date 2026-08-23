#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / productivity-check tick. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# monitor-tick.sh -- wrapper for monitor-tick.py (FW-1b, ).
#
# Runs ENABLED demoted monitoring probes (allowlist in core/config/monitor-probes.yaml,
# ships empty = inert) at their own interval_hours. A clean probe records a last-run
# marker and emits nothing; a tripped probe is converted to ONE deduped goal via
# monitor-finding-convert.py. Reads MIND_AGENT from env to resolve the per-agent
# state file <agent>/session/monitor-tick-state.json.
#
# Canonical invocation: iteration-close.sh productivity-check phase calls
# monitor-tick.py --tick directly (beside agent-watchdog --tick), same LOCAL-tick
# pattern (no daemon, no cloud cron -- guard-441). This wrapper is for ad-hoc
# human inspection:
#   bash core/scripts/monitor-tick.sh           # one tick (default)
#   bash core/scripts/monitor-tick.sh --json    # one tick, print the result JSON
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
exec python3 "$CORE_ROOT/scripts/monitor-tick.py" "${@:---tick}"
