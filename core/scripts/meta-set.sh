#!/usr/bin/env bash
# meta-set.sh — Set a field in a meta-strategy file (bounds-validated, auto-logged)
# Usage: meta-set.sh <file> <dotpath> <value> [--reason "..."]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/meta-yaml.py" set "$@"
