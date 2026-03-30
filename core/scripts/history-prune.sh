#!/usr/bin/env bash
# Prune old history snapshots based on retention policy.
# Usage: bash core/scripts/history-prune.sh [--dry-run]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/history.py" prune "$@"
