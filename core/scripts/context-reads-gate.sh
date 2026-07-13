#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Entry sentinel for hook-fire-audit () — FIRST executable line,
# bash-builtin only, fail-open. mtime of core/logs/hook-fires/context-reads-gate
# = last fire of this hook.
{ _HF_DIR="${BASH_SOURCE[0]%/*}/../.." ; mkdir -p "$_HF_DIR/core/logs/hook-fires" 2>/dev/null && : > "$_HF_DIR/core/logs/hook-fires/context-reads-gate" 2>/dev/null ; unset _HF_DIR ; } 2>/dev/null || true

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
# "MIND_AGENT=..." and dies rc=127 ("not found"). Because hooks fail open on
# non-{0,2} exits, the old form silently disabled read-gating on EVERY Read
# (found 2026-07-07 during the bravo dual-runner investigation).
exec env MIND_AGENT="${AGENT_NAME:-}" python3 "$CORE_ROOT/scripts/context-reads.py" gate $sid_arg "$file_path"
