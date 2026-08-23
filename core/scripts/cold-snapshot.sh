#!/usr/bin/env bash
# Cold-snapshot precious world/ + meta/ state to a retention-immune object key.
# See core/scripts/cold_snapshot.py for the measurement + rationale.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/cold_snapshot.py" "$@"
