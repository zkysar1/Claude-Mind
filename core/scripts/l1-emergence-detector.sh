#!/usr/bin/env bash
# L1 emergence detector (S4 + S6 + S7). Thin wrapper — see .py for docs.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/l1-emergence-detector.py" "$@"
