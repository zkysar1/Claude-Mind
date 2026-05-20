#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Cadence gate for /fresh-eyes-review (and siblings program/tree).
#
# Exit 0 → the ritual should fire this iteration (goal-count cadence crossed).
# Exit 1 → noop (cadence not yet crossed OR config disabled OR error
#          reading state).
#
# Fail-open on ANY error: the cadence gate must never block the loop.
# Parameters in: core/config/aspirations.yaml → fresh_eyes_review.*
# State in:      <agent>/session/working-memory.yaml → last_fresh_eyes_review slot
#
# The skill itself (not this script) updates last_fresh_eyes_review AFTER
# a successful fire. This script only reads state. Cadence is purely
# goal-count-based — see fresh-eyes-cadence-check.py for the rationale.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/fresh-eyes-cadence-check.py" "$@"
