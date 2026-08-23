#!/usr/bin/env bash
# learning-routing-audit.sh — audit learning-store cross-references and doc pointers.
# Detects drift: dangling cross-refs, broken doc pointers, store-catalog mismatch.
# Exit codes: 0 clean, 1 drift detected, 2 input error.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/learning-routing-audit.py" "$@"
