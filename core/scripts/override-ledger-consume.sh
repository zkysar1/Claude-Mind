#!/usr/bin/env bash
# Thin wrapper around override-ledger-consume.py ().
# Aggregates world/override-bypass-ledger.jsonl by gate and reason-cluster,
# producing per-gate override counts/rates and threshold-adjustment hints.
#
# Distinct from gate-stats.sh (passive dashboard across full telemetry):
# this script focuses on the override ledger for downstream automation.
#
# Usage:
#   bash core/scripts/override-ledger-consume.sh [--days N] [--top K]
#                                                [--gate <id>] [--output json|human]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/override-ledger-consume.py" "$@"
