#!/usr/bin/env bash
# mirror-integrity-check.sh — own-cloud mirror integrity gate for chat-mode rituals.
#
# WHY (rb-9443, , 2026-08-27): an assistant/observer session on an
# own-cloud box has NO loop watchdog tick, so MirrorWedgeProbe never fires there,
# and the git-shaped probes those rituals run (status/log/diff) cannot see world/
# at all. Measured 2026-08-27 on an assistant-mode box: 11 tree nodes — a design
# SSOT among them — sat both-diverged FROZEN for 16 sweeps (~2 days) while every
# encode pass printed ENCODED tree:<key> for edits that never left the box. The
# freeze is a silent merge-handler refusal (guard-4778), so owncloud-flush reports
# conflicts=0 and nothing else surfaces it.
#
# WHAT: two checks, both read-only.
#   1. mirror-health.sh verdict (the streak file — persistent both-diverged files).
#   2. Per-session drift read-back: every tree node THIS session encoded
#      (tree-edit-since.py --list, authorship-filtered) is HEADed against the
#      authoritative object with backend-cat.sh --exit-on-drift. A node whose
#      local md5 != remote ETag after the sweep has had its chance is a write that
#      did not land — exactly the silent case above.
#
# Exit: 0 healthy (or not an own-cloud box — n/a, printed); 1 WEDGED or DRIFT
# (act: /reconcile-owncloud-conflicts for class-B files, guard-4778's fenced
# mirror_put recipe for tree nodes — rb-9443 has the worked example);
# 2 indeterminate (could not decide — say so, never read as healthy).
#
# Usage: bash core/scripts/mirror-integrity-check.sh [--since <iso>] [--no-drift]
#   --since   session_start override (default: wm-read.sh session_start)
#   --no-drift  skip check 2 (streak verdict only — the cheap entry probe)
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"

SINCE=""; DRIFT=1
while [ $# -gt 0 ]; do
    case "$1" in
        --since) SINCE="${2:-}"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --no-drift) DRIFT=0; shift ;;
        *) echo "usage: mirror-integrity-check.sh [--since <iso>] [--no-drift]" >&2; exit 2 ;;
    esac
done

# Own-cloud only: on the local backend the file IS the store, drift is unrepresentable.
BACKEND="${STORAGE_BACKEND:-}"
if [ -z "$BACKEND" ] && [ -f "$PROJECT_ROOT/.env.local" ]; then
    BACKEND="$(grep -E '^STORAGE_BACKEND=' "$PROJECT_ROOT/.env.local" | tail -1 | cut -d= -f2- | tr -d '\r"' )"
fi
if [ "$BACKEND" != "own-cloud" ]; then
    echo "mirror-integrity: n/a — backend=${BACKEND:-local} (probe applies to own-cloud boxes only)"
    exit 0
fi

RC=0
# ── 1. persistent both-diverged verdict ────────────────────────────────────────
MH="$(bash "$SCRIPT_DIR/mirror-health.sh" 2>&1)"; MHRC=$?
echo "$MH" | head -20
case "$MHRC" in
    0) ;;
    1) echo "mirror-integrity: WEDGED — tree edits from this box are NOT reaching the fleet; repair before claiming anything is ENCODED (rb-9443, guard-4778)"; RC=1 ;;
    *) echo "mirror-integrity: streak verdict INDETERMINATE (rc=$MHRC) — not evidence of health"; [ "$RC" = 0 ] && RC=2 ;;
esac

# ── 2. per-session edited-node drift read-back ─────────────────────────────────
if [ "$DRIFT" = 1 ]; then
    [ -n "$SINCE" ] || SINCE="$(bash "$SCRIPT_DIR/wm-read.sh" session_start 2>/dev/null | tr -d '\r"')"
    if [ -z "$SINCE" ] || [ "$SINCE" = "null" ]; then
        echo "mirror-integrity: session_start unset — per-session drift check BLIND (not clean; guard-1947)"
        [ "$RC" = 0 ] && RC=2
    else
        TREE="$WORLD_PATH/knowledge/tree"
        NODES="$(python3 "$SCRIPT_DIR/tree-edit-since.py" "$SINCE" --list 2>/dev/null | tr -d '\r')"
        if [ -z "$NODES" ]; then
            echo "mirror-integrity: 0 tree nodes attributed to this session since $SINCE — drift check has nothing to read"
        else
            n=0; drift=0; indet=0
            while IFS= read -r rel; do
                [ -n "$rel" ] || continue
                n=$((n+1))
                p="$TREE/$(echo "$rel" | tr '\\' '/')"
                bash "$SCRIPT_DIR/backend-cat.sh" head "$p" --exit-on-drift >/dev/null 2>&1; r=$?
                case "$r" in
                    0) ;;
                    3) drift=$((drift+1)); echo "  DRIFT  $rel (local != remote — this edit has not landed)" ;;
                    *) indet=$((indet+1)); echo "  ?      $rel (indeterminate rc=$r)" ;;
                esac
            done <<< "$NODES"
            echo "mirror-integrity: session nodes read back: $n checked, $drift drifted, $indet indeterminate"
            if [ "$drift" -gt 0 ]; then RC=1;
            elif [ "$indet" -gt 0 ] && [ "$RC" = 0 ]; then RC=2; fi
        fi
    fi
fi
[ "$RC" = 0 ] && echo "mirror-integrity: OK"
exit "$RC"
