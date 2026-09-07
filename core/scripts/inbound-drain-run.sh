#!/usr/bin/env bash
# inbound-drain-run.sh — thin wrapper for inbound-drain-run.py ().
# Audited entry point for the `inbound-drain` Pattern B executable hook slot; see
# the .py docstring for why the flattener lives in core and why `status` rather
# than a bare count is the answer.
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
_OUT="$(python3 "$_SELF/inbound-drain-run.py" "$@" 2>&1)"
if [ -n "$_OUT" ]; then
    printf '%s\n' "$_OUT"
else
    echo '{"status":"unparseable","drained":0,"failed":[{"file":"-","reason":"wrapper_failed — run py -3 core/scripts/inbound-drain-run.py directly"}]}'
fi
exit 0
