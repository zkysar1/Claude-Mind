#!/usr/bin/env bash
# forge-gate-check -- the /forge-skill readiness gate as one command ().
# Called by /forge-skill Step 1 (Validate) and aspirations-evolve Step 9. The
# criteria, where each threshold is read from, and the verdict rules are
# documented in forge-gate-check.py.
#
#   bash core/scripts/forge-gate-check.sh <gap-id> [--category <tree-node-key>] [--json]
#   bash core/scripts/forge-gate-check.sh --all [--json]
#
# Exit 0 = PASS or WAIVED (--all: at least one gap is forge-ready),
#      1 = BLOCK (--all: none is), 2 = could not evaluate.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/forge-gate-check.py" "$@"
