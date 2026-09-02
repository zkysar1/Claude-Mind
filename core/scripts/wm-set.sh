#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# wm-set — daemon-aware wrapper. Sets a working-memory slot value
# from stdin (JSON or scalar).
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Parse slot arg + optional --override-merge-gate
#   3. Read stdin body (value) BEFORE invoking daemon
#   4. POST /v1/wm/set?slot=<slot>[&override_merge_gate=<justification>]
#   5. On 200, print nothing (CLI byte-compat: cmd_set prints nothing on success)
#
# Usage: echo '"value"' | bash core/scripts/wm-set.sh <slot> [--override-merge-gate <justification>] [--expect-update-count N]
#   rc 9 = 409 stale_write (CAS token mismatch; no write happened)
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
SLOT=""
OVERRIDE_MERGE_GATE=""
EXPECT_UPDATE_COUNT=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --override-merge-gate) OVERRIDE_MERGE_GATE="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --expect-update-count) EXPECT_UPDATE_COUNT="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        *)
            if [ -n "$SLOT" ]; then
                # A second positional is the VALUE handed as an argument — the shape a model
                # reaches for (`wm-set.sh <slot> '<json>'`). It used to silently REPLACE the
                # slot name and send an empty body, and the daemon's `empty_body` reply named
                # neither mistake: measured 2026-08-29 (coach, zc-03), 9 identical retries in
                # one session. Refuse with the exact corrected command instead.
                echo "Error: wm-set.sh takes ONE positional (the slot); the value is read from stdin, not an argument." >&2
                echo "  Run: printf '%s' '$1' | bash core/scripts/wm-set.sh $SLOT" >&2
                exit 1
            fi
            SLOT="$1"; shift;;
    esac
done

if [ -z "$SLOT" ]; then
    echo "Error: slot name required. Usage: printf '%s' '<value>' | bash core/scripts/wm-set.sh <slot> [--override-merge-gate <justification>]" >&2
    exit 1
fi

# Read stdin (the value to set) BEFORE invoking the daemon.
BODY="$(cat)"
if [ -z "$BODY" ]; then
    echo "Error: no value on stdin for slot '$SLOT'. Run: printf '%s' '<json-or-scalar>' | bash core/scripts/wm-set.sh $SLOT" >&2
    exit 1
fi

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY="slot=$(rt_url_encode "$SLOT")"
[ -n "$OVERRIDE_MERGE_GATE" ] && QUERY+="&override_merge_gate=$(rt_url_encode "$OVERRIDE_MERGE_GATE")"
[ -n "$EXPECT_UPDATE_COUNT" ] && QUERY+="&expected_update_count=$(rt_url_encode "$EXPECT_UPDATE_COUNT")"

# rt_curl prints a non-2xx response BODY to stderr and returns 2, so a 409 CAS
# refusal is indistinguishable from any other 4xx at the rc alone. Capture that
# stderr so the 409 can be given its OWN exit code, then re-emit it unchanged so
# the caller still sees current_update_count ().
# `2>&1 >/dev/null` order is load-bearing: duplicate stderr onto the capture
# FIRST, then send stdout to /dev/null.
_wm_set_post() {
    local out rc=0
    out="$(rt_call POST /v1/wm/set --query "$QUERY" --body-string "$BODY" 2>&1 >/dev/null)" || rc=$?
    WM_SET_ERR="$out"
    [ -n "$out" ] && printf '%s\n' "$out" >&2
    return $rc
}

rc=0
WM_SET_ERR=""
_wm_set_post || rc=$?

# rc 9 = CAS refusal (HTTP 409 stale_write): the slot moved under the caller, no
# write happened, and re-reading + re-applying once is the correct response. It
# is deliberately NOT 1 — collapsing "someone else wrote first" into the generic
# failure code is what makes a lost update look like a plumbing error.
_wm_set_exit_for_rc2() {
    case "$WM_SET_ERR" in
        *'"stale_write"'*) exit 9;;
    esac
    exit 1
}

case $rc in
    0) exit 0;;
    2) _wm_set_exit_for_rc2;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            _wm_set_post || rc=$?
            if [ "$rc" = "0" ]; then exit 0; fi
            if [ "$rc" = "2" ]; then _wm_set_exit_for_rc2; fi
        fi
        rt_no_daemon_error "wm-set.sh";;
    *) exit $rc;;
esac
