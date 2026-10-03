#!/usr/bin/env bash
# drain-candidates-to-pending.sh — the candidate tier's kill switch, second half ().
#
#   drain-candidates-to-pending.sh [--apply] [--source world|agent|all]
#
# goal-intake-management.md §9: `candidate_tier.enabled: false` plus this script restores the
# status quo. The flag stops NEW candidates; the ones already filed are selector-invisible
# (§2), so this promotes them all to pending in one pass, under no cap. Order matters: set
# the flag false FIRST, then drain, or new candidates keep arriving behind the drain.
#
# A DRY RUN unless --apply. It never edits the flag. Every §5 ledger row it writes is stamped
# promoted_by=kill-switch-drain, which the groomers' promote cap does not count.
#
# Exit: 0 drained, or a dry run | 7 a candidate remains, or the drain is not proven complete |
#       1 error | 9 wrapper plumbing. Full contract: groom.py.
set -euo pipefail
GROOM_HOME="$(cd "$(dirname "$0")" && pwd)"   # not SCRIPT_DIR: _paths.sh rebinds it (guard-5093)
source "$GROOM_HOME/_paths.sh"
cd "$PROJECT_ROOT"
[ -f "$GROOM_HOME/groom.py" ] || { echo "drain-candidates-to-pending.sh: groom.py missing beside this wrapper" >&2; exit 9; }
exec python3 "$GROOM_HOME/groom.py" drain "$@"
