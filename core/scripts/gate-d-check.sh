#!/usr/bin/env bash
# domain-leak-exempt: Gate D master flag-check — references the GATE_D_* flag
# namespace and the gate-d-kill kill-file path by design (functional experiment seam).
#
# gate-d-check.sh — Gate D master flag check (DORMANT until omni flips the flag).
# Binding spec: Gate D methodology §4.4 / R4 (RATIFIED 2026-06-10) + Addendum A
# amendment 4 (omni, 2026-06-11: agent allowlist — settings.json env is repo-wide,
# but the pilot is alpha-only; the allowlist is the isolation mechanism). Returns
# "on" iff ALL THREE conditions hold:
#   (1) GATE_D_ENABLED == "true"  — set ONLY by omni; DEFAULT OFF (empty), AND
#   (2) the kill-file core/config/gate-d-kill is ABSENT — R4 emergency stop, AND
#   (3) $AYOAI_AGENT appears in core/config/gate-d-agents (one name per line) —
#       pilot rollout control; file managed ONLY by omni. Missing file or unset
#       agent → off.
# Prints "on" or "off" on stdout; ALWAYS exits 0. Fail-closed: any ambiguity → off.
# GATE-INTEGRITY (methodology 9.5): agents MUST NOT set GATE_D_ENABLED and MUST
# NOT edit gate-d-agents.
set -uo pipefail

# Resolve PROJECT_ROOT from this script's location (core/scripts/ -> repo root).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
KILL_FILE="$PROJECT_ROOT/core/config/gate-d-kill"
AGENTS_FILE="$PROJECT_ROOT/core/config/gate-d-agents"

if [[ "${GATE_D_ENABLED:-}" == "true" && ! -f "$KILL_FILE" \
      && -n "${AYOAI_AGENT:-}" && -f "$AGENTS_FILE" ]] \
   && grep -qx "${AYOAI_AGENT}" "$AGENTS_FILE" 2>/dev/null; then
  echo "on"
else
  echo "off"
fi
