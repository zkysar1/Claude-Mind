#!/usr/bin/env bash
# commons-primer.sh: the Mind-side call sites of an agent's commons primer, one
# per (agent, environment) (). The rules and the WHY live in commons-primer.py.
#
#   bash core/scripts/commons-primer.sh birth   # /boot Phase -2: draw once, at first entry to this environment
#   bash core/scripts/commons-primer.sh show    # /prime Phase 2: print it for the first 10 goals here
#
# Exits 0 and prints at least one line saying what happened; only a usage error
# exits non-zero.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/_paths.sh"
exec python3 "$HERE/commons-primer.py" "$@"
