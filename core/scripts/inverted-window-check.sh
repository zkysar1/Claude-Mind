#!/usr/bin/env bash
# Flag non-terminal goals whose `deferred_until` was taken from `resolves_by`
# instead of `resolves_no_earlier_than` — the inverted resolution window of
# guard-3208, which freezes a hypothesis goal for its whole span so it becomes
# selectable only on the day it expires. Detective only; never mutates.
# The five existing defer sweeps do not cover this shape (a FUTURE gate with no
# defer_reason); see the .py docstring for why it is a sibling, not a clause on
# defer-drift-check.sh.
# Sibling pattern: defer-drift-check.sh, reason-less-blocked-check.sh.
#
# Usage: inverted-window-check.sh [--output json|human]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/inverted-window-check.py" "$@"
