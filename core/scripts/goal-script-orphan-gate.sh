#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Goal-Script-Orphan Gate — thin wrapper for goal-script-orphan-gate.py.
# Flags goals whose description/skill names core/scripts/<name>.{sh,py}
# not present on disk. Inverse direction of scripts-referenced-gate.py
# (which flags scripts orphan from references). Companion gate filed via
#  after delta hit the gap on  (override-ledger-consume
# deletion + 1-week orphan-reference silence).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/goal-script-orphan-gate.py" "$@"
