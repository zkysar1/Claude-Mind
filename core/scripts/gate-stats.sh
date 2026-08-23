#!/usr/bin/env bash
# Thin wrapper around gate-stats.py — Phase 6 of the gate audit/retirement
# plan. Read-only telemetry dashboard. Complements gate-retirement-eval.sh
# (which prescribes actions) with passive visibility into firings, override
# rates, trigger histograms, and bulk-override audit correlation.
#
# Usage:
#   bash core/scripts/gate-stats.sh [--days N] [--top K] [--gate <id>]
#                                   [--output json|human]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/gate-stats.py" "$@"
