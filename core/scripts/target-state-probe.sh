#!/usr/bin/env bash
# Target-State Probe — execution-time advisory (Phase 4-pre of aspirations loop).
# See target-state-probe.py for full docs. Called from aspirations-execute.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/target-state-probe.py" "$@"
