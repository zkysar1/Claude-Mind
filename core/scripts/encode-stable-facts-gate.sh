#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Encode-Stable-Facts Gate — thin wrapper for encode-stable-facts-gate.py.
# Enforces the three-probe threshold from .claude/rules/encode-stable-facts.md.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/encode-stable-facts-gate.py" "$@"
