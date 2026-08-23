#!/usr/bin/env bash
# Census of goals whose claim pair was damaged below the application layer
# (own-cloud fenced-PUT reconcile; rb-3636 sub-mechanism B / class ).
# Read-only. A clean verdict is only meaningful alongside the key_presence
# census it prints -- see the BLIND control in claim-integrity-check.py docs.
#  verification outcome 3 + check 3.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/claim-integrity-check.py" "$@"
