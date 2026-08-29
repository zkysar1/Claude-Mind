#!/usr/bin/env bash
# Wrapper for closed-against-own-note-check.py — sources _paths.sh so WORLD_PATH /
# STORAGE_BACKEND resolve from the per-agent local-paths.conf (guard-3864: a bare
# `py -3` has the backend but no mappable world root and silently reads the mirror).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"
exec python3 "$SCRIPT_DIR/closed-against-own-note-check.py" "$@"
