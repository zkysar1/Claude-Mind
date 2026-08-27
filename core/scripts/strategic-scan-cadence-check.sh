#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / precheck critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Cadence gate for /aspirations-strategic-scan — the strategic-scan precheck safety-net ().
#
# Exit 0 → the strategic scan should fire this iteration (last_strategic_scan is
#          unset OR older than strategic_scan.hours_cadence).
# Exit 1 → noop (cadence not crossed, OR any read/parse error).
#
# Fail-open on ANY error: the cadence gate must never block the loop (guard-424:
# errors print to stderr, never silently swallowed).
#
# Params in: core/config/aspirations.yaml       -> strategic_scan.hours_cadence
# State in:  <agent>/session/working-memory.yaml -> last_strategic_scan
#
# WHY (): orchestrator Phase 1.5 is an LLM-enumerated conditional and
# NOTHING in bash read last_strategic_scan for a cadence decision, so the ritual
# starved — measured 19.5h against the 4h cadence (alpha, cc-04, 2026-08-02).
# A Phase-1.5-LOCAL bash gate cannot fix that: a bash call inside an LLM-skippable
# block inherits the skippability, and the digest already proves it (the
# phase-start/phase-end diary markers at L111/L113 exist to witness the phase and
# produced 0 markers in 178 diary lines on a box where the stamp was 3.9h fresh).
# Only a gate reached from an unconditional call helps — hence the Phase 0.5e
# cadence battery, via core/scripts/_cadence_registry.py.
#
# Covers the TIME trigger only; goal_cadence + recurring_settling stay at Phase
# 1.5 and can only make the scan fire SOONER (any fire stamps the slot and resets
# every trigger), so the time bound here is the binding constraint on starvation.
# See the .py docstring for the full scope statement (guard-1760).
#
# READ-ONLY on the slot (guard-155) — aspirations-strategic-scan Phase S5 stays
# the single writer. Idempotent with Phase 1.5 via that shared stamp, exactly as
# evolution pairs Phase 8.8 with precheck Phase 0.5j via last_evolution_at_time.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/strategic-scan-cadence-check.py" "$@"
