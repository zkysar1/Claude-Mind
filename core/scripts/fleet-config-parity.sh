#!/usr/bin/env bash
# Fleet per-node CONFIGURATION drift detection () — thin wrapper.
# Closes the observability gap that liveness probes structurally cannot see:
# a misconfigured node is ALIVE, so the watchdog reports it healthy and is correct.
# Compares every node in core/config/fleet-manifest.yaml and emits PASS / DRIFT.
# Env vars are compared by key NAME only — no secret value is read or emitted
# (prove it with --self-test). See core/scripts/fleet_config_parity.py for full docs.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/fleet_config_parity.py" "$@"
