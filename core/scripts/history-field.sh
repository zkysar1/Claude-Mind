#!/usr/bin/env bash
# history-field.sh — read ONE record's ONE field from a historical snapshot.
# Pure exec passthrough: sources _paths.sh so world/ and meta/ virtual prefixes
# resolve, then execs history-field.py with "$@" unchanged. Read-only — see the
# .py docstring for why this is NOT the restore CLI (guard-4165 / guard-5651).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"
exec python3 "$SCRIPT_DIR/history-field.py" "$@"
