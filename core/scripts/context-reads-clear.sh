#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Clear the context-reads tracker. Called from PreCompact (precompact-checkpoint.sh)
# and from SessionStart source=compact (sessionstart-orchestrator.sh).
#
# Args are passed through — pass `--session-id "$SID"` to clear the tracker THAT
# session actually uses. Without it the clear targets the AGENT-WIDE tracker, and
# on a worker Body that is a file which does not exist: measured 2026-08-22 on
# cc-08, every live tracker on the box was sessions/<SID>/body-context-reads.txt
# and agents/*/session/context-reads.txt matched nothing at all. A bare clear
# there succeeds, reports nothing, and leaves the manifest intact ().
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/context-reads.py" clear "$@"
