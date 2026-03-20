#!/usr/bin/env bash
# PostToolUse[Read] hook — record file reads into context-reads tracker.
# Reads JSON from stdin (tool_input.file_path), records if in scope.
# Partial reads (offset/limit/pages) are NOT recorded — only full reads are tracked.
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
    exit 0  # No file_path or partial read — skip recording
fi

sid_arg=""
if [ -n "$session_id" ]; then
    sid_arg="--session-id $session_id"
fi

source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/context-reads.py" record $sid_arg "$file_path"
