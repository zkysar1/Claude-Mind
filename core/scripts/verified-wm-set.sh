#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / cadence-stamp critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# verified-wm-set.sh -- Write a value to a WM slot with read-back verification + retry-once.
#
# Generalizes the verified-write pattern that fresh-eyes-record-tick.sh applies
# to its cadence stamps. Routes a plain `echo <value> | wm-set.sh <slot>` through
# a write -> read-back -> assert-match -> retry-once guard, so a SILENT DROP
# (wm-set returns exit-0 but the value does not persist -- the load-bearing
#  /  class) FAILS LOUD instead of going undetected.
#
# Contract:
#   - Reads the value to write from stdin (JSON scalar or object).
#   - Writes it to the named WM slot via wm-set.sh.
#   - Reads the slot back (wm-read.sh --json) and asserts JSON-canonical equality.
#   - On mismatch, retries ONCE; on persistent mismatch exits 1 with a diagnostic.
#   - Emits a single status line on stdout (success) or stderr (failure) -- the
#     same loud-on-failure posture as fresh-eyes-record-tick.sh (guard-614:
#     consistent structured output on every exit path).
#
# Usage:  echo '<json-value>' | verified-wm-set.sh <slot-name>
# Origin: 6 (board insight-trigger msg-20260613-122037-alpha-2004).
#         Single-writer of any given cadence slot stays with its owning skill
#         (guard-155); this wrapper only hardens the WRITE MECHANISM.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/_paths.sh"

if [[ $# -lt 1 || -z "${1:-}" ]]; then
    echo "verified-wm-set: ERROR -- slot name required (usage: echo '<value>' | verified-wm-set.sh <slot>)" >&2
    exit 1
fi
SLOT_NAME="$1"

# Read the value to write from stdin (mirrors the wm-set.sh stdin contract).
VALUE="$(cat)"
if [[ -z "$VALUE" ]]; then
    echo "verified-wm-set: ERROR -- empty value on stdin for slot '$SLOT_NAME'" >&2
    exit 1
fi

# JSON-canonical equality of the just-written value vs the slot read-back.
# Values are passed as ARGV (NOT interpolated into the python source) per
# guard-165. The helper always prints 1/0 and exits 0; on a JSON parse error
# it falls back to a trimmed exact-string compare so a non-JSON edge never
# crashes the gate. python3 here is sanctioned -- this .sh sources _paths.sh
# (CLAUDE.md python-invocation rule; matches fresh-eyes-record-tick.sh:89).
_values_match() {
    python3 -c '
import json, sys
a, b = sys.argv[1], sys.argv[2]
try:
    print("1" if json.loads(a) == json.loads(b) else "0")
except Exception:
    print("1" if a.strip() == b.strip() else "0")
' "$1" "$2"
}

# One write+verify attempt. Returns 0 on verified persist, non-zero otherwise.
# Read-back is guarded so a transient wm-read failure becomes a mismatch
# (-> retry) rather than a set -e abort.
_write_and_verify() {
    if ! printf '%s' "$VALUE" | bash "$SCRIPT_DIR/wm-set.sh" "$SLOT_NAME" >/dev/null; then
        return 1
    fi
    local readback="__READ_FAIL__"
    readback="$(bash "$SCRIPT_DIR/wm-read.sh" "$SLOT_NAME" --json)" || readback="__READ_FAIL__"
    if [[ "$(_values_match "$VALUE" "$readback")" == "1" ]]; then
        return 0
    fi
    return 2
}

if _write_and_verify; then
    echo "verified-wm-set: $SLOT_NAME landed (verified)"
    exit 0
fi

# Retry once -- the canonical recovery for the transient silent-drop class
# (the caller of fresh-eyes-record-tick.sh achieved the same via cadence
# re-fire; this wrapper retries in-process so a single drop self-heals).
if _write_and_verify; then
    echo "verified-wm-set: $SLOT_NAME landed on retry (verified)"
    exit 0
fi

echo "verified-wm-set: ERROR -- '$SLOT_NAME' did not persist after write + retry (silent-drop class; value on stdin not confirmed on disk)" >&2
exit 1
