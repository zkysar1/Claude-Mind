#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Cadence gate for /felt-sense-checkin.
#
# Exit 0 → the ritual should fire this iteration (goal-count cadence crossed).
# Exit 1 → noop (cadence not yet crossed OR config disabled OR error).
#
# Fail-open on ANY error: the cadence gate must never block the loop.
# Parameters in: core/config/aspirations.yaml → felt_sense.*
# State in:      <agent>/session/working-memory.yaml → last_felt_sense_checkin slot
#
# The skill itself (not this script) updates last_felt_sense_checkin AFTER
# a successful fire. This script only reads state.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/felt-sense-cadence-check.py" "$@"
