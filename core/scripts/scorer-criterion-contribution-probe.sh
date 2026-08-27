#!/usr/bin/env bash
# Thin wrapper around scorer-criterion-contribution-probe.py — sibling to
# scoring-criterion-audit.sh. Reports whether a goal-selector criterion moves
# the RANKING: fires / magnitude distribution / rank delta / noise-free rank.
#
# Distinct from its neighbours, and the distinction is the point (gap-033):
#   "is the term wired?"            -> a grep over goal-selector.py
#   "is its input field populated?" -> scoring-criterion-audit.sh
#   "does it move the ranking?"     -> THIS
#
# Takes exactly ONE goal-selector run and computes all four outputs by
# subtraction on that snapshot. Re-running the selector RE-ROLLS exploration
# noise (guard-3562), so a two-run diff cannot separate a term's effect from
# the noise lottery — the single run is a correctness requirement, not a
# performance choice. The noise band is read per-agent from the selector's own
# exploration_params (epsilon x noise_scale), never hardcoded.
#
# Read-only: never edits goal-selector.py, weights, or any goal. Writes no log
# (concurrent runs would race on a multi-KB single-line append).
#
# Usage:
#   bash core/scripts/scorer-criterion-contribution-probe.sh --criterion <name>
#        [--output json|human] [--top K]
#   bash core/scripts/scorer-criterion-contribution-probe.sh --list
#   bash core/scripts/scorer-criterion-contribution-probe.sh --self-test
#
# See script docstring for the subtraction identities and the composite-criterion
# caveat (guard-2412).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/scorer-criterion-contribution-probe.py" "$@"
