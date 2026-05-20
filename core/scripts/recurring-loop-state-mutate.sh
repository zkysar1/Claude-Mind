#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# recurring-loop-state-mutate.sh — thin wrapper for recurring-loop-state-mutate.py.
#
# Magic Wand #1 (alpha session-60 reflection, 2026-05-07). recurring-close.sh
# invokes this BEFORE the 4 iteration-close phases. The wrapper sources
# _paths.sh + _platform.sh so the python entrypoint sees Windows-converted
# paths on Git Bash; same pattern as tree-encoding-drift-gate.sh.
#
# Behavior — see recurring-loop-state-mutate.py docstring. Stdout is the
# post-mutation outcome class (routine|deep). Stderr is a summary line for
# the LLM. Always exits 0 (fail-open).

set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$(cd "$(dirname "$0")" && pwd)/_platform.sh"

exec python3 "$CORE_ROOT/scripts/recurring-loop-state-mutate.py" "$@"
