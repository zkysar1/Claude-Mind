#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# PostToolUse[Read] hook — record file reads into context-reads tracker.
# Reads JSON from stdin (tool_input.file_path), records if in scope.
# Ranged reads (offset/limit/pages) ARE recorded, flagged --partial ().
# They used to be dropped here, which made the read-before-edit advisory assert
# "has not been Read this session" about a file just read — on every large file,
# since a large file is exactly the one read with offset/limit. --partial keeps
# them visible to that advisory while invisible to the BLOCKING dedup gate; see
# PARTIAL_PREFIX in context-reads.py for why that split is load-bearing.
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

if [ -z "$file_path" ]; then
    exit 0  # Nothing to record
fi

sid_arg=""
if [ -n "$session_id" ]; then
    sid_arg="--session-id $session_id"
fi

partial_arg=""
if [ "$partial" = "1" ]; then
    partial_arg="--partial"
fi

# Resolve agent from session_id — MIND_AGENT is not injected in Read hooks.
# ORDER-CRITICAL: must stay BEFORE `source _platform.sh`; MSYS_NO_PATHCONV
# (set by _platform.sh) breaks session-binding-read.sh on Git Bash ().
AGENT_NAME="${MIND_AGENT:-}"
if [ -z "$AGENT_NAME" ] && [ -n "$session_id" ]; then
    AGENT_NAME="$(bash "$CORE_ROOT/scripts/session-binding-read.sh" "$session_id" 2>/dev/null || true)"
fi

source "$CORE_ROOT/scripts/_platform.sh"
# `exec env VAR=... cmd` — NOT `exec VAR=... cmd`. Prefix assignments are only
# parsed at the start of a simple command; after the word `exec` they become
# exec's FIRST ARGUMENT, so bash tries to run a program literally named
# "MIND_AGENT=..." and dies rc=127 ("not found"). PostToolUse hook errors are
# swallowed, so the old form silently disabled read-RECORDING on every Read —
# the tracker never accumulated entries (found 2026-07-07, twin of the same
# bug in context-reads-gate.sh).
exec env MIND_AGENT="${AGENT_NAME:-}" python3 "$CORE_ROOT/scripts/context-reads.py" record $sid_arg $partial_arg "$file_path"
