#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# SessionStart(compact) hook -- inject context restoration after compaction.
# Called by the SessionStart hook in .claude/settings.json (matcher: compact).
# Delegates to postcompact-restore.py (reads checkpoint, prints restoration to stdout).
set -euo pipefail
# _paths.sh + _platform.sh must both be sourced on Windows so CORE_ROOT is
# in cygpath form (C:/...) -- raw `pwd` returns /c/... which Python on
# Windows cannot open. Always use $CORE_ROOT for paths handed to python3,
# never $(cd && pwd).
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"

# SessionStart hooks inherit no env vars, so MIND_AGENT is unset here.
# Without binding resolution, _paths.py would set AGENT_DIR=None and
# postcompact-restore.py would crash at `AGENT_DIR / "session" / ...`.
SID=$(python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")
AGENT=$(python3 "$CORE_ROOT/scripts/_resolve_agent_from_sid.py" "$SID" 2>/dev/null || echo "")

# No agent bound to this SID -> nothing to restore. Exit clean; the user
# will see an empty post-compact prompt and can /start to rebind.
[ -z "$AGENT" ] && exit 0

# Observer guard: this hook resumes the autonomous loop after autocompact.
# Observers (reader/assistant mode) that coexist with the runner ALSO fire
# SessionStart(compact) when they autocompact, and without this guard receive
# the runner's "Resume execution on THIS goal" imperative — the mode-violation
# that surfaced as the 2026-05-10 bravo "assistant entered the loop" incident.
# Discriminator: only the runner's HOOK_SID equals running-session-id AFTER
# session-save-id.sh runs. session-save-id.sh updates running-session-id only
# when its witness gate confirms a legit runner resume, so that file is
# the canonical runner-identity signal at this point.
# DO NOT reorder this hook before session-save-id.sh in .claude/settings.json
# — the discriminator depends on session-save-id.sh having run first.
# Missing/empty running-session-id (no autonomous runner active) → skip;
# nothing to resume.
RUNNING_SID=$(cat "$(agent_dir "$AGENT")/session/running-session-id" 2>/dev/null | tr -d '\r\n')
if [ -z "$RUNNING_SID" ] || [ "$SID" != "$RUNNING_SID" ]; then
    exit 0
fi

export MIND_AGENT="$AGENT"
exec python3 "$CORE_ROOT/scripts/postcompact-restore.py"
