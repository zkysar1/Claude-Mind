#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Restore all WM slots from compact checkpoint.
# Called by Phase -0.5c of the aspirations loop.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
python3 "$CORE_ROOT/scripts/compact-restore-slots.py" "$@"
_rc=$?
# Post-autocompact resume is where the "run the always-run calls from memory,
# skip Skill(aspirations-precheck)" shape is born (measured cc-04 2026-08-17:
# 4 compactions in 100 min, precheck dark for 4 iterations). Print the
# precheck-gap verdict as the LAST lines so a `| tail -N` reader still sees it.
# Fail-open; the restore's own rc is preserved.
bash "$CORE_ROOT/scripts/precheck-gap-check.sh" 2>/dev/null || true
exit "$_rc"
