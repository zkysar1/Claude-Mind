#!/usr/bin/env bash
# Guardrail-vs-guardrail pair audit () — thin wrapper.
#
# REQUIRED entry point. The .py resolves WORLD_PATH from the environment, and
# that mapping comes from the per-agent local-paths.conf that ONLY _paths.sh
# reads — so a bare `py -3 core/scripts/guardrail-pair-audit.py` falls back to a
# nonexistent PROJECT_ROOT/world. The .py now RAISES on that path rather than
# reporting a zero-record scan as clean, but the wrapper is what makes the
# correct case work (guard-3864).
#
# See core/scripts/guardrail-pair-audit.py for full docs.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/guardrail-pair-audit.py" "$@"
