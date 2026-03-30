#!/usr/bin/env bash
# Restore a historical version of a file.
# Usage: bash core/scripts/history-restore.sh <file> <version-name>
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/history.py" restore "$@"
