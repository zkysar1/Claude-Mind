#!/usr/bin/env bash
# PreToolUse[Skill] hook — programmatic utilization enforcement.
# Fires before every skill invocation. Only acts when the skill is
# aspirations-state-update. If retrieval-session.json has utilization_pending=true,
# auto-runs utilization-feedback.sh --all-noise as a fallback.
#
# This is the backstop that ensures the system NEVER has zero utilization data,
# even if the LLM skips Step 5b and Phase 4.26 entirely.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
# _platform.sh converts MSYS paths to Windows paths for python3 interop.
# Without this, $PROJECT_ROOT is /c/... which Windows Python cannot open.
source "$CORE_ROOT/scripts/_platform.sh"

# Extract skill name from hook stdin JSON (same pattern as context-reads-skill-gate.sh)
skill_info=$(python3 -c "
import sys,json
d = json.load(sys.stdin)
ti = d.get('tool_input',{})
sk = ti.get('skill','')
print(sk)
" 2>/dev/null) || true

# Only act on aspirations-state-update
if [ "$skill_info" != "aspirations-state-update" ]; then
    exit 0
fi

# Check if agent is bound
if [ -z "${AYOAI_AGENT:-}" ]; then
    exit 0
fi

SESSION_FILE="$PROJECT_ROOT/$AYOAI_AGENT/session/retrieval-session.json"

# No session file = no retrieval happened for this goal — pass silently
if [ ! -f "$SESSION_FILE" ]; then
    exit 0
fi

# Check if utilization is still pending
pending=$(python3 -c "
import json,sys
try:
    with open(sys.argv[1],'r') as f:
        d = json.load(f)
    goal = d.get('goal_id','')
    pending = d.get('utilization_pending', False)
    print(f'{pending}|{goal}')
except:
    print('False|')
" "$SESSION_FILE" 2>/dev/null) || true

is_pending="${pending%%|*}"
goal_id="${pending#*|}"

if [ "$is_pending" != "True" ]; then
    exit 0
fi

# Utilization pending — auto-apply all-noise fallback
echo "[utilization-gate] Phase 4.26 was skipped for $goal_id — auto-applying all-noise fallback" >&2
bash "$CORE_ROOT/scripts/utilization-feedback.sh" --goal "$goal_id" --all-noise >/dev/null 2>&1 || true

exit 0
