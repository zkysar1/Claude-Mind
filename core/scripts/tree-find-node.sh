#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"

# Translate --text to --find for tree.py, pass other args through
args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --text) args+=(--find "$2"); shift 2 ;;
    *) args+=("$1"); shift ;;
  esac
done

source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/tree.py" read "${args[@]}"
