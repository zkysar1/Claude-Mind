#!/usr/bin/env bash
# Scan world+agent queues for aged cross-agent handoffs and (optionally)
# post coordination-board visibility notes. See handoff-aging-check.py for
# the full docstring + design rationale. Bash-enforces precheck Phase
# 0.5b.2b — closes the LLM-only-pseudocode gap surfaced by fresh-eyes-review
# 2026-06-18 (; sibling of inbox-alert-age-check.sh / ).
#
# Usage: handoff-aging-check.sh [--apply] [--escalate-hours N] [--agent NAME]
#                               [--board-escalation-log <path>]  # tests only
#                               [--no-board]                      # tests only
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/handoff-aging-check.py" "$@"
