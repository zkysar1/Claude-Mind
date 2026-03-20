#!/usr/bin/env bash
# PreToolUse[Read] hook — gate duplicate file reads.
# Reads JSON from stdin (tool_input.file_path), checks context-reads tracker.
# Exit 0 = allow read, Exit 2 = block (already in context).
# Partial reads (offset/limit/pages) always pass — only full reads are gated.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"

# Extract file_path, session_id, and detect partial read parameters
read_info=$(python3 -c "
import sys,json
d = json.load(sys.stdin)
ti = d.get('tool_input',{})
fp = ti.get('file_path','')
partial = '1' if (ti.get('offset') is not None or ti.get('limit') is not None or ti.get('pages') is not None) else '0'
sid = d.get('session_id','')
print(f'{partial}|{sid}|{fp}')
" 2>/dev/null)

partial="${read_info%%|*}"
rest="${read_info#*|}"
session_id="${rest%%|*}"
file_path="${rest#*|}"

if [ -z "$file_path" ] || [ "$partial" = "1" ]; then
    exit 0  # No file_path or partial read — always allow
fi

sid_arg=""
if [ -n "$session_id" ]; then
    sid_arg="--session-id $session_id"
fi

source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/context-reads.py" gate $sid_arg "$file_path"
