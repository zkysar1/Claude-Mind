#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
CMD="${1:-select}"
shift 2>/dev/null || true
source "$CORE_ROOT/scripts/_platform.sh"
# Silent-empty guard (): rc=0 with 0 bytes on BOTH streams was
# observed intermittently on 3 boxes, and NO code path in goal-selector.py can
# produce it — cmd_select/cmd_blocked end in an unconditional print, so an
# empty ranking is "[]", never "". An empty here is a failure to PRODUCE a
# result, and callers (aspirations Phase 2) cannot distinguish it from "no
# candidates" (guard-3440 class). Capture to an UNSYNCED temp file, assert
# non-empty, then emit — the empty becomes a loud nonzero exit. The temp file
# deliberately lives OUTSIDE the repo/agents tree: capture paths inside the
# own-cloud synced tree can be truncated mid-run by the sync (the 
# log-rewrite class), and this guard must not inherit that.
# Exit 7 on the empty — distinct from python tracebacks (1), argparse (2),
# daemon-unreachable (3), and timeout kills (124), so a caller's rc log alone
# identifies the signature. stderr passes through live (banners untouched).
OUT_TMP="$(mktemp "${TMPDIR:-/tmp}/goal-selector-out.XXXXXX")"
trap 'rm -f "$OUT_TMP"' EXIT
rc=0
python3 "$CORE_ROOT/scripts/goal-selector.py" "$CMD" "$@" > "$OUT_TMP" || rc=$?
if [ "$rc" -ne 0 ]; then
  exit "$rc"
fi
if [ ! -s "$OUT_TMP" ]; then
  echo "[goal-selector.sh] FATAL: goal-selector.py exited 0 with EMPTY stdout — the g-115-6146 silent-empty signature, not a legitimate result (an empty ranking prints '[]'). Do NOT read this as 'no candidates'; re-run the selector." >&2
  exit 7
fi
cat "$OUT_TMP"
