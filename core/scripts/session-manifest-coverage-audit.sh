#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# session-manifest-coverage-audit.sh — wrapper for the pair-consumer entry-type audit.
#
# Verifies every distinct entry-type in core/config/session-manifest.yaml has a
# handler branch in BOTH session_snapshot.py main() and
# session-manifest-clear.sh inline-Python. Exit 0 on full coverage, 1 on miss,
# 2 on usage / parse failure.
#
# Authorized callers:
#   - User-invoked sanity check
#   - /verify-learning Step 3 (once stable — see )
#
# Lineage: , rb-728 (SSOT/re-derive), guard-477 (pair-consumer contract).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_platform.sh"
exec python3 "$CORE_ROOT/scripts/session-manifest-coverage-audit.py" "$@"
