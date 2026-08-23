#!/usr/bin/env bash
# skill-gaps-validate.sh — Validate meta/skill-gaps.yaml schema ()
# Usage: skill-gaps-validate.sh [path]   (defaults to meta/skill-gaps.yaml)
# Exit 0 = valid, 1 = corrupt (diagnostic on stderr), 2 = could not load.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/skill-gaps-validate.py" "$@"
