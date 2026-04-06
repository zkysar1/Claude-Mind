#!/usr/bin/env bash
# Release a claimed world goal (clear claimed_by and claimed_at).
# Usage: aspirations-release.sh <goal-id>
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/aspirations.py" release "${1:?Usage: aspirations-release.sh <goal-id>}"
