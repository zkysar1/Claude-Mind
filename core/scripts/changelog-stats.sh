#!/usr/bin/env bash
# Show changelog statistics.
# Usage: bash core/scripts/changelog-stats.sh [--since <duration>]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/changelog.py" stats "$@"
