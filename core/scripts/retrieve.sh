#!/usr/bin/env bash
# Unified context retrieval. Supports --supplementary-only to skip tree nodes.
# All depth levels return full results with .md content (depth limits removed).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/retrieve.py" "$@"
