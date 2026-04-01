#!/usr/bin/env bash
# Stop hook — capture ✶ Insight blocks from assistant output.
# Appends to <agent>/insights.jsonl. Always exits 0 (never blocks).
#
# IMPORTANT: This hook runs BEFORE stop-hook.sh in the same hooks array.
# json.load(stdin) in capture-insights.py would consume all stdin, leaving
# stop-hook.sh with nothing. Save stdin to a file so both hooks can read it.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"

# Save stdin for stop-hook.sh (runs next, needs session_id from the same payload)
STOP_HOOK_STDIN="$PROJECT_ROOT/.stop-hook-stdin.json"
cat > "$STOP_HOOK_STDIN"
python3 core/scripts/capture-insights.py < "$STOP_HOOK_STDIN" || true
