#!/usr/bin/env bash
# Flag code-deliverable goals closed status=completed whose commit is absent
# from origin past a 30-min push-throttle window (rb-3135 completed!=committed
# class). Detective only by default — never mutates goal state; --apply files
# ONE dedup'd Investigate per flagged goal into . See
# completed-not-committed-sweep.py for full semantics and the  /
# rb-3135 incident (4 instances in ~1 week, each caught by hand, none by a gate).
# Sibling pattern: defer-drift-check.sh, unblock-parent-status-sweep.sh,
# parent-supersession-sweep.sh (rb-428 detective-sweep family).
#
# Usage: completed-not-committed-sweep.sh [--apply] [--output json|human] \
#          [--min-age-minutes N] [--lookback-hours N]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/completed-not-committed-sweep.py" "$@"
