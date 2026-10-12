#!/usr/bin/env bash
# Cross-link sweep over reasoning bank + guardrails () — thin wrapper.
#
# REQUIRED entry point. The .py resolves WORLD_PATH from the environment, and
# that mapping comes from the per-agent local-paths.conf that ONLY _paths.sh
# reads, so a bare `py -3 core/scripts/crosslink-sweep.py` falls back to a
# nonexistent PROJECT_ROOT/world (guard-3864). The .py exits 2 on that path
# rather than report a zero-record scan as clean.
#
# See core/scripts/crosslink-sweep.py for the predicate and flags.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/crosslink-sweep.py" "$@"
