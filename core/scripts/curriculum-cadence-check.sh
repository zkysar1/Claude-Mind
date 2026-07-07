#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / precheck cadence gate. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Cadence gate for the curriculum re-evaluation ritual (1).
#
# Exit 0 → curriculum-evaluate should re-run this iteration (interval elapsed
#          OR never evaluated).
# Exit 1 → noop (interval not yet elapsed OR config disabled/absent OR error
#          reading state).
#
# Fail-open on ANY error: the cadence gate must never block the loop.
# Parameters in: core/config/aspirations.yaml → curriculum_cadence.*
# State in:      <agent>/session/working-memory.yaml → last_curriculum_eval slot
#
# The precheck phase (not this script) runs curriculum-evaluate.sh, updates
# last_curriculum_eval AFTER a successful evaluation, and routes any qualifying
# promotion through /curriculum-gates (guard-33 email-confirmed). This script
# only reads state — see curriculum-cadence-check.py for the rationale.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/curriculum-cadence-check.py" "$@"
