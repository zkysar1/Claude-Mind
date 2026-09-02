#!/usr/bin/env bash
# Layer-C detective wrapper — hand-rolled embedded-block extraction sites.
# , closing guard-2222 at the tool layer. Read-only.
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh" || exit 1
export PROJECT_ROOT
exec python3 "$PROJECT_ROOT/core/scripts/embedded-block-hand-roll-audit.py" "$@"
