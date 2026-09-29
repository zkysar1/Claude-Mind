#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Clear the context-reads tracker. Called from PreCompact (precompact-checkpoint.sh)
# and from SessionStart source=compact (sessionstart-orchestrator.sh).
#
# Args are passed through — pass `--session-id "$SID"` to clear the tracker THAT
# session actually uses. Without it (and without $MIND_SID, ) the clear
# targets the AGENT-WIDE tracker, which no session with a per-session dir uses
# (). On a worker Body that file may not exist at all: measured 2026-08-22 on
# cc-08, every live tracker on the box was sessions/<SID>/body-context-reads.txt
# and agents/*/session/context-reads.txt matched nothing at all. A bare clear
# there succeeds, reports nothing, and leaves the manifest intact ().
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/context-reads.py" clear "$@"
