#!/usr/bin/env bash
# Detect parent "Apply: <topic>" goals deferred while sibling decomposition
# fully satisfied their intent. See parent-supersession-sweep.py for the full
# docstring. Report-only by default; --apply marks candidates as superseded
# (status=completed with outcome_note "superseded by sibling decomposition").
#
# Usage: parent-supersession-sweep.sh [--max-age-hours N] [--min-siblings N] \
#                                     [--apply] [--output json|human]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/parent-supersession-sweep.py" "$@"
