#!/usr/bin/env bash
# Re-examine aged blockers against the capability gate. See blocker-recheck.py.
# Usage: blocker-recheck.sh [--max-age-hours N] [--apply] [--investigate-aspiration asp-XXX]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/blocker-recheck.py" "$@"
