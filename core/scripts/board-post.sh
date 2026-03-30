#!/usr/bin/env bash
# Post a message to a board channel. Message text is read from stdin.
# Usage: echo "message" | bash core/scripts/board-post.sh --channel <name> [--reply-to <id>] [--tags <t1,t2>]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/board.py" post "$@"
