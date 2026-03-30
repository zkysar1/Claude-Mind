#!/usr/bin/env bash
# meta-dead-ends.sh — Dead end registry for meta-strategy approaches
# Usage:
#   echo '<json>' | meta-dead-ends.sh add
#   meta-dead-ends.sh check --file <f> --field <dp> --value <v>
#   meta-dead-ends.sh read [--active] [--category <cat>]
#   meta-dead-ends.sh increment <id>
#   meta-dead-ends.sh review <id>
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/meta-dead-ends.py" "$@"
