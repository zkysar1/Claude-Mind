#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# shipped-claim-store-check.sh
# Thin wrapper over core/scripts/shipped-claim-store-check.py.
#
# Detects a completed goal whose outcome_note CLAIMS a named symbol was
# shipped into a store-backed artifact, when the STORE's copy of that
# artifact contains the symbol zero times.
#
# Canonical incident (, 2026-08-22): closed `completed` claiming a
# `--direct` mode and a `probe_direct()` in zakpod1-pp-aging-probe.py; the
# store's world/scripts/zakpod1-pp-aging-probe.py (24,976 B) contains neither,
# and its docstring argues the opposite design. The existing
# goal-completion-artifact-gate passes this cleanly -- it reads title +
# description (not outcome_note), tests Path.exists() (not content), on the
# local disk (not the store), and world/scripts is not among its path roots.
#
# Usage:
#   bash shipped-claim-store-check.sh --goal <goal-id> [--note "<text>"]
#   outcome_note (multi-line prose) passed via stdin when --note is omitted.
#
# Output: single-line JSON on stdout (gates/shipped_claim.evaluate payload
# plus `resolved`). Exit 0 = clean / no claim / internal error (fail-open),
# 1 = mismatch found. A mismatch is a REPORT, never a block -- the goal is
# already closed by the time this runs.
#
# Cross-refs:
#   - logic:    core/scripts/gates/shipped_claim.py (pure, unit-tested)
#   - sibling:  core/scripts/goal-completion-artifact-gate.py (existence, at
#               close time) -- this is the CONTENT arm, at state-update time
#   - caller:   core/scripts/iteration-close.sh do_state_update
#   - ledger:   world/shipped-claim-mismatches.jsonl
#   - this Apply: 

# CRITICAL -- DO NOT add `set -e`. Per guard-141 framework gates MUST fail
# open on every error path; this runs inside iteration-close.sh
# do_state_update and blocking on a gate error would crash the obligation
# chain.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/_paths.sh"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/_platform.sh"

py -3 "$SCRIPT_DIR/shipped-claim-store-check.py" "$@"
rc=$?
if [ "$rc" -ne 0 ] && [ "$rc" -ne 1 ]; then
  # Any unexpected rc (missing interpreter, import failure before the
  # script's own fail-open handler) is reported as a clean no-op rather
  # than propagated -- see the set -e note above.
  echo '{"fired":false,"mismatches":[],"claims_checked":0,"reason":"wrapper-error"}'
  rc=0
fi
exit "$rc"
