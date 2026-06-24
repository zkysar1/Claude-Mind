#!/usr/bin/env bash
# Anticipated-failures store (Phase 3.96 anticipatory reflection) — `update` subcommand.
# Argv goal_id + stdin JSON outcome dict; sets record.outcome; exit 2 if goal_id not found.
# Engine: core/scripts/anticipated-failures.py. Design: world/conventions/anticipated-failures.md.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/anticipated-failures.py" update "$@"
