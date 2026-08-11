#!/usr/bin/env bash
# handoff-currency-check.sh — boot Step 0.5 gate ().
#
# Refuses the ABBREVIATED auto-continuation path when handoff.yaml is far
# behind the journal, which under own-cloud means it was resurrected from the
# backend after boot's local-only consume (guard-1493). Blast radius of NOT
# gating is goal SELECTION: a resurrected first_action reaches the loop as a
# pre-scored top pick and bypasses fresh scoring.
#
# Usage:  bash core/scripts/handoff-currency-check.sh [--agent <name>] [--json]
# Exit:   0 = current (proceed with auto-continuation)
#         2 = STALE  (fall through to a full boot)
#
# FAIL-OPEN (guard-142): every failure of this script's own dependencies —
# unresolvable agent, unreadable journal, missing python — exits 0. The gate
# refuses only on positively-measured staleness, never on absent evidence.
# Threshold: HANDOFF_CURRENCY_MAX_DAYS (default 3).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || { echo "handoff-currency: _paths.sh unavailable — failing open" >&2; exit 0; }

AGENT="${MIND_AGENT:-}"
PASSTHRU=()
AS_JSON=0
while [ $# -gt 0 ]; do
    case "$1" in
        # shift 2 with $#==1 returns non-zero and does NOT shift, so inside a
        # `while [ $# -gt 0 ]` loop $1 is re-processed forever. This script has
        # no `set -e` to abort on the failed shift and uses ${2:-}, which is
        # exactly the hanging conjunction (guard-1224). Caught by
        # test_shift2_argv_hang.py on this very file.
        --agent) AGENT="${2:-}"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --json)  PASSTHRU+=("--json"); AS_JSON=1; shift ;;
        *)       shift ;;
    esac
done

# EVERY early exit reports WHY, on stdout, in the caller's chosen format.
# A gate that declines silently has green as its only observable state, so a
# broken gate and a passing gate are indistinguishable — the exact shape that
# kept the ORIGINAL defect invisible (guard-1977, rb-5871). Costs one line.
_skip() {
    if [ "$AS_JSON" = "1" ]; then
        printf '{"verdict": "skipped", "reason": "%s", "agent": "%s"}\n' "$1" "$AGENT"
    else
        echo "handoff-currency: skipped — $1"
    fi
    exit 0
}

[ -n "$AGENT" ] || _skip "no agent bound — failing open (guard-142)"

HANDOFF="$(agent_dir "$AGENT")/session/handoff.yaml"
# Absent handoff is the ordinary non-continuation boot: nothing to gate. This
# is ALSO the normal mid-session state (boot consumed it), so seeing this
# skip outside boot is expected, not a fault.
[ -f "$HANDOFF" ] || _skip "no local handoff.yaml — not a continuation boot"

# Journal is the freshness reference. Unreadable -> empty -> gate fails open.
JLU="$(MIND_AGENT="$AGENT" bash "$SCRIPT_DIR/journal-read.sh" --meta 2>/dev/null \
        | grep -oE '"last_updated"[[:space:]]*:[[:space:]]*"[^"]*"' \
        | head -1 | sed -E 's/.*"([^"]*)"$/\1/')"

HANDOFF_PATH="$HANDOFF" JOURNAL_LAST_UPDATED="${JLU:-}" MIND_AGENT="$AGENT" \
    python3 "$SCRIPT_DIR/handoff_currency.py" "${PASSTHRU[@]+"${PASSTHRU[@]}"}"
rc=$?
# Only 2 (measured-stale) propagates as a refusal; anything else is fail-open.
[ "$rc" = "2" ] && exit 2
exit 0
