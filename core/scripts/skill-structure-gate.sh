#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Skill-Structure Gate — thin wrapper for skill-structure-gate.py.
# Dynamic enforcement of SKILL.md invariants per
# .claude/rules/return-protocol.md "Applies To" + Verification sections.
# Pattern mirrors capability-gate.sh and audit-schema-gate.sh.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/skill-structure-gate.py" "$@"
