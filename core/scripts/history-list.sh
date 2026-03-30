#!/usr/bin/env bash
# List all historical versions of a file.
# Usage: bash core/scripts/history-list.sh <file>
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/history.py" list "$@"
