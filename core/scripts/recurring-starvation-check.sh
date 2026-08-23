#!/usr/bin/env bash
# Detect recurring goals that have silently STOPPED firing — the open-loop
# blind spot left by every close-triggered cadence detector (the streak-break
# canary only fires when a recurring goal CLOSES late; one that stops closing
# emits nothing). See recurring-starvation-check.py for the full docstring,
# both evidence gates, and why rank is deliberately not used.
#
# Report-only by default; --apply files at most --max-file Unblock goals
# (default 1) for the worst offenders, deduplicated on exact origin_signal.
#
# Usage: recurring-starvation-check.sh [--multiplier N] [--apply]
#                                      [--max-file N] [--output human|json]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/recurring-starvation-check.py" "$@"
