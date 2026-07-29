#!/usr/bin/env bash
# One-shot migration to Phase 2.6 per-session dir layout.
#
# Run when ready to retire the legacy .active-agent-<SID> files at
# PROJECT_ROOT. Safe to run multiple times (idempotent).
#
# Usage:
#   bash core/scripts/migrate-to-phase-2-6.sh [--dry-run] [--verbose]
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
py -3 "$SCRIPT_DIR/migrate-to-phase-2-6.py" "$@" \
    || python3 "$SCRIPT_DIR/migrate-to-phase-2-6.py" "$@"
