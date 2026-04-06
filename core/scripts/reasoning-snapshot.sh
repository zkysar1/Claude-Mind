#!/usr/bin/env bash
# Reasoning snapshot — tight-zone proactive context persistence.
# Subcommands: write, read, clear
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/reasoning-snapshot.py" "$@"
