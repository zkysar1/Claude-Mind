#!/usr/bin/env bash
# Phase 8e Decision Rules append wrapper.
# Dispatches to decision-rules-append.py. See aspirations-state-update/SKILL.md
# Step 8e and core/config/conventions/decision-rules.md.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/decision-rules-append.py" "$@"
