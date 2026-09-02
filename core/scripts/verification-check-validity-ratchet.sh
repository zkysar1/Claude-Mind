#!/usr/bin/env bash
# verification-check-validity-ratchet.sh — advisory drift check with baseline
# ratchet for STRUCTURED-BUT-UNEVALUATABLE verification checks ().
#
# RATCHETED: `unevaluatable_structured` — dict-shaped checks[] entries that
# declare a type yet cannot be evaluated (unknown type after vocabulary
# normalization, or a known type missing a hard-required field). Every item is
# actionable by editing one check, and the count does not move when authors
# file prose checks.
#
# REPORTED but deliberately NOT ratcheted: structured-share and well-formedness.
# They are RATIOS, and audit-baselines.md forbids baselining a ratio; ratcheting
# structured-share would also re-create the anti-detector the seed re-measurement
# identified, since it falls on prose-filing volume with no defect introduced.
# See the .py docstring.
#
# Classifies STATICALLY and never calls predicate.evaluate() — that executes
# command_succeeds bodies and would run arbitrary commands across every goal.
# Exit 0 always unless VERIFY_LEARNING_DRIFT_HARD_GATE=1.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/verification-check-validity-ratchet.py" "$@"
