#!/usr/bin/env bash
# Completed-not-closed DRAIN slate () — thin wrapper.
# Prints the oldest few OPEN (in-progress/pending, undeferred) + noted goals
# THIS agent owns (claimed_by, or unclaimed + executed_by), for the
# reducer's bounded per-iteration disposition (aspirations-precheck Phase
# 0.5g.7). Report-only; has no --apply and cannot change claim state.
# See core/scripts/completed-not-closed-slate.py for full docs.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/completed-not-closed-slate.py" "$@"
