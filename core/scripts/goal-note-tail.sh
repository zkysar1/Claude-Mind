#!/usr/bin/env bash
# goal-note-tail — read the END of an append-ordered goal narrative field.
#
#   goal-note-tail.sh [--source world|agent] <goal-id> [--field <f>]
#                     [--blocks N] [--json]
#
# guard-2043 forbids a TRUNCATED read of an append-ordered field because the
# corrective block is at the END. This is the inverse: it drops the BEGINNING
# and reports exactly what it dropped (blocks, bytes, markers), so the omission
# is explicit and recoverable instead of silent. It is the third move for a goal
# whose read surface is over the claim cap — previously the only options were
# "read 100+ KB whole" or "release it unstarted".
#
# READ-ONLY by construction: it imports the store READER and no writer.
#
# The arg grammar lives in goal-note-tail.py's argparse (one source of truth);
# this wrapper adds no parser of its own, so there is no PASSTHROUGH arm and no
# flag-transfer hazard (guard-1047 / guard-2525 class).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"

exec python3 "$CORE_ROOT/scripts/goal-note-tail.py" "$@"
