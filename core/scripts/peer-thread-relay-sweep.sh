#!/usr/bin/env bash
# peer-thread-relay-sweep.sh — thin wrapper for peer-thread-relay-sweep.py
# (). Surfaces peer-deployment thread replies that never reached the
# peer; see the .py docstring and _peer_thread_relay.py for the predicate.
#
# NOT fail-open-to-0 like the cadence battery: this sweep's exit code carries
# meaning (1 = stranded directives found, 0 = clean), and flattening a wrapper
# failure to 0 would make "the script broke" indistinguishable from "nothing is
# stranded" — the exact silent-clean class the sweep exists to end. A wrapper
# failure exits 2 instead, which is neither.
set -uo pipefail
_SELF="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$_SELF/_paths.sh" 2>/dev/null || true
python3 "$_SELF/peer-thread-relay-sweep.py" "$@"
rc=$?
if [ "$rc" -gt 1 ]; then
  echo "[peer-thread-relay] wrapper_failed rc=$rc — this is NOT a clean result" >&2
  exit 2
fi
exit "$rc"
