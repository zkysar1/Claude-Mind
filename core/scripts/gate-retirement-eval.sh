#!/usr/bin/env bash
# Thin wrapper around gate-retirement-eval.py — Phase 5 of the gate
# audit/retirement plan. Reads meta/gate-firings.jsonl + core/config/gates.yaml
# and emits per-gate recommendations.
#
# Usage:
#   bash core/scripts/gate-retirement-eval.sh [--days N] [--min-fires K]
#                                             [--gate <id>] [--output json|human]
#
# See script docstring for full rule taxonomy.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/gate-retirement-eval.py" "$@"
