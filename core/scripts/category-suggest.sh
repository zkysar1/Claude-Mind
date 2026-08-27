#!/usr/bin/env bash
# Category suggestion — maps free text to tree node keys.
# Usage: category-suggest.sh --text "Fix authentication retry logic" [--top 3]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
python3 "$CORE_ROOT/scripts/category-suggest.py" "$@"
