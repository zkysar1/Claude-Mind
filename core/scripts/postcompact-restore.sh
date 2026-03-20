#!/usr/bin/env bash
# SessionStart(compact) hook — inject context restoration after compaction.
# Called by the SessionStart hook in .claude/settings.json (matcher: compact).
# Delegates to postcompact-restore.py (reads checkpoint, prints restoration to stdout).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/postcompact-restore.py" "$@"
