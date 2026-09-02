#!/usr/bin/env bash
# fleet-capacity-snapshot.sh — per-agent fleet CAPACITY snapshot in one call
# (routed / AVAILABLE / HIGH / in_flight). Deliberately does NOT invoke
# goal-selector.sh: that script is non-idempotent (guard-2261) and stochastic
# (guard-3562), so a per-agent loop would mutate the very lanes it observes.
# See core/scripts/fleet-capacity-snapshot.py for the full rationale.
# Exit 1 ONLY when the guard-2596 conservation check fails.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/fleet-capacity-snapshot.py" "$@"
