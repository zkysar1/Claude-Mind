#!/usr/bin/env bash
# zakcode-session-end-hook.sh — zak-code SessionEnd lifecycle hook (Mode 1 + Mode 2)
#
# Fired by zak-code's SessionEnd hook. Reads the LifecyclePayload JSON from
# stdin and writes three persistence artifacts at session close:
#
#   [Mode 1 - Step 2.5] session-summary.yaml  (always)
#   [Mode 2 - Step 3.5] experience.jsonl entry (: chat_session record)
#   [Mode 2 - Step 3.5] working-memory snapshot (: two copy locations)
#
# Output paths:
#   agents/<AGENT>/sessions/<session_id>/session-summary.yaml
#   agents/<AGENT>/sessions/<session_id>/working-memory-snapshot.yaml
#   agents/<AGENT>/session/working-memory-snapshot.yaml  (latest — for next-session restore)
#   agents/<AGENT>/experience.jsonl  (appended; direct write, no daemon needed at hook time)
#
# Payload shape (zak-code LifecyclePayload, sent on stdin):
#   {
#     "event": "SessionEnd",
#     "session_id": "<id>",
#     "cwd": "<workspace>",
#     "data": {
#       "trigger": "session_end",
#       "session_summary": {
#         "session_id": "<id>",
#         "message_count": <n>,
#         "total_usage": { "input_tokens": <n>, "output_tokens": <n>, ... },
#         "created_at": "<iso>"
#       }
#     }
#   }
#
# Agent detection (in order of preference):
#   1. MIND_AGENT env var
#   2. Single agents/*/local-paths.conf match (exactly one agent)
#   3. Falls back to "unknown" with a warning on stderr
#
# Configure in .zakcode/settings.json (or .claude/settings.json):
#   {
#     "hooks": {
#       "SessionEnd": [
#         {
#           "matcher": "*",
#           "hooks": [
#             {
#               "type": "command",
#               "command": "bash core/scripts/zakcode-session-end-hook.sh",
#               "timeout": 10
#             }
#           ]
#         }
#       ]
#     }
#   }
#
# Exit codes: 0 = wrote summary, 2 = skipped (no session_id or agent undetectable)
#
set -euo pipefail

PAYLOAD=$(cat)
if [ -z "$PAYLOAD" ]; then
    echo "[zakcode-session-end-hook] WARNING: empty stdin, skipping" >&2
    exit 2
fi

# Parse payload fields
PARSED=$(py -3 -c "
import json, sys
payload = json.loads(sys.argv[1])
s = payload.get('data', {}).get('session_summary', {})
usage = s.get('total_usage', {})
print(payload.get('session_id', ''))
print(s.get('message_count', 0))
print(s.get('created_at', ''))
print(usage.get('input_tokens', 0))
print(usage.get('output_tokens', 0))
print(payload.get('cwd', ''))
" "$PAYLOAD" 2>/dev/null) || {
    echo "[zakcode-session-end-hook] ERROR: could not parse JSON payload" >&2
    exit 2
}

SESSION_ID=$(echo "$PARSED" | sed -n '1p')
MSG_COUNT=$(echo "$PARSED"  | sed -n '2p')
CREATED_AT=$(echo "$PARSED" | sed -n '3p')
IN_TOK=$(echo "$PARSED"     | sed -n '4p')
OUT_TOK=$(echo "$PARSED"    | sed -n '5p')
CWD=$(echo "$PARSED"        | sed -n '6p')

if [ -z "$SESSION_ID" ]; then
    echo "[zakcode-session-end-hook] WARNING: no session_id in payload, skipping" >&2
    exit 2
fi

# Resolve project root from this script's location (core/scripts/ -> ../../)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Agent detection
AGENT="${MIND_AGENT:-}"
if [ -z "$AGENT" ]; then
    CONF_PATHS=$(find "$PROJECT_ROOT/agents" -name "local-paths.conf" 2>/dev/null)
    CONF_COUNT=$(echo "$CONF_PATHS" | grep -c . 2>/dev/null || echo 0)
    if [ "$CONF_COUNT" -eq 1 ]; then
        AGENT=$(basename "$(dirname "$CONF_PATHS")")
    else
        echo "[zakcode-session-end-hook] WARNING: MIND_AGENT not set and $CONF_COUNT agents found; using 'unknown'" >&2
        AGENT="unknown"
    fi
