#!/usr/bin/env bash
# Diff current file vs a historical version.
# Usage: bash core/scripts/history-diff.sh <file> <version-name>
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/history.py" diff "$@"
