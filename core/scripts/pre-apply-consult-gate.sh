#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget; called once per Phase-4
# execution. Pre-apply consult gate for inherited cross-agent framework Applies.
# See pre-apply-consult-gate.py for full docs. Wired into aspirations-execute
# Phase 4 just before "Execute primary goal inline (host does ALL writing)".
#
# Posture: advisory, fail-open, always exits 0. The gate's value is the visible
# stdout banner; the LLM is expected to act on it (run retrieve.sh) BEFORE the
# first Edit/Write. Origin:  / rb-987 /  incident.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/pre-apply-consult-gate.py" "$@"
