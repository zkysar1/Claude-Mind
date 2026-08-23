#!/usr/bin/env bash
# Load stop/skip conditions reference — returns path only if not already in context.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"

python3 "$CORE_ROOT/scripts/context-reads.py" check-file \
    "$CONFIG_DIR/stop-skip-conditions.md"
