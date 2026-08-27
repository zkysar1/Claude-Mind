#!/usr/bin/env bash
# world-script-crlf-check.sh — thin wrapper for world-script-crlf-check.py
# (). Asserts every executable *.sh under world/scripts and
# core/scripts is LF-only; see the .py docstring for why bash cannot be made
# CRLF-tolerant the way the GAE-2 data-file parsers were, and for the positive
# control showing own-cloud delivers CRLF into world/scripts today.
#
# Report-only by contract: there is no --apply and there must never be one.
# Repairing a file under world/ writes into a live own-cloud-synced store and
# can race the sync.
#
# Fail-open by design: an always-run precheck lane must never block the loop, so
# any wrapper-level failure still exits 0 with a structured line (guard-614).
#
# Args pass straight through ("$@") — deliberately NO bash-side arg parsing, so
# there is no `shift 2` to get wrong (guard-1224) and exactly one parser owns the
# flag surface (the .py's argparse). Add flags THERE, never here.
set -uo pipefail
_SELF="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$_SELF/_paths.sh" 2>/dev/null || true
python3 "$_SELF/world-script-crlf-check.py" "$@" \
  || echo '{"check":"world-script-crlf","scanned":0,"offender_count":0,"offenders":[],"failed":["wrapper_failed — run py -3 core/scripts/world-script-crlf-check.py directly"],"per_root":[]}'
exit 0
