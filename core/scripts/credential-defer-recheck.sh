#!/usr/bin/env bash
# Re-probe human_blocked: defers that name an env-read/credential probe.
# Only auto-clears defers where an env-var key can be extracted AND env-read.sh
# reports the key as now present. Genuinely human-only defers (no env/credential
# indicator word) are never touched. See credential-defer-recheck.py for the full
# docstring and the conservative extraction design.
#
# Dry-run by default; --apply actually clears defer_reason.
# Usage: credential-defer-recheck.sh [--max-age-hours N] [--apply] [--output json|human]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/credential-defer-recheck.py" "$@"
