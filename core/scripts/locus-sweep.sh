#!/usr/bin/env bash
# locus-sweep.sh — thin wrapper over locus-sweep.py.
#
# Read-only — never mutates. See locus-sweep.py's module docstring for what a
# LOCUS constraint is, why the share is reported as a BRACKET rather than a
# percentage, and why the real fix is a locus field rather than a better regex.
#
# The wrapper exists because a .py with no .sh shipped alongside it is how
# audit-deferred-defers.py sat uncalled (aspirations-precheck SKILL.md ~L1745):
# built, verified-to-exist by a presence-only check, and never once invoked.
# Call site: aspirations-precheck Phase 0.5b lane 0.5b.18.
#
#   bash core/scripts/locus-sweep.sh --output json
#   bash core/scripts/locus-sweep.sh --output markdown
#   bash core/scripts/locus-sweep.sh --band undecided
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

exec python3 "$CORE_ROOT/scripts/locus-sweep.py" "$@"
