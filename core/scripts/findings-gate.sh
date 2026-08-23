#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Phase 8.5 Actionable Findings Gate wrapper.
# Dispatches to findings-gate.py. See aspirations-state-update/SKILL.md
# Step 8.5 and rb-428 (bash-consolidation drift).
#
# : stdout is teed to core/logs/findings-gate.log so the
# `findings_count=N created=M` line and per-signal context persist
# beyond LLM tool-output capture.  calibration metric 2
# found suppression rate unmeasurable because the gate output was
# only consumed inline by the LLM. With this tee, suppression rate =
# (sum of N - sum of M) / sum of N is grep-able from the log.
# pipefail (set above) ensures the python exit code propagates
# through tee — tee always returns 0, but pipefail picks the rightmost
# non-zero, so a failing gate still surfaces as failure.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
mkdir -p "$CORE_ROOT/logs"
python3 "$CORE_ROOT/scripts/findings-gate.py" "$@" | tee -a "$CORE_ROOT/logs/findings-gate.log"
