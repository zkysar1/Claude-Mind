#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# PreCompact hook — snapshot encoding state before context compression.
# Called by the PreCompact hook in .claude/settings.json.
# Delegates to precompact-checkpoint.py (writes checkpoint YAML). Stdin is
# consumed by the SID-resolution preamble below for MIND_AGENT export; the
# python target sees empty stdin and falls back to trigger="auto" — see L28-30.
#
# PreCompact hooks inherit no env vars — MIND_AGENT is unset here, so
# precompact-checkpoint.py would crash at module-load on `AGENT_DIR / "session"`
# (None / str -> TypeError) and exit 1 before main() runs. Result: no checkpoint
# written, autocompact proceeds, postcompact-restore falls through to the
# degraded-restore path. Bravo's 2026-05-05 hung-autocompact incident traced
# back to this silent-since-creation failure. Fix mirrors postcompact-restore.sh:
# resolve agent from stdin SID, export MIND_AGENT, then exec the python script.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"

# CRITICAL — DO NOT REMOVE OR FACTOR OUT THE FOLLOWING SID→AGENT RESOLUTION.
# It looks duplicative with postcompact-restore.sh, and it is — by design.
# PreCompact and SessionStart are SEPARATE hook contexts that BOTH inherit
# zero env vars (the bash-agent-inject PreToolUse hook only fires on Bash
# tool calls). Each hook script must resolve MIND_AGENT independently. If
# you remove this preamble believing some upstream sets MIND_AGENT, the
# python script crashes at module load (None / "session" → TypeError),
# Python exits 1, and Claude Code proceeds with autocompact silently —
# no checkpoint written, postcompact-restore falls through to degraded
# path, loop_state lost. Fixed 2026-05-05; see rb-697.
#
# Stdin is consumed here for SID extraction — the python script's `trigger`
# field falls back to "auto" when stdin is empty, so re-piping is not
# load-bearing. Checkpoint correctness depends on AGENT_DIR resolution.
SID=$(python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")
AGENT=$(python3 "$CORE_ROOT/scripts/_resolve_agent_from_sid.py" "$SID" 2>/dev/null || echo "")

# No agent bound to this SID → nothing to checkpoint. Exit clean (fail-open
# discipline: PreCompact never blocks compaction).
if [[ -z "$AGENT" ]]; then
    echo "[precompact-checkpoint] WARN: no agent for SID=$SID — skip checkpoint" >&2
    exit 0
fi

export MIND_AGENT="$AGENT"
exec python3 "$CORE_ROOT/scripts/precompact-checkpoint.py" "$@"
