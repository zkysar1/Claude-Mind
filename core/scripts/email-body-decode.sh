#!/usr/bin/env bash
# Decode ONE raw MIME email into BOTH readable slices (gap-070, ).
#
# Thin wrapper over core/scripts/email_body_decode.py. Sourcing _paths.sh puts
# the .python-shim on PATH so `python3` resolves to a real interpreter inside
# this script -- the canonical "python3 only inside a .sh that sources
# _paths.sh" pattern (CLAUDE.md Python Invocation).
#
# Raw MIME arrives on STDIN (or via --file). Fetching the message is NOT this
# script's job and is deliberately out of scope -- the alert and agent-inbox
# lanes already own their own fetch, and pulling bucket/key resolution in here
# would make a domain-free helper domain-coupled. Pipe their raw output in:
#
#   bash "$WORLD_PATH/scripts/email-read.sh" read <key> | bash core/scripts/email-body-decode.sh
#   cat msg.eml | bash core/scripts/email-body-decode.sh --format text --slice full
#   bash core/scripts/email-body-decode.sh --file msg.eml
#
# Exit: 0 decoded · 2 empty input · non-zero from python on a hard parse error.
set -euo pipefail

_SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_SELF_DIR/../.." && pwd)"
# shellcheck disable=SC1091
source "$PROJECT_ROOT/core/scripts/_paths.sh"

exec python3 "$PROJECT_ROOT/core/scripts/email_body_decode.py" "$@"
