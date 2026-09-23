#!/usr/bin/env bash
# Graceful-stop D7 handoff check () — thin wrapper over stop_handoff_check.py.
# Refuses D7's mode flip until THIS stop has written agents/<agent>/session/handoff.yaml;
# on a refusal it prints the consolidation digest's Step 9 verbatim. See the .py docstring.
# Usage: stop-handoff-check.sh [--agent <name>] [--proceed-without-handoff "<why>"]
# Exit:  0 = handoff fresh | override | fail-open;  1 = refused (Step 9 printed).
# The agent comes from --agent, MIND_AGENT, or MIND_AGENT (the vessel's call shape),
# exported BEFORE _paths.sh so path resolution names the right agent.
set -euo pipefail
if [ -z "${MIND_AGENT:-}" ] && [ -n "${MIND_AGENT:-}" ]; then export MIND_AGENT="$MIND_AGENT"; fi
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/stop_handoff_check.py" "$@"
