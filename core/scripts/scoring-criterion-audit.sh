#!/usr/bin/env bash
# Thin wrapper around scoring-criterion-audit.py — sister to
# gate-retirement-eval.sh. Scans the goal corpus (world + agent aspiration
# stores) and recommends dead / degenerate / sparse / skewed scoring-criterion
# input fields per core/config/scoring-criteria.yaml.
#
# Usage:
#   bash core/scripts/scoring-criterion-audit.sh
#        [--goals-source world|agent|both] [--agent-name NAME]
#        [--all-agents] [--min-goals K] [--criterion ID]
#        [--include-sample-values | --no-sample-values]
#        [--output json|human] [--no-log] [--self-test]
#
# See script docstring for full rule taxonomy.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/scoring-criterion-audit.py" "$@"
