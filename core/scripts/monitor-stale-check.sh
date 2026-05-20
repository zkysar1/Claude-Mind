#!/usr/bin/env bash
# Auto-complete Monitor goals whose processor-run ID is superseded. See monitor-stale-check.py.
# Usage: monitor-stale-check.sh [--max-age-hours N] [--apply]
#
# The bash wrapper probes processor-run.sh (the canonical probe path) and
# passes the result as MSC_CURRENT_RUN_DIR so the Python child does not have
# to re-shell into bash (sig-005: Python→bash subprocess on Windows resolves
# to a WSL shim that cannot traverse OneDrive paths).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"

# Probe the canonical path; extract run_dir from JSON. Fail open (empty env
# var → Python reports "skipped: no_current_run_id").
PROC_SCRIPT="$WORLD_DIR/scripts/processor-run.sh"
if [ -x "$PROC_SCRIPT" ]; then
    RUN_JSON=$(bash "$PROC_SCRIPT" check-complete 2>/dev/null || true)
    # Python does the JSON parse to keep bash logic minimal.
    export MSC_CHECK_COMPLETE_JSON="$RUN_JSON"
fi

exec python3 "$CORE_ROOT/scripts/monitor-stale-check.py" "$@"
