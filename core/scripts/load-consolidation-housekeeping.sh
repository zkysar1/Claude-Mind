#!/usr/bin/env bash
# Load consolidation housekeeping digest — returns path only if not already in context.
# Follows load-execute-protocol.sh pattern for context-reads integration.
# The digest (core/config/consolidation-housekeeping.md) is a compact version of
# .claude/skills/aspirations-consolidate/SKILL.md Steps 2.6-10 — housekeeping only.
# ~235 lines vs ~500+ for the full skill.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"

DIGEST="$CONFIG_DIR/consolidation-housekeeping.md"
OUT=$(python3 "$CORE_ROOT/scripts/context-reads.py" check-file "$DIGEST")
[ -n "$OUT" ] || exit 0
echo "$OUT"
# : a vessel mind Read this digest with limit=200 and never reached
# Step 9, the fast path's only handoff.yaml writer. Say where it is, from the file.
LINES=$(wc -l < "$DIGEST" | tr -d ' ')
STEP9=$(grep -n -m1 '^## Step 9:' "$DIGEST" | cut -d: -f1 || true)
echo "NOTE: $LINES lines. Read it to the END (no limit): Step 9, the only fast-path writer of handoff.yaml, is at line ${STEP9:-?}, and graceful-stop D7 refuses to finish the stop without it."
