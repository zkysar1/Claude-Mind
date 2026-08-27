#!/usr/bin/env bash
# infra-health.sh — Infrastructure health check and tracking
#
# Probes infrastructure components and records results in <agent>/infra-health.yaml.
# Thin wrapper around infra-health.py.
#
# Usage:
#   infra-health.sh check <component>         # Probe one component
#   infra-health.sh check-all                 # Probe all components
#   infra-health.sh status                    # Read current health state
#   infra-health.sh stale [--hours N]         # List stale components (default: 2h)
#
# Components: defined in <agent>/infra-health.yaml

set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/infra-health.py" "$@"
