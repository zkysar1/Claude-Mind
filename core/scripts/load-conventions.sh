#!/usr/bin/env bash
# Batch convention check — print convention file paths NOT yet in context.
# Usage: load-conventions.sh aspirations pipeline experience ...
# Output: absolute paths of convention files that need reading (one per line).
# If output is empty, all conventions are already loaded — skip reads.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/context-reads.py" check "$@"
