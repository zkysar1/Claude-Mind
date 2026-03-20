#!/usr/bin/env bash
# One-time migration: assign categories to all goals missing them.
# Usage: category-backfill.sh [--dry-run]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
python3 "$CORE_ROOT/scripts/category-backfill.py" "$@"
