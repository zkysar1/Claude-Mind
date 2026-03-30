#!/usr/bin/env bash
# Build a compact context block for spawned agent prompts.
# Output: plain text suitable for embedding in Agent() prompt parameter.
# Read-only — does not modify any state files.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/build-agent-context.py" "$@"
