#!/usr/bin/env bash
# Save current file state to .history/ before overwriting.
# Usage: bash core/scripts/history-save.sh <file> <agent> [summary]
#
# Typically called automatically by write scripts via _fileops.py.
# This shell wrapper exists for manual saves or non-Python scripts.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"

FILE="${1:?Usage: history-save.sh <file> <agent> [summary]}"
AGENT="${2:?Usage: history-save.sh <file> <agent> [summary]}"
SUMMARY="${3:-}"

# Pass args via sys.argv to avoid shell injection from quotes in SUMMARY
# CORE_SCRIPTS env prepend (): _fileops.py lives in $CORE_ROOT/scripts,
# but `python3 -c` puts cwd (PROJECT_ROOT) on sys.path, not core/scripts — so a
# bare `from _fileops import` fails on linux/cc-05 (worked on Windows via the shim).
# Same fix pattern as iteration-close.sh (L551/L1283) + stop-hook.sh (L320): pass
# the dir via env (guard-165 — NOT interpolated into the python source) and
# sys.path.insert it before the import.
CORE_SCRIPTS="$CORE_ROOT/scripts" python3 -c "
import os, sys
sys.path.insert(0, os.environ['CORE_SCRIPTS'])
from _fileops import save_history, resolve_base_dir

base = resolve_base_dir(sys.argv[1])
if base is None:
    print(f'Error: {sys.argv[1]} not under WORLD_DIR or META_DIR', file=sys.stderr)
    sys.exit(1)
save_history(sys.argv[1], base, sys.argv[2], sys.argv[3])
" "$FILE" "$AGENT" "$SUMMARY"
