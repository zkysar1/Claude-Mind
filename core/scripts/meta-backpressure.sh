#!/usr/bin/env bash
# meta-backpressure.sh — Backpressure gate for meta-strategy changes
# Usage:
#   meta-backpressure.sh monitor --change-id <id> --file <f> --field <dp> --old <v> --new <v> --baseline <imp_k>
#   meta-backpressure.sh check --learning-value <v>
#   meta-backpressure.sh graduate --change-id <id>
#   meta-backpressure.sh status
#   meta-backpressure.sh cooldown-check [--window <N>]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/meta-backpressure.py" "$@"
