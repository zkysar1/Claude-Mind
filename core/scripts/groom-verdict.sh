#!/usr/bin/env bash
# groom-verdict.sh — apply ONE ledgered grooming verdict to ONE candidate (, B4).
#
#   groom-verdict.sh --goal <id> --verdict promote|merge|rb-route|close-moot|keep
#                    [--evidence "<why>"] [--survivor <goal-id>] [--rb-id rb-NNN]
#                    [--rehome-to <asp-id>] [--source world|agent] [--dry-run]
#
# The LLM chooses the verdict; the engine enforces every bound (I4): the promote
# cap is counted from the §5 ledger inside the engine, the goal must still be a
# live candidate at the store of record, and survivor / rb-id / re-home target
# are validated before any write. A non-keep verdict on a non-recurring legacy-
# inbox goal also moves it (move-on-touch, I6).
#
# Exit: 0 applied | 4 stale | 5 promote cap | 6 invalid argument | 7 partial |
#       1 error | 9 wrapper plumbing. Full contract: groom.py.
set -euo pipefail
GROOM_HOME="$(cd "$(dirname "$0")" && pwd)"   # not SCRIPT_DIR: _paths.sh rebinds it (guard-5093)
source "$GROOM_HOME/_paths.sh"
cd "$PROJECT_ROOT"
[ -f "$GROOM_HOME/groom.py" ] || { echo "groom-verdict.sh: groom.py missing beside this wrapper" >&2; exit 9; }
exec python3 "$GROOM_HOME/groom.py" verdict "$@"
