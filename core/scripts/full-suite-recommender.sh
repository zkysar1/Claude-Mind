#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget; called once per Phase-4
# close. Advisory full-suite test recommender. See
# full-suite-recommender.py for full docs. Wired into aspirations-execute
# Phase 4 just AFTER "Execute primary goal inline" and BEFORE
# phase_4_completed_at, so the LLM sees the recommendation before Phase 5
# verify claims "all tests pass".
#
# Posture: advisory, fail-open, always exits 0. The gate's value is the
# visible stdout banner; the LLM is expected to act on it BEFORE Phase 5.
#
# Origin:  /  /  (testSymmetry regression that
# targeted-only tests missed but full-suite would have caught).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/full-suite-recommender.py" "$@"
