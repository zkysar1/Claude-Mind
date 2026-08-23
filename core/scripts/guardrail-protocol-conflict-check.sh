#!/usr/bin/env bash
# Guardrail-vs-protocol conflict check () — cadence wrapper ().
# See core/scripts/guardrail-protocol-conflict-check.py for full docs.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/guardrail-protocol-conflict-check.py" "$@"
