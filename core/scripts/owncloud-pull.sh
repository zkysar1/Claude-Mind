#!/usr/bin/env bash
# DAEMON-ONLY. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
#
# owncloud-pull.sh — pull an agent's continuity-tier session files from S3 to
# local, freshness-aware, via the daemon.
#
# The read-side complement of owncloud-flush.sh. Called by the /start IDLE
# branch BEFORE boot does its raw Read of handoff.yaml, so an agent moved to a
# new machine resumes from the last machine's flushed handoff / working-memory /
# execution-diary / ... instead of a stale (or absent) local copy. The endpoint
# (owncloud_sync.pull_continuity) NEVER clobbers a local file with unpushed local
# writes (the manifest baseline gates every overwrite).
#
# Why a daemon endpoint and NOT a CLI sweep: only the daemon holds the scoped
# MIND_AWS_* creds + governed roots in its env.
#
# Agent resolution: --agent <name> overrides; otherwise MIND_AGENT.
# Output: a one-line summary on stdout. Exit 0 on success / local-backend no-op;
# exit 1 on a daemon error or a per-file pull error.
#
# Usage: bash core/scripts/owncloud-pull.sh [--agent <name>]
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

AGENT=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --agent) AGENT="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        *) echo "[owncloud-pull] unknown arg: $1" >&2; exit 2;;
    esac
done
[ -z "$AGENT" ] && AGENT="${MIND_AGENT:-}"
if [ -z "$AGENT" ]; then
    echo "[owncloud-pull] ERROR: no agent (pass --agent <name> or set MIND_AGENT)" >&2
    exit 2
fi

source "$CORE_ROOT/scripts/_runtime.sh"

QUERY="agent=$(rt_url_encode "$AGENT")"
_do_call() { rt_call POST /v1/admin/owncloud-pull --query "$QUERY"; }

# Capture STDOUT only (no 2>&1): rt_call routes the JSON body to stdout, while
# staleness warnings + rc=2 error bodies go to stderr. Merging stderr would
# corrupt the JSON and break the parser (and a stale daemon is the common case
# right after a framework edit).
rc=0
RESPONSE="$(_do_call)" || rc=$?

if [ "$rc" = "3" ]; then
    if rt_try_autospawn; then
        rc=0
        RESPONSE="$(_do_call)" || rc=$?
    fi
fi

case $rc in
    0) ;;  # fall through to summary
    2) echo "[owncloud-pull] daemon returned an error (detail on stderr above)" >&2; exit 1;;
    3) rt_no_daemon_error "owncloud-pull.sh";;
    *) echo "[owncloud-pull] unexpected rc=$rc" >&2; exit "$rc";;
esac

# rt_python_launcher is the SSOT launcher ("py -3" on Windows). UNQUOTED below so
# "py -3" splits into two words. RESPONSE passed via env (guard-165), not
# interpolated into the single-quoted source.
PYLAUNCH="$(rt_python_launcher 2>/dev/null || true)"
if [ -z "$PYLAUNCH" ]; then
    echo "[owncloud-pull] (raw) $RESPONSE"
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
if not r.get("ok"):
    if "error" in r:
        print(f"[owncloud-pull] FAILED (backend={backend}): {r['error']}")
        sys.exit(2)
    print(f"[owncloud-pull] no-op (backend={backend}; {r.get('reason','')})")
    sys.exit(0)
errs = r.get("errors", 0)
print(f"[owncloud-pull] agent={r.get('agent','?')} backend={backend} "
      f"pulled={r.get('pulled',0)} in_sync={r.get('in_sync',0)} "
      f"scanned={r.get('scanned',0)} s3_absent={r.get('s3_absent',0)} "
      f"local_ahead={r.get('local_ahead_skipped',0)} errors={errs}")
pf = r.get("pulled_files") or []
if pf:
    print(f"[owncloud-pull] pulled: {', '.join(pf)}")
sys.exit(2 if errs else 0)
PYEOF
)" || pyrc=$?

case $pyrc in
    0) echo "$SUMMARY"; exit 0;;
    2) echo "$SUMMARY"
       echo "[owncloud-pull] WARN: pull reported errors — some continuity files may be stale on this machine" >&2
       exit 1;;
    *) echo "[owncloud-pull] (raw) $RESPONSE"; exit 0;;
esac
