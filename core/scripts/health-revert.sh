#!/usr/bin/env bash
# Tiered, calibration-gated self-health revert (Phase 3) — thin wrapper.
# See core/scripts/health-revert.py + core/config/conventions/health-ledger.md §11.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/health-revert.py" "$@"
