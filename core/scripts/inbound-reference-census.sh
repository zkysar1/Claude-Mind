#!/usr/bin/env bash
# inbound-reference-census.sh — read-only inbound-reference census for NON-TREE
# artifacts ( / D1). Removes both scope filters of
# tree.py::_iter_body_md_refs (.md-only, tree-paths-only) and scans tree bodies
# + world/conventions/ + the JSONL stores.
# Reports THREE states — live / dangling / unmeasurable — because dangling-ness
# is box-dependent: another agent's temp path is absent here whether or not it
# was purged. See the .py docstring.
# Exit 0 always unless --exit-on-dangling is passed.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/inbound-reference-census.py" "$@"
