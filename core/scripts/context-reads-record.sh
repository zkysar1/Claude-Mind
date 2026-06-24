#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
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

# Resolve agent from session_id — MIND_AGENT is not injected in Read hooks.
# ORDER-CRITICAL: must stay BEFORE `source _platform.sh`; MSYS_NO_PATHCONV
# (set by _platform.sh) breaks session-binding-read.sh on Git Bash ().
AGENT_NAME="${MIND_AGENT:-}"
if [ -z "$AGENT_NAME" ] && [ -n "$session_id" ]; then
    AGENT_NAME="$(bash "$CORE_ROOT/scripts/session-binding-read.sh" "$session_id" 2>/dev/null || true)"
fi

source "$CORE_ROOT/scripts/_platform.sh"
exec MIND_AGENT="${AGENT_NAME:-}" python3 "$CORE_ROOT/scripts/context-reads.py" record $sid_arg "$file_path"
