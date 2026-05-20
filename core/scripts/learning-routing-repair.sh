#!/usr/bin/env bash
# learning-routing-repair.sh — one-shot cleanup of dangling cross-references.
# Default is dry-run. Pass --apply to write repairs + preserve old values in
# world/.history/learning-routing-repair-YYYY-MM-DD.jsonl.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/learning-routing-repair.py" "$@"
