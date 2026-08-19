#!/usr/bin/env bash
# Completed-not-closed triage — REPORT-ONLY reducer lane () — thin wrapper.
# Surfaces finished goals still held by a dead worker carrier, for the reducer to
# verify and close. Has no --apply and cannot change claim state.
# See core/scripts/completed-not-closed-triage.py for full docs.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/completed-not-closed-triage.py" "$@"
