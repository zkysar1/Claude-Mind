#!/usr/bin/env bash
# Health-regression detection sweep (Phase 2) — thin wrapper.
# See core/scripts/health-regression-check.py + core/config/conventions/health-ledger.md.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/health-regression-check.py" "$@"
