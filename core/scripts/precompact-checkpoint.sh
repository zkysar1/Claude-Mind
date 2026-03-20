#!/usr/bin/env bash
# PreCompact hook — snapshot encoding state before context compression.
# Called by the PreCompact hook in .claude/settings.json.
# Delegates to precompact-checkpoint.py (reads stdin JSON, writes checkpoint YAML).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/precompact-checkpoint.py" "$@"
