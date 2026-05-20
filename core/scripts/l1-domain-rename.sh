#!/usr/bin/env bash
# Rename an L1 domain (S8). Thin wrapper — see l1-domain-rename.py for docs.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/l1-domain-rename.py" "$@"
