#!/usr/bin/env bash
# PreToolUse[Skill] hook — gate AND record skill invocations.
# Reads JSON from stdin (tool_input.skill), checks context-reads tracker.
# Exit 0 = allow skill (and record it), Exit 2 = block (already in context).
#
# Combined gate+record because PostToolUse does not fire for the Skill tool
# (Skill injects content into the conversation stream rather than returning
# a traditional tool result).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"

# Extract skill name and session_id from hook stdin JSON
skill_info=$(python3 -c "
import sys,json
d = json.load(sys.stdin)
ti = d.get('tool_input',{})
sk = ti.get('skill','')
sid = d.get('session_id','')
print(f'{sid}|{sk}')
" 2>/dev/null) || true

session_id="${skill_info%%|*}"
skill_name="${skill_info#*|}"

if [ -z "$skill_name" ]; then
    exit 0  # No skill name — allow
fi

# Convert MSYS paths to Windows paths BEFORE constructing skill_path.
# Python's Path.resolve() mishandles MSYS /c/... paths, producing C:/c/...
source "$CORE_ROOT/scripts/_platform.sh"

# Construct the SKILL.md path that would be injected
skill_path="$PROJECT_ROOT/.claude/skills/$skill_name/SKILL.md"

if [ ! -f "$skill_path" ]; then
    exit 0  # Skill file doesn't exist — allow (harness will handle error)
fi

sid_arg=""
if [ -n "$session_id" ]; then
    sid_arg="--session-id $session_id"
fi

# Use gate subcommand — it exits 0 (allow) for untracked AND out-of-scope paths,
# exits 2 (block) only for tracked paths. The &&/|| idiom captures exit codes
# safely under set -e (commands in &&/|| chains are exempt from errexit).
python3 "$CORE_ROOT/scripts/context-reads.py" gate $sid_arg "$skill_path" >/dev/null 2>&1 && gate_rc=0 || gate_rc=$?

if [ "$gate_rc" -eq 2 ]; then
    echo "Skill /$skill_name instructions already in context — follow them from earlier in this conversation. Do NOT re-invoke." >&2
    exit 2
fi

# First invocation — record and allow
python3 "$CORE_ROOT/scripts/context-reads.py" record $sid_arg "$skill_path" 2>/dev/null || true
exit 0
