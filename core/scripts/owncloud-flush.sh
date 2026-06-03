#!/usr/bin/env bash
# DAEMON-ONLY. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
#
# owncloud-flush.sh — force an immediate own-cloud mirror sweep via the daemon.
#
# Pushes every governed-dir file this machine owns (world, meta, owned-agent
# dirs INCLUDING session/ continuity files) to S3 NOW, instead of waiting for
# the daemon's periodic sweep thread's next tick (default 120s). Called by
# /stop's graceful handler (aspirations-graceful-stop D6.7) so a machine-move
# right after a clean /stop cannot strand the session's last continuity writes
# (handoff.yaml, working-memory.yaml, execution-diary.jsonl, ...) on the local
# disk where the next machine would never see them.
#
# Why a daemon endpoint and NOT `py -3 owncloud_sync.py --all`: only the daemon
# has the scoped MIND_AWS_* creds + governed roots loaded in its environment
# (via _load_env_local at daemon start). A bare CLI sweep from the loop would
# lack creds and silently no-op. The endpoint calls the SAME owncloud_sync.sweep()
# the periodic thread calls — one sweep code path, no drift.
#
# Output: a one-line summary on stdout (backend, pushed, errors).
# Exit codes:
#   0  — flush succeeded (incl. local-backend no-op, or call OK but unparseable)
#   1  — daemon returned an error, OR the sweep reported per-file errors>0
#        (so /stop D6.7's `|| echo WARN` fires and the user is told)
#
# Usage: bash core/scripts/owncloud-flush.sh
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

source "$CORE_ROOT/scripts/_runtime.sh"

_do_call() { rt_call POST /v1/admin/owncloud-flush; }

# Capture STDOUT only (no 2>&1). rt_call routes the JSON body to stdout on
# success; rt_check_staleness warnings ("[runtime] WARNING: daemon is running
# stale code ...") and rc=2 error bodies go to STDERR. Merging stderr into
# RESPONSE would prepend non-JSON to the body and break the parser below — and
# a stale daemon is the COMMON case right after a framework edit. Leaving stderr
# unredirected lets those messages flow to the user's terminal harmlessly.
rc=0
RESPONSE="$(_do_call)" || rc=$?

if [ "$rc" = "3" ]; then
    # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback. One spawn try.
    if rt_try_autospawn; then
        rc=0
        RESPONSE="$(_do_call)" || rc=$?
    fi
fi

case $rc in
    0) ;;  # fall through to summary
    # rc=2: rt_curl already printed the error body to stderr (visible above), so
    # RESPONSE is empty here — do not interpolate it.
    2) echo "[owncloud-flush] daemon returned an error (detail on stderr above)" >&2; exit 1;;
    3) rt_no_daemon_error "owncloud-flush.sh";;
    *) echo "[owncloud-flush] unexpected rc=$rc" >&2; exit "$rc";;
esac

# Summarize the JSON body. python exits 0 (clean / no-op) or 2 (sweep errors>0);
# any other rc means python is unavailable or the body was unparseable -> the
# rt_call itself returned 0, so the flush request succeeded; degrade to echoing
# the raw response and exit 0 (do NOT fail a clean stop on a pretty-print hiccup).
# rt_python_launcher is the SSOT for the inline-python launcher (every other
# daemon-aware wrapper uses it): "py -3" on Windows, "python3" on POSIX, empty
# on a Windows box without the `py` launcher. UNQUOTED below so "py -3" splits
# into two words. RESPONSE is passed via env (NOT interpolated into the
# single-quoted source) per guard-165.
PYLAUNCH="$(rt_python_launcher 2>/dev/null || true)"
if [ -z "$PYLAUNCH" ]; then
    # No python launcher: the flush already succeeded; degrade to raw echo.
    echo "[owncloud-flush] (raw) $RESPONSE"
    exit 0
fi
pyrc=0
SUMMARY="$(RESPONSE="$RESPONSE" $PYLAUNCH - <<'PYEOF'
import json, os, sys
try:
    r = json.loads(os.environ["RESPONSE"])
except Exception:
    sys.exit(3)  # unparseable -> bash degrades to raw echo
backend = r.get("backend", "?")
if not r.get("flushed"):
    if "error" in r:
        print(f"[owncloud-flush] FAILED (backend={backend}): {r['error']}")
        sys.exit(2)
    print(f"[owncloud-flush] no-op (backend={backend}; {r.get('reason','')})")
    sys.exit(0)
errs = r.get("errors", 0)
print(f"[owncloud-flush] backend={backend} pushed={r.get('pushed',0)} "
      f"in_sync={r.get('in_sync',0)} scanned={r.get('scanned',0)} "
      f"conflicts={r.get('conflicts',0)} errors={errs}")
sys.exit(2 if errs else 0)
PYEOF
)" || pyrc=$?

case $pyrc in
    0) echo "$SUMMARY"; exit 0;;
    2) echo "$SUMMARY"
       echo "[owncloud-flush] WARN: sweep reported errors — periodic sweep will retry; avoid an immediate machine-move" >&2
       exit 1;;
    *) # python unavailable / unparseable: the call succeeded, just echo raw.
       echo "[owncloud-flush] (raw) $RESPONSE"; exit 0;;
esac
