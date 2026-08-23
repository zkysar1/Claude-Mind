#!/usr/bin/env bash
# Promotion plan-verdict triage — thin wrapper.
#
# Standalone READ-ONLY cross-repo classifier (NOT a daemon endpoint, so no
# runtime/daemon dependency and no Python-CLI-fallback concern — it never
# touches agent state). Run it whenever promote-to-upstream's --plan verdict
# blocks (exit 21): it assigns every flagged file one of four classes
# (DEST-FROZEN / SEED_MOTION / SYNC_VINTAGE / AUTHORED) and emits a
# force-past-plan-ready evidence ledger, or exit 2 when authored residue
# needs a back-port first. See core/scripts/promotion-plan-triage.py for the
# full contract and core/config/conventions/promotion-runbook.md Phase 4 for
# the decision table.
#
# Usage:
#   bash core/scripts/promotion-plan-triage.sh --source <repo> --target <dest-clone> \
#        --plan-log <promote-run-log> [--prior-tag vX.Y.Z] [--json]
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$DIR/promotion-plan-triage.py"
# Windows: `py -3 <file>` avoids the Microsoft Store python3 stub (see
# core/config/conventions/python-invocation.md). Fall back to python3 (shimmed
# by the PreToolUse hook) only if the py launcher is absent.
if command -v py >/dev/null 2>&1; then
  exec py -3 "$SCRIPT" "$@"
else
  exec python3 "$SCRIPT" "$@"
fi
