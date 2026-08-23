#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Pending-questions sweep — self-resolution scan.
# Replaces consolidation-housekeeping.md Step 2.8's per-question LLM probe loop
# with a single Python pass. See core/scripts/pending-questions-sweep.py for
# heuristic details and JSON output schema.
#
# Subcommands: sweep | stats
# Exit 0 = no flags raised. Exit 1 = flags raised (LLM should review).
# Exit 2 = input error.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/pending-questions-sweep.py" "$@"
