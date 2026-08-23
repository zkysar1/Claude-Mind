#!/usr/bin/env bash
# Scan world+agent queues for goals carrying `user` in participants and
# (optionally) EMAIL THE USER ONE DIGEST on a FIXED CADENCE (default 72h), plus
# post a coordination-board record that doubles as the shared schedule marker.
# An EMPTY list still sends — the short all-clear. See
# user-blocker-escalation-check.py for the full docstring + design rationale.
#
# The delivery-channel sibling of dependency-timeout-check.sh /
# handoff-aging-check.sh / inbox-alert-age-check.sh. Those three all escalate to
# the coordination board, which is agent-to-agent and therefore structurally
# incapable of discharging a block whose condition is a HUMAN action — measured
# : 10+ board posts in one day on a HIGH ship-gate blocker while the
# user was never told and proactive_escalation_log stayed empty.
#
# Usage: user-blocker-escalation-check.sh [--apply] [--cadence-hours N] [--agent NAME]
#                                         [--board-escalation-log <path>]  # tests only
#                                         [--no-board] [--no-email]        # tests only
#                                         [--world-aspirations <path>]     # tests only
#                                         [--agent-aspirations <path>]     # tests only
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/user-blocker-escalation-check.py" "$@"
