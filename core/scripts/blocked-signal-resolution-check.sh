#!/usr/bin/env bash
# Flag status=blocked goals whose block signals (blocked_by / blocker_ref) have
# ALL resolved. Complement of reason-less-blocked-check.sh (which finds blocked
# goals carrying NO block signal). Detective only — never mutates. See the
# module docstring in blocked-signal-resolution-check.py for the predicate,
# the polymorphic-input handling, and why there is no --apply.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/blocked-signal-resolution-check.py" "$@"
