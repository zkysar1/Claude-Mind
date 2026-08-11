#!/usr/bin/env bash
# Scar-tissue check — the subtractive gradient's cadence () — thin wrapper.
# See core/scripts/scar-tissue-check.py for full docs.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/scar-tissue-check.py" "$@"
