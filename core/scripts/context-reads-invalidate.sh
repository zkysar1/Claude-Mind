#!/usr/bin/env bash
# PostToolUse[Write,Edit] hook — invalidate modified files from context-reads tracker.
# Reads JSON from stdin (tool_input.file_path), removes from tracker if present.
# Only invalidates mutable tracked paths (world/knowledge/tree/**).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"

# Extract file_path from hook stdin JSON
file_path=$(python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('file_path',''))" 2>/dev/null)

if [ -z "$file_path" ]; then
    exit 0
fi

source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/context-reads.py" invalidate "$file_path"
