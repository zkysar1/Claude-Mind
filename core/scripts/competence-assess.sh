#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
exec python3 "$CORE_ROOT/scripts/competence-assess.py" "$@"
