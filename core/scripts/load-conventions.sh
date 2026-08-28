#!/usr/bin/env bash
# Batch convention check — print convention file paths NOT yet in context.
# Usage: load-conventions.sh aspirations pipeline experience ...
# Output: absolute paths of convention files that need reading (one per line).
# If output is empty, all conventions are already loaded — skip reads.
set -euo pipefail
# No names = nothing requested (a skill whose front matter lists no conventions still runs
# "Step 0: Load Conventions"). That is a clean no-op, not a usage error: the argparse
# failure this used to produce (rc=2) sent a served small model into a load-conventions.sh
# investigation instead of its next step (coach, 2026-08-28, three sessions in a row).
if [ "$#" -eq 0 ]; then
  exit 0
fi
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/context-reads.py" check "$@"
