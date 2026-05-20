#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
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

# Long-term invocation telemetry — append to per-agent JSONL ledger.
# Fail-open: any failure here must NOT block the skill from running.
# Knowledge tree: world/knowledge/tree/system/system-constraints-loop/skill-telemetry-signal-master-plan.md
if [ -n "${AGENT_DIR:-}" ] && [ -n "$skill_name" ]; then
    AGENT_DIR="$AGENT_DIR" AGENT_NAME="${AGENT_NAME:-}" SKILL_NAME="$skill_name" SESSION_ID="$session_id" \
        python3 - <<'PY' 2>/dev/null || true
import json, datetime, os
agent_dir = os.environ.get('AGENT_DIR', '')
agent_name = os.environ.get('AGENT_NAME', '')
skill = os.environ.get('SKILL_NAME', '')
sid = os.environ.get('SESSION_ID', '')
if agent_dir and skill:
    row = {
        'ts': datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
        'skill': skill,
        'agent': agent_name,
        'sid': sid,
        'invocation_source': 'model',
    }
    try:
        with open(os.path.join(agent_dir, 'skill-invocations.jsonl'), 'a', encoding='utf-8') as f:
            f.write(json.dumps(row) + chr(10))
    except Exception:
        pass
PY
fi

exit 0
