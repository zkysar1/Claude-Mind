#!/usr/bin/env bash
# Execution diary — append-only reasoning breadcrumb trail.
# Subcommands: append, read, summary, trim
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/execution-diary.py" "$@"
