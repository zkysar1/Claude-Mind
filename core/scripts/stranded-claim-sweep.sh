#!/usr/bin/env bash
# Stranded Claim Sweep — thin wrapper for stranded-claim-sweep.py.
# See the .py header for the protocol and 4 for the rationale.
#
# Usage:
#   bash core/scripts/stranded-claim-sweep.sh              # dry-run
#   bash core/scripts/stranded-claim-sweep.sh --apply      # release stranded
#   bash core/scripts/stranded-claim-sweep.sh --stale-minutes 10 --apply
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/stranded-claim-sweep.py" "$@"
