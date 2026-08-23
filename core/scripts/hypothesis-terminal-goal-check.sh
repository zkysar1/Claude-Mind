#!/usr/bin/env bash
# Surface OPEN goals whose backing hypothesis has already reached a terminal
# pipeline stage (resolved / archived), so a goal whose question is already
# answered stops competing for selector attention. Complement of
# hypothesis-discovered-overdue-sweep.py, which handles the INVERSE case
# (records orphaned in stage=discovered past their deadline) and never looks
# at goals. Detective only — never mutates; see the module docstring in
# hypothesis-terminal-goal-check.py for the verdict ladder, the lane routing,
# and why there is deliberately no --apply.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/hypothesis-terminal-goal-check.py" "$@"
