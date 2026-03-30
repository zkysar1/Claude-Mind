#!/usr/bin/env bash
# List available board channels with message counts.
# Usage: bash core/scripts/board-channels.sh
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/board.py" channels "$@"
