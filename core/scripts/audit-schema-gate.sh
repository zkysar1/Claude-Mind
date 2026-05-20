#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Audit-Schema Gate — thin wrapper for audit-schema-gate.py.
# See .claude/rules/verify-before-assuming.md sister rule rb-245:
# verify pseudocode field names against actual JSONL schema before auditing.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/audit-schema-gate.py" "$@"
