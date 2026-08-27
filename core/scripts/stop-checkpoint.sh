#!/usr/bin/env bash
# Graceful-stop checkpoint sentinel — thin pure-CLI wrapper ( / FW-11).
# Subcommands: write --target-mode <m> | read | resume-needed | clear.
# Manages agents/<agent>/session/stop-checkpoint.json — the persistent
# detection signal for resuming an autocompact-interrupted graceful stop.
# NOT daemon-routed: this is session-local state (sibling to iteration-checkpoint.json),
# not one of the 35 daemon-migrated wrappers. See stop_checkpoint.py for docs.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/stop_checkpoint.py" "$@"
