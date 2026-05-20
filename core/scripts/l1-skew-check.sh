#!/usr/bin/env bash
# L1 distribution skew detector (S1) — thin wrapper.
# See core/scripts/l1-skew-check.py for full docs.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/l1-skew-check.py" "$@"
