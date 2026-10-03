#!/usr/bin/env bash
# groom-ledger.sh — read / sample the §5 grooming verdict ledger (, B4).
#
#   groom-ledger.sh [--agent <name>] [--verdict <v>] [--since <iso>]
#                   [--sample N | --limit N]
#
# The ledger itself (world/candidate-grooming-ledger.jsonl) is written by the
# update-goal path for every committed candidate transition and by groom.py for
# keep. --sample N draws a DETERMINISTIC sample (lowest sha1 of each row), so
# sprint-planning oversight can sample >=5 verdicts per cycle reproducibly (I4).
# Engine and exit codes: groom.py.
set -euo pipefail
GROOM_HOME="$(cd "$(dirname "$0")" && pwd)"   # not SCRIPT_DIR: _paths.sh rebinds it (guard-5093)
source "$GROOM_HOME/_paths.sh"
cd "$PROJECT_ROOT"
[ -f "$GROOM_HOME/groom.py" ] || { echo "groom-ledger.sh: groom.py missing beside this wrapper" >&2; exit 9; }
exec python3 "$GROOM_HOME/groom.py" ledger "$@"
