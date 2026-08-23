#!/usr/bin/env bash
# D2 tree-node cluster archival () — thin pure-CLI wrapper.
# Subcommands: scan | apply <key> <verdict> | restore <key> | cluster <key>.
# Writes _tree.yaml via tree.py's file-locked write_tree (history + changelog
# tracked) — NOT daemon-routed: this is a maintenance/analysis tool, not one of
# the 35 daemon-migrated wrappers. Natural-gated DORMANT (archives_per_pass=0
# in core/config/aspirations.yaml -> tree_archival). See tree_archive.py for docs
# and d2-cluster-archival-design.md for the design.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/tree_archive.py" "$@"
