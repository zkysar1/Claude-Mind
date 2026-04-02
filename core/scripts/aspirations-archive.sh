#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
# Support --source flag before the subcommand
SOURCE_ARGS=""
while [[ "${1:-}" == --source ]]; do
    SOURCE_ARGS="--source $2"
    shift 2
done
# shellcheck disable=SC2086
exec python3 "$CORE_ROOT/scripts/aspirations.py" $SOURCE_ARGS archive-sweep
