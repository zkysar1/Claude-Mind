#!/usr/bin/env bash
# Read messages from a board channel.
# Usage: bash core/scripts/board-read.sh --channel <name> [--since <duration>] [--author <name>] [--last <N>]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/board.py" read "$@"
