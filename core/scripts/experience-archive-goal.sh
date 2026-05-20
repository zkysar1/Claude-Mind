#!/usr/bin/env bash
# Phase 4.25 bash-enforced goal-execution experience archive wrapper.
# Dispatches to experience.py archive-goal. See convention
# core/config/conventions/experience.md and rb-428 (bash-consolidation drift).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/experience.py" archive-goal "$@"
