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
# BACKEND-POLYMORPHIC — no feature flag. The daemon endpoint (_runner_preamble in
# mind_api/src/endpoints/admin.py) returns {ok:true, noop:true} for any
# non-own-cloud backend, so this wrapper ALWAYS calls the daemon and the daemon
# decides: real DDB CAS under STORAGE_BACKEND=own-cloud, a clean no-op otherwise.
# There is no OWNERSHIP_MODE switch (removed 2026-07-02, ) — single-
# runner enforcement is unconditional, derived from STORAGE_BACKEND alone.
# ════════════════════════════════════════════════════════════════════════════
#
# Usage:
#   bash core/scripts/runner-claim.sh <acquire|heartbeat|release> \
#        [--agent <name>] [--token <uuid>]
#   --agent defaults to $MIND_AGENT; --token defaults to the framework-owned
#   UUID4 at agents/<agent>/session/runner-token.
#
# Exit codes:
#   0  — op succeeded, OR daemon no-op (non-own-cloud backend — nothing to claim)
#   1  — daemon returned an error (caller decides; all three call sites fail open)
#   2  — bad usage (unknown op / missing agent / missing token)
#   4  — acquire only: another machine holds a live claim (held=true) -> refuse
#   5  — release only: released=False — the release did NOT confirm a RUNNING->IDLE
#        transition (idempotent no-op, OR a wedged-daemon stranded self-claim).
#        NOT a hard error; /stop D6.8 surfaces a WARN + handoff note ().
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

[ -z "$AGENT" ] && AGENT="${MIND_AGENT:-}"
if [ -z "$AGENT" ]; then
    echo "[runner-claim] ERROR: no agent (pass --agent <name> or set MIND_AGENT)" >&2
    exit 2
fi

# Token default: the framework-owned UUID4 written at /start (triple-written with
# running-session-id + latest-session-id). Resolve the agent dir via _paths.sh so
# the AGENTS_PARENT_DIR constant stays the single sync point (CLAUDE.md Agent-dir
# Resolution).
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
# acquire via stale-break: the claim was NOT free — a peer's RUNNING claim was
# broken because its heartbeat exceeded OWNERSHIP_STALE_SECONDS. Surface that
# LOUDLY: the 2026-07-07 bravo dual-runner incident started exactly here, when
# this summary printed a plain "acquire: ok" and the /start narration concluded
# "no live peer was detected" while a live runner on another machine lost its
# claim mid-iteration. Exit 0 — stale-break IS the designed crash-takeover path
# — but the operator must see whose claim was broken and how stale it was.
if op == "acquire" and r.get("reclaimed_stale"):
    prev_mid = r.get("prev_machine_id") or "unknown-machine"
    age = r.get("prev_heartbeat_age_seconds")
    age_s = f"{age}s (~{age // 60}m)" if isinstance(age, int) else "unknown age"
    print(f"[runner-claim] acquire: ok — BROKE A STALE CLAIM (backend={backend}) "
          f"previously held by '{prev_mid}', heartbeat age {age_s}. This is the "
          f"crash-takeover path, NOT a clean 'no peer' acquire. If that machine's "
          f"runner could still be alive (e.g. a very long LLM turn), verify it is "
          f"stopped: a live peer that loses its claim keeps running but stops "
          f"syncing (split-brain).")
    sys.exit(0)
if op == "acquire":
    print(f"[runner-claim] acquire: ok (backend={backend} acquired={r.get('acquired')})")
elif op == "release":
    # : released=True means THIS call performed the RUNNING->IDLE
    # transition (clean self-release). released=False is the idempotent no-op —
    # already IDLE/reclaimed, OR (the danger case) a wedged daemon left this
    # machine's claim stranded RUNNING and the release did NOT confirm. The
    # False-return is CORRECT (owncloud_backend.release_runner), but framing it
    # as "ok" + exit 0 buried the stranded-claim case: /stop D6.8's `|| WARN`
    # never fired and the next /start hit rc=4 on its own stale claim. SURFACE
    # released=False loudly and exit 5 ("release unconfirmed", distinct from
    # 2=hard daemon error) so D6.8 can WARN + drop a handoff note.
    if r.get("released") is False:
        print(f"[runner-claim] release: UNCONFIRMED (backend={backend} released=False) "
              f"— this /stop did NOT transition the DDB claim RUNNING->IDLE (already "
              f"released/reclaimed, OR the claim is stranded RUNNING if the daemon was "
              f"wedged). The next /start reclaims a genuinely-stale claim (acquire-before-"
              f"heartbeat, g-328-31); verify runner-state if a stop was expected to "
              f"release a LIVE claim.")
        sys.exit(5)
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
    5) echo "$SUMMARY" >&2; exit 5;;          # release unconfirmed (released=False) -> D6.8 WARN + handoff note ()
    *) echo "[runner-claim] (raw) $RESPONSE"; exit 0;;  # unparseable: call still ok
esac
