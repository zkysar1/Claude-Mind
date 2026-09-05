#!/usr/bin/env bash
# recent-completions-packet.sh -- the recent-completions review packet (gap-079).
# Thin wrapper over recent_completions_packet.py; joins completed non-recurring
# goals (with descriptions + outcome_note, absence reported explicitly) with the
# unfiltered hypothesis stage histogram.
#   recent-completions-packet.sh [--limit N] [--since ISO] [--json]
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"
if command -v cygpath >/dev/null 2>&1; then SCRIPT_DIR_NATIVE="$(cygpath -w "$SCRIPT_DIR")"; else SCRIPT_DIR_NATIVE="$SCRIPT_DIR"; fi
exec python3 "$SCRIPT_DIR_NATIVE/recent_completions_packet.py" "$@"
