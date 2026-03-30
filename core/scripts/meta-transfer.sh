#!/usr/bin/env bash
# meta-transfer.sh — Export/import meta-strategy bundles for cross-domain transfer
# Usage:
#   meta-transfer.sh export --output <path>
#   meta-transfer.sh import --input <path> [--dry-run]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/meta-transfer.py" "$@"
