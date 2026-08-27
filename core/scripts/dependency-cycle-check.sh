#!/usr/bin/env bash
# Walk the blocked_by GRAPH and report dependency cycles (X blocks Y blocks X).
# Every other check inspects a single EDGE, so a ring passes all of them at
# once — see the module docstring in dependency-cycle-check.py for the founding
# incident, why this is a separate sweep from blocked-signal-resolution-check
# (guard-1690's status=blocked dead zone), and why there is no --apply.
# Detective only — never mutates.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/dependency-cycle-check.py" "$@"
