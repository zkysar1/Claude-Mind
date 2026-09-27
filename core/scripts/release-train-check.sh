#!/usr/bin/env bash
# release-train-check — the release train's time-push trigger (read-only).
# Same implementation as agent-watchdog's ReleaseTrainProbe (_release_train.py).
#   bash core/scripts/release-train-check.sh [--json] [--nudge]
# Exit 0: not due (or not this deployment's train); 2: due; 1: unmeasured.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/release-train-check.py" "$@"
