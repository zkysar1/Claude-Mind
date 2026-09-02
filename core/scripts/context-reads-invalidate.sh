#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# PostToolUse[Write,Edit] hook — invalidate modified files from context-reads tracker.
# Reads JSON from stdin (tool_input.file_path), removes from tracker if present.
# Only invalidates mutable tracked paths (world/knowledge/tree/**).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"

# Extract file_path AND session_id from the hook JSON in ONE stdin pass -- stdin
# is a stream and json.load consumes it, so a second extraction would read an
# empty stdin and silently yield "" (same shape as tree-write-fence.sh:35).
# session_id is what routes the invalidation to the per-Body tracker ();
# without it this hook always cleared the AGENT-WIDE file while reads were being
# recorded per-Body, so in a forked Body an edited tree node stayed dedup-BLOCKED.
hook_info=$(python3 -c "
import sys,json
d = json.load(sys.stdin)
print((d.get('session_id','') or '') + '|' + (d.get('tool_input',{}).get('file_path','') or ''))
" 2>/dev/null)

session_id="${hook_info%%|*}"
file_path="${hook_info#*|}"

if [ -z "$file_path" ]; then
    exit 0
fi

sid_arg=""
if [ -n "$session_id" ]; then
    sid_arg="--session-id $session_id"
fi

# Resolve agent from session_id — MIND_AGENT is not injected into Write/Edit
# hooks, and tracker_path() needs the agent dir to find sessions/<unitKey>/.
# ORDER-CRITICAL: must stay BEFORE `source _platform.sh`; MSYS_NO_PATHCONV
# (set by _platform.sh) breaks session-binding-read.sh on Git Bash ().
AGENT_NAME="${MIND_AGENT:-}"
if [ -z "$AGENT_NAME" ] && [ -n "$session_id" ]; then
    AGENT_NAME="$(bash "$CORE_ROOT/scripts/session-binding-read.sh" "$session_id" 2>/dev/null || true)"
fi

source "$CORE_ROOT/scripts/_platform.sh"
# `exec env VAR=... cmd` — NOT `exec VAR=... cmd`. After the word `exec` a prefix
# assignment becomes exec's FIRST ARGUMENT, so bash tries to run a program named
# "MIND_AGENT=..." and dies rc=127. PostToolUse hook errors are swallowed, so the
# wrong form silently disables invalidation entirely (the twin of that exact bug
# in context-reads-record.sh / context-reads-gate.sh, found 2026-07-07).
exec env MIND_AGENT="${AGENT_NAME:-}" python3 "$CORE_ROOT/scripts/context-reads.py" invalidate $sid_arg "$file_path"
