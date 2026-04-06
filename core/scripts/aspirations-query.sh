#!/usr/bin/env bash
# Targeted goal query — searches both world and agent queues, returns only matching goals.
# Lightweight alternative to loading the full aspirations-compact.json into context.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/aspirations.py" query "$@"
