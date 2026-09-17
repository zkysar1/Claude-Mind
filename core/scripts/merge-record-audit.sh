#!/usr/bin/env bash
# merge-record-audit — diff governed stores BY RECORD ID against a pre-merge state.
#
#   merge-record-audit.sh [PRE_SHA] [--post <SHA>] [--json]
#
# PRE_SHA defaults to ORIG_HEAD (what a just-ran `git merge` leaves behind).
# --post defaults to the WORKING TREE; pass a SHA only to REPLAY an old merge.
#
# Exit 0 = nothing lost · 1 = GENUINE LOSS · 2 = usage / unresolvable pre-state ·
#      4 = INTERNAL FAULT, which is deliberately NOT 1: an uncaught exception exits 1
#          in CPython, so a plumbing failure would otherwise be indistinguishable from
#          data loss to anything reading the exit code (fresh-eyes F1).
# Non-zero means loss and nothing else, so this is safe to wire into a post-merge
# path — which is the point: it turns guard-424 from a rule someone must remember
# into a step that always runs.
#
# Rationale, the seven false-alarm classes it filters, and the guardrails behind
# each: core/scripts/merge_record_audit.py module docstring + the inline comments.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
exec python3 "$CORE_ROOT/scripts/merge_record_audit.py" "$@"
