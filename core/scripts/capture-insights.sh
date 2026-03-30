#!/usr/bin/env bash
# Stop hook — capture ✶ Insight blocks from assistant output.
# Appends to <agent>/insights.jsonl. Always exits 0 (never blocks).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
python3 core/scripts/capture-insights.py || true
