#!/usr/bin/env bash
# meta-experiment.sh — A/B experiment lifecycle management
# Usage:
#   meta-experiment.sh create --strategy <file> --field <dotpath> --baseline <v> --variant <v>
#   meta-experiment.sh status [--id <exp-id>]
#   meta-experiment.sh resolve --id <exp-id>
#   meta-experiment.sh list [--active|--completed]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/meta-experiment.py" "$@"
