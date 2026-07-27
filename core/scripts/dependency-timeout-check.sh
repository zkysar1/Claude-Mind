#!/usr/bin/env bash
# Escalate dependency-blocked goals approaching the dependency timeout. See
# dependency-timeout-check.py for the full docstring + design rationale
# (notably WHY the cooldown is the coordination-board post rather than the
# per-agent WM proactive_escalation_log the originating goal specified).
# Bash-enforces precheck Phase 0.5b.2 — the last un-scripted member of the
# escalation-sweep family (; siblings inbox-alert-age-check.sh
# / handoff-aging-check.sh / reason-less-blocked-check.sh).
#
# Usage: dependency-timeout-check.sh [--apply] [--threshold-hours N] [--agent NAME]
#                                    [--board-escalation-log <path>]  # tests only
#                                    [--no-board]                     # tests only
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/dependency-timeout-check.py" "$@"
