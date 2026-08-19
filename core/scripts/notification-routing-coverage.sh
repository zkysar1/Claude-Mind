#!/usr/bin/env bash
# Thin wrapper around notification-routing-coverage.py ().
#
# guard-350: SKILL.md pseudocode must reach Python through a .sh wrapper.
# /verify-learning calls this; the engine is the .py next to it.
#
# EXIT CODE IS THE VERDICT and is PROPAGATED deliberately:
#   0 = every detected user-email sender is routed or reasoned-allowlisted
#   1 = at least one unrouted sender (the report names each)
#   2 = the scan could not read a root — NOT a pass, and distinguished from 0
#       on purpose: a zero-hit report from a scan that read nothing is the
#       rb-245 false clean.
set +e
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"

python3 "$CORE_ROOT/scripts/notification-routing-coverage.py" "$@"
exit $?
