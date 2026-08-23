#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Cheap idle sentinel — decides whether the current turn should background-sleep
# or proceed into the full aspirations skill.
#
# Exit 0 + empty stdout:  proceed normally (caller should load skill).
# Exit 0 + stdout text:   that stdout is an ADDITIONAL-CONTEXT directive —
#                         caller MUST NOT load the skill this turn.
#
# CRITICAL INVARIANTS (do not weaken):
#   - Stop signals win. If stop-requested or stop-loop is present we MUST
#     proceed, so the skill's Phase -1.4 graceful stop can run. Never sleep
#     through a stop request — that's the UX bug this whole design avoids.
#   - This script MUST NOT mutate blocked_sleep_until. Phase -0.5e is the
#     single owner of expire/clear. Adding a clear here creates ambiguity
#     about who "won" a concurrent update.
#   - No fallbacks. If wm-read or the parse fails, set -e kills the script;
#     caller sees non-zero exit and proceeds (fail open). Silencing errors
#     here would mean a corrupt blocked_sleep_until value silently sleeps
#     forever.
#
# Call sites:
#   1. SessionStart(matcher:compact) hook — fires after postcompact-restore.sh
#      so the timer slot is already restored before this reads it.
#   2. SKILL.md Phase -0.5e — first-line delegation before the rest of the skill.

set -euo pipefail
# Source _paths.sh for CORE_ROOT (Windows-form C:/... path). Never use
# raw `pwd` for paths passed to python3 -- on Windows MSYS it returns
# /c/... which Python cannot parse.
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"

# SessionStart(compact) hook call site has no env vars, so MIND_AGENT is
# unset and _paths.sh leaves AGENT_DIR empty. Resolve from session_id on
# stdin, then re-source _paths.sh so AGENT_NAME/AGENT_DIR are populated.
# LLM Phase -0.5e calls arrive with MIND_AGENT already set by the PreToolUse
# hook, so the resolve branch is skipped.
if [ -z "$AGENT_NAME" ]; then
    SID=$(python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")
    AGENT=$(python3 "$CORE_ROOT/scripts/_resolve_agent_from_sid.py" "$SID" 2>/dev/null || echo "")
    [ -z "$AGENT" ] && exit 0
    export MIND_AGENT="$AGENT"
    source "$CORE_ROOT/scripts/_paths.sh"
fi

[ -f "$AGENT_DIR/session/stop-requested" ] && exit 0
[ -f "$AGENT_DIR/session/stop-loop" ]       && exit 0

# wm-read prints the literal string "null" for a null slot, or a JSON string
# like "2026-04-16T21:30:00" when set. Strip the JSON quoting.
RAW=$(bash "$CORE_ROOT/scripts/wm-read.sh" blocked_sleep_until)
VAL=$(printf '%s' "$RAW" | tr -d '"' | tr -d '\r\n')
if [ -z "$VAL" ] || [ "$VAL" = "null" ]; then
  exit 0
fi

NOW_EPOCH=$(python3 -c "import time; print(int(time.time()))")
WAKE_EPOCH=$(python3 -c "
import sys, datetime
s = sys.argv[1].rstrip('Z')
print(int(datetime.datetime.fromisoformat(s).timestamp()))
" "$VAL")
REMAINING=$(( WAKE_EPOCH - NOW_EPOCH ))

# Horizon ≤60s: cheap to inline-sleep; let Phase -0.5e handle the residual.
if [ "$REMAINING" -le 60 ]; then
  exit 0
fi

# Cap the per-cycle sleep at 600s (10 min). Even when blocked_sleep_until is
# hours out, wake every 10 min to run a light precheck pass — completion
# runners, blocker resolution check, capability recheck sweep. This is the
# session-47 fix for "backoff silently disables framework health checks"
# (see .claude/rules/probe-with-canonical-code-path.md, guard-147, and the
# fuzzy-crafting-noodle plan). The sentinel below tells Phase -0.5e ELSE
# that this wake is a midway checkpoint, not a final expiration: it should
# run a light precheck and re-enter backoff if the blocker persists.
LIGHT_PRECHECK_CAP=600
SLEEP_DURATION="$REMAINING"
CHECKPOINT_SLEEP=0
if [ "$REMAINING" -gt "$LIGHT_PRECHECK_CAP" ]; then
  SLEEP_DURATION="$LIGHT_PRECHECK_CAP"
  CHECKPOINT_SLEEP=1
fi

# Horizon >60s: emit background-sleep directive.
# Mandatory: interruptible-sleep.sh (not plain `sleep`). The 1s-granularity
# stop-signal check lets /stop respond within seconds instead of waiting for
# a 30-minute plain sleep to exit.
cat <<EOF
=== IDLE TICK ===
Blocked-sleep timer active: ${REMAINING}s remaining (wake at ${VAL}).
Per-cycle cap: ${LIGHT_PRECHECK_CAP}s — will wake periodically to run a light
precheck pass (completion runners, blocker resolution, capability recheck)
even if the timer is further out. This cycle sleeps ${SLEEP_DURATION}s.
CHECKPOINT_SLEEP=${CHECKPOINT_SLEEP} (0=final, 1=midway — run light precheck then re-enter backoff).
DO NOT load Skill(aspirations). DO NOT run any selection or execution.
Emit exactly ONE tool call:
  Bash("MIND_AGENT=${AGENT_NAME} QUIESCENCE_SLEEP=1 bash core/scripts/interruptible-sleep.sh ${SLEEP_DURATION}", run_in_background=true)
When the harness notifies you of its exit, call Skill('aspirations') with args='loop'.
If you see this directive again after an autocompact, re-emit the Bash call
for the NEW ${SLEEP_DURATION} printed above (this script recomputes on each invocation).
=================
EOF
