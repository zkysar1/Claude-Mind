#!/usr/bin/env bash
# DAEMON-ONLY. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
#
# runner-claim.sh — acquire / heartbeat / release the DDB runner claim for an
# agent via the daemon (lodestar dynamic-ownership design §4). The cross-machine
# half of single-runner enforcement: a real DDB session-lock so two machines
# cannot both run the same agent (replaces the advisory team-state heartbeat).
#
# Three operations, mapped to the /v1/admin/runner-* endpoints (mind_api/src/
# endpoints/admin.py):
#   acquire   — IDLE->RUNNING CAS at /start. On a live peer claim the daemon
#               returns held=true; this wrapper exits 4 so /start can refuse the
#               autonomous start (mirror of the local runner-identity refusal).
#   heartbeat — refresh heartbeat_at each iteration (from heartbeat-tick.sh).
#   release   — clean RUNNING->IDLE at /stop, AFTER the final S3 flush (§6).
#
# ════════════════════════════════════════════════════════════════════════════
# CUTOVER GATE — INERT BY DEFAULT (design §7).
# This wrapper no-ops (exit 0) unless OWNERSHIP_MODE=dynamic. Until the gated
# cutover (0) flips that flag, every call here is a cheap no-op: the
# default `static` path makes NO daemon call, NO DDB write — the lifecycle
# call sites (/start, heartbeat-tick.sh, /stop) behave byte-for-byte as before.
# The flag is the SAME one the §3 ownership resolver reads (one cutover switch).
# ════════════════════════════════════════════════════════════════════════════
#
# Usage:
#   bash core/scripts/runner-claim.sh <acquire|heartbeat|release> \
#        [--agent <name>] [--token <uuid>]
#   --agent defaults to $MIND_AGENT; --token defaults to the framework-owned
#   UUID4 at agents/<agent>/session/runner-token.
#
# Exit codes:
#   0  — op succeeded, OR inert no-op (OWNERSHIP_MODE!=dynamic / local backend)
#   1  — daemon returned an error (caller decides; all three call sites fail open)
#   2  — bad usage (unknown op / missing agent / missing token)
#   4  — acquire only: another machine holds a live claim (held=true) -> refuse
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

OP="${1-}"; [ $# -gt 0 ] && shift || true
AGENT=""
TOKEN=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --agent) AGENT="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --token) TOKEN="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        *) echo "[runner-claim] unknown arg: $1" >&2; exit 2;;
    esac
done

case "$OP" in
    acquire)   ENDPOINT="/v1/admin/runner-acquire";;
    heartbeat) ENDPOINT="/v1/admin/runner-heartbeat";;
    release)   ENDPOINT="/v1/admin/runner-release";;
    *) echo "[runner-claim] usage: runner-claim.sh <acquire|heartbeat|release> [--agent N] [--token T]" >&2; exit 2;;
esac

# ── Cutover gate (inert by default). Cheap env read — no subprocess, no file. ──
if [ "${OWNERSHIP_MODE:-static}" != "dynamic" ]; then
    echo "[runner-claim] no-op: OWNERSHIP_MODE=${OWNERSHIP_MODE:-static} (claim wiring inert until cutover)"
    exit 0
fi

[ -z "$AGENT" ] && AGENT="${MIND_AGENT:-}"
if [ -z "$AGENT" ]; then
    echo "[runner-claim] ERROR: no agent (pass --agent <name> or set MIND_AGENT)" >&2
    exit 2
fi

# Token default: the framework-owned UUID4 written at /start (triple-written with
# running-session-id + latest-session-id). Resolve the agent dir via _paths.sh so
# the AGENTS_PARENT_DIR constant stays the single sync point (CLAUDE.md Agent-dir
# Resolution). Only reached in dynamic mode — the inert path above already exited.
if [ -z "$TOKEN" ]; then
    source "$CORE_ROOT/scripts/_paths.sh"
    _TOKFILE="$AGENT_DIR/session/runner-token"
    if [ -r "$_TOKFILE" ]; then
        TOKEN="$(tr -d '[:space:]' < "$_TOKFILE")"
    fi
fi
if [ -z "$TOKEN" ]; then
    echo "[runner-claim] ERROR: no runner token (pass --token, or ensure agents/$AGENT/session/runner-token exists)" >&2
    exit 2
fi

source "$CORE_ROOT/scripts/_runtime.sh"

QUERY="agent=$(rt_url_encode "$AGENT")&token=$(rt_url_encode "$TOKEN")"
_do_call() { rt_call POST "$ENDPOINT" --query "$QUERY"; }

# Capture STDOUT only (no 2>&1): rt_call routes the JSON body to stdout; staleness
# warnings + rc=2 error bodies go to stderr. Merging would corrupt the JSON.
rc=0
RESPONSE="$(_do_call)" || rc=$?

if [ "$rc" = "3" ]; then
    # DAEMON-ONLY: no Python CLI fallback. One spawn try, then re-call.
    if rt_try_autospawn; then
        rc=0
        RESPONSE="$(_do_call)" || rc=$?
    fi
fi

case $rc in
    0) ;;  # fall through to summary
    2) echo "[runner-claim] $OP: daemon returned an error (detail on stderr above)" >&2; exit 1;;
    3) rt_no_daemon_error "runner-claim.sh ($OP)";;
    *) echo "[runner-claim] $OP: unexpected rc=$rc" >&2; exit "$rc";;
esac

# rt_python_launcher is the SSOT launcher ("py -3" on Windows). UNQUOTED so it
# word-splits. RESPONSE + OP passed via env (guard-165), NOT interpolated into
# the single-quoted source.
PYLAUNCH="$(rt_python_launcher 2>/dev/null || true)"
if [ -z "$PYLAUNCH" ]; then
    echo "[runner-claim] (raw) $RESPONSE"
    exit 0
fi
pyrc=0
SUMMARY="$(RESPONSE="$RESPONSE" OP="$OP" $PYLAUNCH - <<'PYEOF'
import json, os, sys
op = os.environ.get("OP", "?")
try:
    r = json.loads(os.environ["RESPONSE"])
except Exception:
    sys.exit(3)  # unparseable -> bash degrades to raw echo
backend = r.get("backend", "?")
if r.get("noop"):
    print(f"[runner-claim] {op}: no-op (backend={backend}; {r.get('reason','')})")
    sys.exit(0)
if not r.get("ok"):
    print(f"[runner-claim] {op}: FAILED (backend={backend}): {r.get('error','?')}")
    sys.exit(2)
# acquire: held=true means a peer owns the live claim -> caller must refuse.
if op == "acquire" and r.get("held"):
    print(f"[runner-claim] acquire: HELD (backend={backend}) — another machine "
          f"owns a live claim for this agent; refuse autonomous start")
    sys.exit(4)
if op == "acquire":
    print(f"[runner-claim] acquire: ok (backend={backend} acquired={r.get('acquired')})")
elif op == "release":
    print(f"[runner-claim] release: ok (backend={backend} released={r.get('released')})")
else:
    print(f"[runner-claim] heartbeat: ok (backend={backend} beat={r.get('beat')})")
sys.exit(0)
PYEOF
)" || pyrc=$?

case $pyrc in
    0) echo "$SUMMARY"; exit 0;;
    2) echo "$SUMMARY" >&2; exit 1;;          # daemon op-level failure
    4) echo "$SUMMARY" >&2; exit 4;;          # acquire held -> caller refuses
    *) echo "[runner-claim] (raw) $RESPONSE"; exit 0;;  # unparseable: call still ok
esac
