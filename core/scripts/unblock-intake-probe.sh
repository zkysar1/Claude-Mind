#!/usr/bin/env bash
# Probe an Unblock goal's intake state — fast re-check at blocker-claim time.
# See unblock-intake-probe.py for the full design (rb-1111,  incident).
# Usage: unblock-intake-probe.sh --goal-id g-XXX-NN [--source world|agent] [--min-age-hours N] [--force]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/unblock-intake-probe.py" "$@"
