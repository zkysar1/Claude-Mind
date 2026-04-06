#!/usr/bin/env bash
# Load consolidation housekeeping digest — returns path only if not already in context.
# Follows load-execute-protocol.sh pattern for context-reads integration.
# The digest (core/config/consolidation-housekeeping.md) is a compact version of
# .claude/skills/aspirations-consolidate/SKILL.md Steps 2.6-10 — housekeeping only.
# ~235 lines vs ~500+ for the full skill.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"

python3 "$CORE_ROOT/scripts/context-reads.py" check-file \
    "$CONFIG_DIR/consolidation-housekeeping.md"