fi

SESSION_DIR="$PROJECT_ROOT/agents/$AGENT/sessions/$SESSION_ID"
mkdir -p "$SESSION_DIR"
OUT="$SESSION_DIR/session-summary.yaml"
ENDED_AT="$(date +%Y-%m-%dT%H:%M:%S)"

cat > "$OUT" <<YAML
agent: "$AGENT"
session_id: "$SESSION_ID"
source: zak-code
created_at: "$CREATED_AT"
ended_at: "$ENDED_AT"
message_count: $MSG_COUNT
usage:
  input_tokens: $IN_TOK
  output_tokens: $OUT_TOK
workspace: "$CWD"
YAML

echo "[zakcode-session-end-hook] wrote $OUT"

# --- Mode 2 / : Working-memory snapshot ---
# Copies working-memory.yaml to two locations: per-session archive and the
# "latest" pointer at agents/<agent>/session/ that the next session can restore from.
# Entirely fail-open — never blocks the hook or the session close.
_write_wm_snapshot() {
    local WM_SRC="$PROJECT_ROOT/agents/$AGENT/session/working-memory.yaml"
    [ -f "$WM_SRC" ] || return 0
    cp "$WM_SRC" "$SESSION_DIR/working-memory-snapshot.yaml"
    cp "$WM_SRC" "$PROJECT_ROOT/agents/$AGENT/session/working-memory-snapshot.yaml"
    echo "[zakcode-session-end-hook] wrote working-memory snapshots (per-session + latest)"
}
_write_wm_snapshot 2>/dev/null || true

# --- Mode 2 / : Experience trace ---
# Appends a minimal chat_session record to experience.jsonl.
# Written directly (no daemon) because hook context has no daemon available.
# The record carries enough metadata for retrieval (type, date, session_id,
# usage) without requiring the full goal-execution experience schema.
_write_experience_record() {
    local EXP_FILE="$PROJECT_ROOT/agents/$AGENT/experience.jsonl"
    local TODAY
    TODAY="$(date +%Y-%m-%d)"
    local NOW_TS
    NOW_TS="$(date +%Y-%m-%dT%H:%M:%S)"
    local EXP_ID="exp-zak-${TODAY}-${SESSION_ID:0:8}"
    py -3 - "$EXP_ID" "$TODAY" "$SESSION_ID" "$CREATED_AT" "$NOW_TS" \
             "$MSG_COUNT" "$IN_TOK" "$OUT_TOK" "$CWD" "$AGENT" <<'PYEOF'
import json, sys
a = sys.argv
record = {
    "id": a[1],
    "type": "chat_session",
    "date": a[2],
    "summary": (
        f"zak-code session ({a[10]}): {a[6]} messages, "
        f"{a[7]} input / {a[8]} output tokens"
    ),
    "session_id": a[3],
    "source": "zakcode-session-end-hook",
    "created_at": a[4],
    "ended_at": a[5],
    "workspace": a[9],
    "retrieval_stats": None,
}
print(json.dumps(record, ensure_ascii=False))
PYEOF
}
{
    EXP_RECORD="$(_write_experience_record 2>/dev/null)"
    if [ -n "$EXP_RECORD" ]; then
        EXP_FILE="$PROJECT_ROOT/agents/$AGENT/experience.jsonl"
        printf '%s\n' "$EXP_RECORD" >> "$EXP_FILE"
        EXP_ID=$(py -3 -c "import json,sys; print(json.loads(sys.argv[1]).get('id','?'))" "$EXP_RECORD" 2>/dev/null || echo "?")
        echo "[zakcode-session-end-hook] appended experience record $EXP_ID"
    fi
} 2>/dev/null || true

exit 0
