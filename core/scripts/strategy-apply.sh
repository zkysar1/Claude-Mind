#!/usr/bin/env bash
# strategy-apply.sh — Surface applicable meta-strategy heuristics during goal execution.
# Full doc: core/scripts/strategy-apply.py
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/strategy-apply.py" "$@"
