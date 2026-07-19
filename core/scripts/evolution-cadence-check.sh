#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / precheck critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Cadence gate for /aspirations-evolve — the evolution-cadence precheck safety-net (0).
#
# Exit 0 → evolution should fire this iteration (last_evolution_at_time is unset
#          OR older than maintenance_cadence.evolution.hours_cadence, AND the
#          per-session evolution cap is not reached).
# Exit 1 → noop (cadence not crossed, session cap reached, OR any read/parse error).
#
# Fail-open on ANY error: the cadence gate must never block the loop (guard-424:
# errors print to stderr, never silently swallowed).
#
# Params in: core/config/aspirations.yaml     -> maintenance_cadence.evolution.hours_cadence
#            core/config/evolution-triggers.yaml -> global.max_evolutions_per_session
# State in:  <agent>/session/working-memory.yaml -> last_evolution_at_time + loop_state.evolutions
#
# WHY (0): the Phase 8.8 evolution cadence tick is bypassed by
# recurring-close.sh on recurring-heavy sessions, starving evolution (~99h vs
# the 12h cadence, observed 2026-07-15). This is the precheck-side net — it fires
# regardless of close path, mirroring the fresh-eyes / felt-sense /
# health-regression / curriculum cadence sweeps. Idempotent with Phase 8.8 via
# the shared last_evolution_at_time stamp. Invoked from precheck Phase 0.5j.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/evolution-cadence-check.py" "$@"
