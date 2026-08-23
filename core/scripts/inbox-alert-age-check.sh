#!/usr/bin/env bash
# Scan  for aged alert-sweep Unblocks and (optionally) escalate to user.
# See inbox-alert-age-check.py for the full docstring + design rationale.
# Closes finding (2) of  — Apply .
#
# Usage: inbox-alert-age-check.sh [--apply] [--asp-id ]
#                                 [--high-hours N] [--medium-hours N]
#                                 [--board-escalation-log <path>]  # tests only
#                                 [--no-email] [--no-board]        # tests only
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/inbox-alert-age-check.py" "$@"
