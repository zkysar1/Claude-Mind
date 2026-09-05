#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# loop-exhaustion-fence.sh — the AUTHORIZED stop-signal writer for a loop that
# CANNOT EXECUTE ().
#
# A loop with no room left to act has, until now, had exactly one legal move:
# iterate emptily.  Measured 2026-09-04 (bravo, cc-05): ~35 null iterations over
# 2h21m, execution-diary mtime frozen throughout, at roughly one full model turn
# per 40s indefinitely.  This fence gives that condition a branch.
#
# AUTHORIZATION: `.claude/rules/stop-hook-compliance.md` rule 2 names this script
# as the THIRD authorized writer of `session-signal-set.sh stop-requested`
# outside /stop (productivity-stop-gate.sh first, reducer-self-fence.sh second).
# The recovery-gate / recovery-yank pair listed in that rule move `agent-state`
# rather than setting this signal and are a separate count.  INVOKED ONLY by
# stop-hook.sh.  The LLM MUST NOT invoke this directly.
#
# The DECISION is script-gated in loop_exhaustion_fence.py::decide (pure, fully
# branch-tested) — not LLM-discretionary — so the model cannot elect a stop
# because it feels done.  This wrapper owns only the WRITE.
#
# FAIL-OPEN EVERYWHERE.  A fence that cannot decide must never stop a healthy
# loop and must never delay a hook: every failure path exits 0 without writing.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

AGENT="${MIND_AGENT:-}"
[ -z "$AGENT" ] && exit 0   # not a bound context

# Idempotent: a stop is already in progress, so there is nothing to add and
# re-writing stop-target-mode could race /stop's own write.
if bash "$SCRIPT_DIR/session-signal-exists.sh" stop-requested 2>/dev/null; then
    exit 0
fi

# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || true

# The `|| true` above is load-bearing for fail-open and also lets _paths.sh fail
# SILENTLY, leaving agent_dir undefined so SESSION_DIR would become "/session"
# — under root with a writable / that path is WRITABLE, which would split
# stop-target-mode from the signal (reducer-self-fence.sh F-001, ).
# Validate before writing anything.
SESSION_DIR=""
if declare -f agent_dir >/dev/null 2>&1; then
    _AD="$(agent_dir "$AGENT" 2>/dev/null || true)"
    [ -n "$_AD" ] && [ -d "$_AD" ] && SESSION_DIR="$_AD/session"
fi
if [ -z "$SESSION_DIR" ] || [ ! -d "$SESSION_DIR" ]; then
    echo "[loop-exhaustion-fence] session dir unresolved for agent=$AGENT; holding" >&2
    exit 0
fi

# `py -3` is the Windows launcher shape and `python3` takes no -3, so the flag
# travels WITH the interpreter choice, never appended to whichever won
# (guard-335 / rb-370 — python-invocation.md).
if command -v py >/dev/null 2>&1; then
    PYRUN=(py -3)
else
    PYRUN=(python3)
fi

VERDICT_JSON="$("${PYRUN[@]}" "$SCRIPT_DIR/loop_exhaustion_fence.py" \
    --sid "${HOOK_SID:-}" \
    --log "${HOOK_LOG:-}" \
    --diary "$SESSION_DIR/execution-diary.jsonl" \
    2>/dev/null || true)"
[ -z "$VERDICT_JSON" ] && exit 0

VERDICT="$(printf '%s' "$VERDICT_JSON" | "${PYRUN[@]}" -c \
    "import sys,json;print(json.load(sys.stdin).get('verdict',''))" 2>/dev/null || true)"
REASON="$(printf '%s' "$VERDICT_JSON" | "${PYRUN[@]}" -c \
    "import sys,json;print(json.load(sys.stdin).get('reason',''))" 2>/dev/null || true)"

case "$VERDICT" in
  pause)
    # No write.  The message is the whole rung: it tells the turn to end on a
    # REGISTERED external-wait sleep (stop-hook Gate 2.6 ALLOWs a turn-end that
    # has one) instead of re-entering immediately.  Reversible by construction —
    # if room frees up, the next wake resumes the ordinary loop.
    echo "LOOP-EXHAUSTION PAUSE: ${REASON}. Do NOT re-enter the loop immediately. End this turn on 'EXTERNAL_WAIT=1 bash core/scripts/interruptible-sleep.sh 600', which registers a background job so this turn-end is ALLOWed, then resume normally."
    exit 1
    ;;
  stop)
    # ORDER CRITICAL — /stop Phase -1.4 reads stop-target-mode with no fallback,
    # so the file MUST exist before stop-requested is set.  Do NOT reorder.
    printf 'assistant' > "$SESSION_DIR/stop-target-mode" 2>/dev/null || exit 0
    if ! bash "$SCRIPT_DIR/session-signal-set.sh" stop-requested 2>/dev/null; then
        rm -f "$SESSION_DIR/stop-target-mode" 2>/dev/null || true
        echo "[loop-exhaustion-fence] WARN: stop decided (${REASON}) but session-signal-set failed; reverted stop-target-mode. Loop continues." >&2
        exit 0
    fi
    printf '%s loop-exhaustion-fence stop agent=%s sid=%s verdict=%s\n' \
        "$(date +%Y-%m-%dT%H:%M:%S)" "$AGENT" "${HOOK_SID:-}" "$VERDICT_JSON" \
        >> "$SESSION_DIR/loop-exhaustion-fence.log" 2>/dev/null || true
    echo "LOOP-EXHAUSTION STOP: ${REASON}. stop-requested is now SET (target mode: assistant). Your next action is the ordinary graceful stop at Phase -1.4 — complete in-flight obligations and stop. This was decided by a script gate, not by you."
    exit 2
    ;;
  *)
    exit 0
    ;;
esac
